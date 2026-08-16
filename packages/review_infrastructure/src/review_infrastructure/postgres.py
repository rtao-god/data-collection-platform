from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from review_application import (
    ReviewCaseDetail,
    ReviewCaseSummary,
    ReviewConflict,
    ReviewNotFound,
    ReviewQueueCursor,
    ReviewQueuePage,
)
from review_contracts import (
    CandidateEvidence,
    CandidateRevision,
    ManualObservation,
    ManualObservationCommand,
    QualityRecord,
    ReviewCase,
    ReviewDecision,
    ReviewDecisionCommand,
    SuppressionCommand,
    SuppressionRevision,
    SuppressionScope,
    deterministic_suppression_id,
)
from review_core import (
    ReviewDecisionConflict,
    StaleReviewRevision,
    SuppressionTransitionError,
    activate_suppression,
    create_manual_observation,
    decide_review_case,
    resolve_suppression,
)


class PostgresReviewRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list_cases(
        self,
        *,
        state: str,
        limit: int,
        cursor: ReviewQueueCursor | None,
    ) -> ReviewQueuePage:
        query = sa.text(
            """
            WITH latest AS (
                SELECT DISTINCT ON (case_id)
                    case_id,
                    revision,
                    state,
                    reason_codes,
                    current_decision_id,
                    recorded_at_utc
                FROM review.review_case_revisions
                ORDER BY case_id, revision DESC
            )
            SELECT
                c.case_id,
                c.candidate_id,
                c.candidate_revision,
                latest.revision,
                latest.state,
                latest.reason_codes,
                latest.current_decision_id,
                latest.recorded_at_utc
            FROM latest
            JOIN review.review_cases AS c ON c.case_id = latest.case_id
            WHERE latest.state = :state
              AND (
                    CAST(:cursor_at AS timestamptz) IS NULL
                    OR (latest.recorded_at_utc, latest.case_id)
                       < (CAST(:cursor_at AS timestamptz), CAST(:cursor_id AS uuid))
              )
            ORDER BY latest.recorded_at_utc DESC, latest.case_id DESC
            LIMIT :row_limit
            """
        )
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    query,
                    {
                        "state": state,
                        "cursor_at": None if cursor is None else cursor.recorded_at_utc,
                        "cursor_id": None if cursor is None else cursor.case_id,
                        "row_limit": limit + 1,
                    },
                )
                .mappings()
                .all()
            )
        visible = rows[:limit]
        items = tuple(_summary(row) for row in visible)
        next_cursor = None
        if len(rows) > limit and visible:
            last = visible[-1]
            next_cursor = ReviewQueueCursor(
                recorded_at_utc=last["recorded_at_utc"],
                case_id=last["case_id"],
            )
        return ReviewQueuePage(items=items, next_cursor=next_cursor)

    def get_case(self, case_id: UUID) -> ReviewCaseDetail:
        with self._engine.connect() as connection:
            case = self._load_case(connection, case_id, for_update=False)
            candidate = self._load_candidate(
                connection,
                case.candidate_id,
                case.candidate_revision,
            )
            quality = self._load_quality(
                connection,
                case.candidate_id,
                case.candidate_revision,
            )
            decisions = tuple(
                _decision(row)
                for row in connection.execute(
                    sa.text(
                        """
                        SELECT *
                        FROM review.review_decisions
                        WHERE case_id = :case_id
                        ORDER BY case_revision, decision_id
                        """
                    ),
                    {"case_id": case_id},
                ).mappings()
            )
            observations = tuple(
                _observation(row)
                for row in connection.execute(
                    sa.text(
                        """
                        SELECT *
                        FROM review.manual_observations
                        WHERE candidate_id = :candidate_id
                        ORDER BY recorded_at_utc, observation_id
                        """
                    ),
                    {"candidate_id": case.candidate_id},
                ).mappings()
            )
            suppressions = tuple(
                _suppression(row)
                for row in connection.execute(
                    sa.text(
                        """
                        WITH latest AS (
                            SELECT DISTINCT ON (suppression_id) *
                            FROM review.suppression_revisions
                            WHERE target_kind = 'candidate'
                              AND target_id = :target_id
                            ORDER BY suppression_id, revision DESC
                        )
                        SELECT *
                        FROM latest
                        WHERE state = 'active'
                          AND (expires_at_utc IS NULL OR expires_at_utc > CURRENT_TIMESTAMP)
                        ORDER BY suppression_id
                        """
                    ),
                    {"target_id": str(case.candidate_id)},
                ).mappings()
            )
        return ReviewCaseDetail(
            case=case,
            candidate=candidate,
            quality=quality,
            decisions=decisions,
            manual_observations=observations,
            active_suppressions=suppressions,
        )

    def submit_decision(
        self,
        command: ReviewDecisionCommand,
        *,
        now_utc: datetime,
    ) -> tuple[ReviewCase, ReviewDecision]:
        try:
            with self._engine.begin() as connection:
                replay = self._decision_replay(connection, command)
                if replay is not None:
                    return replay
                case = self._load_case(connection, command.case_id, for_update=True)
                try:
                    next_case, decision = decide_review_case(
                        case,
                        command,
                        now_utc=now_utc,
                    )
                except (ReviewDecisionConflict, StaleReviewRevision) as exc:
                    raise ReviewConflict(
                        str(exc),
                        "Reload the review case and retry against its current revision.",
                    ) from exc
                connection.execute(sa.text("SET CONSTRAINTS ALL DEFERRED"))
                self._insert_case_revision(connection, next_case)
                self._insert_decision(connection, decision)
                return next_case, decision
        except IntegrityError as exc:
            raise ReviewConflict(
                "The review decision conflicts with persisted immutable history.",
                "Reload the case and retry the exact command against current state.",
            ) from exc

    def add_manual_observation(
        self,
        command: ManualObservationCommand,
        *,
        now_utc: datetime,
    ) -> ManualObservation:
        try:
            with self._engine.begin() as connection:
                replay = (
                    connection.execute(
                        sa.text(
                            """
                        SELECT * FROM review.manual_observations
                        WHERE command_digest = :command_digest
                        """
                        ),
                        {"command_digest": command.command_digest},
                    )
                    .mappings()
                    .one_or_none()
                )
                if replay is not None:
                    observation = _observation(replay)
                    _verify_observation_replay(observation, command)
                    return observation
                self._require_candidate_revision(
                    connection,
                    command.candidate_id,
                    command.candidate_revision,
                    for_update=True,
                )
                if command.supersedes_observation_id is not None:
                    superseded = (
                        connection.execute(
                            sa.text(
                                """
                            SELECT candidate_id, field_key
                            FROM review.manual_observations
                            WHERE observation_id = :observation_id
                            """
                            ),
                            {"observation_id": command.supersedes_observation_id},
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if superseded is None:
                        raise ReviewNotFound(
                            "The superseded manual observation does not exist.",
                            "Reload candidate evidence before creating a replacement observation.",
                        )
                    if (
                        superseded["candidate_id"] != command.candidate_id
                        or superseded["field_key"] != command.field_key
                    ):
                        raise ReviewConflict(
                            "A manual observation can supersede only the same candidate field.",
                            "Select the current observation for the exact candidate field.",
                        )
                observation = create_manual_observation(command, now_utc=now_utc)
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO review.manual_observations (
                            observation_id, candidate_id, candidate_revision, field_key,
                            value_text, value_digest, actor_id, reason_code,
                            supersedes_observation_id, command_digest, recorded_at_utc,
                            correlation_id
                        ) VALUES (
                            :observation_id, :candidate_id, :candidate_revision, :field_key,
                            :value_text, :value_digest, :actor_id, :reason_code,
                            :supersedes_observation_id, :command_digest, :recorded_at_utc,
                            :correlation_id
                        )
                        """
                    ),
                    observation.model_dump(),
                )
                return observation
        except IntegrityError as exc:
            raise ReviewConflict(
                "The manual observation conflicts with persisted immutable history.",
                "Reload candidate evidence and retry the exact command.",
            ) from exc

    def get_suppression(self, suppression_id: UUID) -> SuppressionRevision:
        with self._engine.connect() as connection:
            return self._load_suppression(connection, suppression_id, for_update=False)

    def activate_suppression(
        self,
        command: SuppressionCommand,
        *,
        now_utc: datetime,
    ) -> SuppressionRevision:
        suppression_id = deterministic_suppression_id(
            command.target_kind,
            command.target_id,
            command.scopes,
            command.reason_code,
        )
        try:
            with self._engine.begin() as connection:
                replay = self._suppression_replay(connection, command)
                if replay is not None:
                    return replay
                existing = connection.execute(
                    sa.text(
                        """
                        SELECT suppression_id
                        FROM review.suppression_revisions
                        WHERE suppression_id = :suppression_id
                        LIMIT 1
                        FOR UPDATE
                        """
                    ),
                    {"suppression_id": suppression_id},
                ).scalar_one_or_none()
                if existing is not None:
                    raise ReviewConflict(
                        "The suppression identity already has immutable history.",
                        "Resolve or inspect the existing suppression instead of reactivating it.",
                    )
                suppression = activate_suppression(command, now_utc=now_utc)
                self._insert_suppression(connection, suppression)
                return suppression
        except IntegrityError as exc:
            raise ReviewConflict(
                "The suppression conflicts with persisted immutable history.",
                "Reload suppression state and retry the exact command.",
            ) from exc

    def resolve_suppression(
        self,
        command: SuppressionCommand,
        *,
        now_utc: datetime,
    ) -> SuppressionRevision:
        suppression_id = deterministic_suppression_id(
            command.target_kind,
            command.target_id,
            command.scopes,
            command.reason_code,
        )
        try:
            with self._engine.begin() as connection:
                replay = self._suppression_replay(connection, command)
                if replay is not None:
                    return replay
                current = self._load_suppression(
                    connection,
                    suppression_id,
                    for_update=True,
                )
                try:
                    resolved = resolve_suppression(
                        current,
                        command,
                        now_utc=now_utc,
                    )
                except (StaleReviewRevision, SuppressionTransitionError) as exc:
                    raise ReviewConflict(
                        str(exc),
                        "Reload the suppression and retry against its current revision.",
                    ) from exc
                self._insert_suppression(connection, resolved)
                return resolved
        except IntegrityError as exc:
            raise ReviewConflict(
                "The suppression resolution conflicts with immutable history.",
                "Reload suppression state and retry the exact command.",
            ) from exc

    def _load_case(
        self,
        connection: sa.Connection,
        case_id: UUID,
        *,
        for_update: bool,
    ) -> ReviewCase:
        if for_update:
            statement = sa.text(
                """
                SELECT
                    c.case_id,
                    c.candidate_id,
                    c.candidate_revision,
                    c.opened_at_utc,
                    r.revision,
                    r.state,
                    r.reason_codes,
                    r.current_decision_id,
                    r.recorded_at_utc,
                    r.correlation_id
                FROM review.review_cases AS c
                JOIN review.review_case_revisions AS r
                  ON r.case_id = c.case_id
                WHERE c.case_id = :case_id
                ORDER BY r.revision DESC
                LIMIT 1
                FOR UPDATE OF r
                """
            )
        else:
            statement = sa.text(
                """
                SELECT
                    c.case_id,
                    c.candidate_id,
                    c.candidate_revision,
                    c.opened_at_utc,
                    r.revision,
                    r.state,
                    r.reason_codes,
                    r.current_decision_id,
                    r.recorded_at_utc,
                    r.correlation_id
                FROM review.review_cases AS c
                JOIN review.review_case_revisions AS r
                  ON r.case_id = c.case_id
                WHERE c.case_id = :case_id
                ORDER BY r.revision DESC
                LIMIT 1
                """
            )
        row = (
            connection.execute(
                statement,
                {"case_id": case_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ReviewNotFound(
                f"Review case {case_id} does not exist.",
                "Refresh the review queue and select an existing case.",
            )
        return _case(row)

    def _load_candidate(
        self,
        connection: sa.Connection,
        candidate_id: UUID,
        revision: int,
    ) -> CandidateRevision:
        row = (
            connection.execute(
                sa.text(
                    """
                SELECT
                    c.candidate_id,
                    c.entity_kind,
                    r.revision,
                    r.cluster_id,
                    r.resolution_state,
                    r.snapshot_digest,
                    r.source_lineage_digest,
                    r.normalized_payload,
                    r.recorded_at_utc,
                    r.correlation_id
                FROM candidates.candidates AS c
                JOIN candidates.candidate_revisions AS r
                  ON r.candidate_id = c.candidate_id
                WHERE c.candidate_id = :candidate_id AND r.revision = :revision
                """
                ),
                {"candidate_id": candidate_id, "revision": revision},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ReviewNotFound(
                f"Candidate revision {candidate_id}/{revision} does not exist.",
                "Rebuild the review case from a persisted candidate revision.",
            )
        evidence = tuple(
            CandidateEvidence(
                position=evidence_row["position"],
                evidence_kind=evidence_row["evidence_kind"],
                evidence_digest=evidence_row["evidence_digest"],
            )
            for evidence_row in connection.execute(
                sa.text(
                    """
                    SELECT position, evidence_kind, evidence_digest
                    FROM candidates.candidate_revision_evidence
                    WHERE candidate_id = :candidate_id
                      AND candidate_revision = :revision
                    ORDER BY position
                    """
                ),
                {"candidate_id": candidate_id, "revision": revision},
            ).mappings()
        )
        return CandidateRevision(
            candidate_id=row["candidate_id"],
            revision=row["revision"],
            entity_kind=row["entity_kind"],
            cluster_id=row["cluster_id"],
            resolution_state=row["resolution_state"],
            snapshot_digest=row["snapshot_digest"],
            source_lineage_digest=row["source_lineage_digest"],
            normalized_payload=dict(row["normalized_payload"]),
            evidence=evidence,
            recorded_at_utc=row["recorded_at_utc"],
            correlation_id=row["correlation_id"],
        )

    def _load_quality(
        self,
        connection: sa.Connection,
        candidate_id: UUID,
        revision: int,
    ) -> QualityRecord | None:
        row = (
            connection.execute(
                sa.text(
                    """
                SELECT *
                FROM quality.quality_evaluations
                WHERE candidate_id = :candidate_id
                  AND candidate_revision = :revision
                ORDER BY evaluated_at_utc DESC, evaluation_id DESC
                LIMIT 1
                """
                ),
                {"candidate_id": candidate_id, "revision": revision},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return QualityRecord(
            evaluation_id=row["evaluation_id"],
            candidate_id=row["candidate_id"],
            candidate_revision=row["candidate_revision"],
            policy_digest=row["policy_digest"],
            export_eligible=row["export_eligible"],
            blockers=tuple(row["blockers"]),
            evaluation_digest=row["evaluation_digest"],
            evaluated_at_utc=row["evaluated_at_utc"],
            correlation_id=row["correlation_id"],
        )

    def _require_candidate_revision(
        self,
        connection: sa.Connection,
        candidate_id: UUID,
        revision: int,
        *,
        for_update: bool,
    ) -> None:
        if for_update:
            statement = sa.text(
                """
                SELECT candidate_id
                FROM candidates.candidate_revisions
                WHERE candidate_id = :candidate_id AND revision = :revision
                FOR SHARE
                """
            )
        else:
            statement = sa.text(
                """
                SELECT candidate_id
                FROM candidates.candidate_revisions
                WHERE candidate_id = :candidate_id AND revision = :revision
                """
            )
        exists = connection.execute(
            statement,
            {"candidate_id": candidate_id, "revision": revision},
        ).scalar_one_or_none()
        if exists is None:
            raise ReviewNotFound(
                f"Candidate revision {candidate_id}/{revision} does not exist.",
                "Reload candidate state before adding manual evidence.",
            )

    def _load_suppression(
        self,
        connection: sa.Connection,
        suppression_id: UUID,
        *,
        for_update: bool,
    ) -> SuppressionRevision:
        if for_update:
            statement = sa.text(
                """
                SELECT *
                FROM review.suppression_revisions
                WHERE suppression_id = :suppression_id
                ORDER BY revision DESC
                LIMIT 1
                FOR UPDATE
                """
            )
        else:
            statement = sa.text(
                """
                SELECT *
                FROM review.suppression_revisions
                WHERE suppression_id = :suppression_id
                ORDER BY revision DESC
                LIMIT 1
                """
            )
        row = (
            connection.execute(
                statement,
                {"suppression_id": suppression_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ReviewNotFound(
                f"Suppression {suppression_id} does not exist.",
                "Refresh suppression state and select an existing suppression.",
            )
        return _suppression(row)

    def _decision_replay(
        self,
        connection: sa.Connection,
        command: ReviewDecisionCommand,
    ) -> tuple[ReviewCase, ReviewDecision] | None:
        row = (
            connection.execute(
                sa.text(
                    """
                SELECT * FROM review.review_decisions
                WHERE command_digest = :command_digest
                """
                ),
                {"command_digest": command.command_digest},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        decision = _decision(row)
        _verify_decision_replay(decision, command)
        case_row = (
            connection.execute(
                sa.text(
                    """
                SELECT
                    c.case_id, c.candidate_id, c.candidate_revision, c.opened_at_utc,
                    r.revision, r.state, r.reason_codes, r.current_decision_id,
                    r.recorded_at_utc, r.correlation_id
                FROM review.review_cases AS c
                JOIN review.review_case_revisions AS r ON r.case_id = c.case_id
                WHERE c.case_id = :case_id AND r.revision = :revision
                """
                ),
                {"case_id": decision.case_id, "revision": decision.case_revision},
            )
            .mappings()
            .one()
        )
        return _case(case_row), decision

    def _suppression_replay(
        self,
        connection: sa.Connection,
        command: SuppressionCommand,
    ) -> SuppressionRevision | None:
        row = (
            connection.execute(
                sa.text(
                    """
                SELECT * FROM review.suppression_revisions
                WHERE command_digest = :command_digest
                """
                ),
                {"command_digest": command.command_digest},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        suppression = _suppression(row)
        _verify_suppression_replay(suppression, command)
        return suppression

    @staticmethod
    def _insert_case_revision(
        connection: sa.Connection,
        case: ReviewCase,
    ) -> None:
        connection.execute(
            sa.text(
                """
                INSERT INTO review.review_case_revisions (
                    case_id, revision, state, reason_codes, current_decision_id,
                    recorded_at_utc, correlation_id
                ) VALUES (
                    :case_id, :revision, :state, :reason_codes, :current_decision_id,
                    :recorded_at_utc, :correlation_id
                )
                """
            ).bindparams(sa.bindparam("reason_codes", type_=sa.ARRAY(sa.Text()))),
            {
                "case_id": case.case_id,
                "revision": case.revision,
                "state": case.state,
                "reason_codes": list(case.reason_codes),
                "current_decision_id": case.current_decision_id,
                "recorded_at_utc": case.recorded_at_utc,
                "correlation_id": case.correlation_id,
            },
        )

    @staticmethod
    def _insert_decision(
        connection: sa.Connection,
        decision: ReviewDecision,
    ) -> None:
        connection.execute(
            sa.text(
                """
                INSERT INTO review.review_decisions (
                    decision_id, case_id, case_revision, outcome, actor_id, rationale,
                    evidence_references, supersedes_decision_id, command_digest,
                    decided_at_utc, correlation_id
                ) VALUES (
                    :decision_id, :case_id, :case_revision, :outcome, :actor_id,
                    :rationale, :evidence_references, :supersedes_decision_id,
                    :command_digest, :decided_at_utc, :correlation_id
                )
                """
            ).bindparams(sa.bindparam("evidence_references", type_=sa.ARRAY(sa.Text()))),
            {
                **decision.model_dump(),
                "evidence_references": list(decision.evidence_references),
            },
        )

    @staticmethod
    def _insert_suppression(
        connection: sa.Connection,
        suppression: SuppressionRevision,
    ) -> None:
        scopes = set(suppression.scopes)
        connection.execute(
            sa.text(
                """
                INSERT INTO review.suppression_revisions (
                    suppression_id, revision, state, target_kind, target_id,
                    suppress_discovery, suppress_normalization, suppress_export,
                    reason_code, actor_id, evidence_reference, starts_at_utc,
                    expires_at_utc, resolved_at_utc, command_digest, correlation_id
                ) VALUES (
                    :suppression_id, :revision, :state, :target_kind, :target_id,
                    :suppress_discovery, :suppress_normalization, :suppress_export,
                    :reason_code, :actor_id, :evidence_reference, :starts_at_utc,
                    :expires_at_utc, :resolved_at_utc, :command_digest, :correlation_id
                )
                """
            ),
            {
                "suppression_id": suppression.suppression_id,
                "revision": suppression.revision,
                "state": suppression.state,
                "target_kind": suppression.target_kind,
                "target_id": suppression.target_id,
                "suppress_discovery": "discovery" in scopes,
                "suppress_normalization": "normalization" in scopes,
                "suppress_export": "export" in scopes,
                "reason_code": suppression.reason_code,
                "actor_id": suppression.actor_id,
                "evidence_reference": suppression.evidence_reference,
                "starts_at_utc": suppression.starts_at_utc,
                "expires_at_utc": suppression.expires_at_utc,
                "resolved_at_utc": suppression.resolved_at_utc,
                "command_digest": suppression.command_digest,
                "correlation_id": suppression.correlation_id,
            },
        )


def _summary(row: RowMapping) -> ReviewCaseSummary:
    return ReviewCaseSummary(
        case_id=row["case_id"],
        candidate_id=row["candidate_id"],
        candidate_revision=row["candidate_revision"],
        revision=row["revision"],
        state=row["state"],
        reason_codes=tuple(row["reason_codes"]),
        current_decision_id=row["current_decision_id"],
        recorded_at_utc=row["recorded_at_utc"],
    )


def _case(row: RowMapping) -> ReviewCase:
    return ReviewCase(
        case_id=row["case_id"],
        candidate_id=row["candidate_id"],
        candidate_revision=row["candidate_revision"],
        revision=row["revision"],
        state=row["state"],
        reason_codes=tuple(row["reason_codes"]),
        current_decision_id=row["current_decision_id"],
        opened_at_utc=row["opened_at_utc"],
        recorded_at_utc=row["recorded_at_utc"],
        correlation_id=row["correlation_id"],
    )


def _decision(row: RowMapping) -> ReviewDecision:
    return ReviewDecision(
        decision_id=row["decision_id"],
        case_id=row["case_id"],
        case_revision=row["case_revision"],
        outcome=row["outcome"],
        actor_id=row["actor_id"],
        rationale=row["rationale"],
        evidence_references=tuple(row["evidence_references"]),
        supersedes_decision_id=row["supersedes_decision_id"],
        command_digest=row["command_digest"],
        decided_at_utc=row["decided_at_utc"],
        correlation_id=row["correlation_id"],
    )


def _observation(row: RowMapping) -> ManualObservation:
    return ManualObservation(
        observation_id=row["observation_id"],
        candidate_id=row["candidate_id"],
        candidate_revision=row["candidate_revision"],
        field_key=row["field_key"],
        value_text=row["value_text"],
        value_digest=row["value_digest"],
        actor_id=row["actor_id"],
        reason_code=row["reason_code"],
        supersedes_observation_id=row["supersedes_observation_id"],
        command_digest=row["command_digest"],
        recorded_at_utc=row["recorded_at_utc"],
        correlation_id=row["correlation_id"],
    )


def _suppression(row: RowMapping) -> SuppressionRevision:
    scopes: list[SuppressionScope] = []
    if row["suppress_discovery"]:
        scopes.append("discovery")
    if row["suppress_export"]:
        scopes.append("export")
    if row["suppress_normalization"]:
        scopes.append("normalization")
    return SuppressionRevision(
        suppression_id=row["suppression_id"],
        revision=row["revision"],
        state=row["state"],
        target_kind=row["target_kind"],
        target_id=row["target_id"],
        scopes=tuple(scopes),
        reason_code=row["reason_code"],
        actor_id=row["actor_id"],
        evidence_reference=row["evidence_reference"],
        starts_at_utc=row["starts_at_utc"],
        expires_at_utc=row["expires_at_utc"],
        resolved_at_utc=row["resolved_at_utc"],
        command_digest=row["command_digest"],
        correlation_id=row["correlation_id"],
    )


def _verify_decision_replay(
    decision: ReviewDecision,
    command: ReviewDecisionCommand,
) -> None:
    if (
        decision.case_id != command.case_id
        or decision.case_revision != command.expected_case_revision + 1
        or decision.outcome != command.outcome
        or decision.actor_id != command.actor_id
        or decision.rationale != command.rationale
        or decision.evidence_references != command.evidence_references
        or decision.supersedes_decision_id != command.supersedes_decision_id
    ):
        raise ReviewConflict(
            "A review command digest is already bound to different immutable content.",
            "Investigate the command identity collision before retrying.",
        )


def _verify_observation_replay(
    observation: ManualObservation,
    command: ManualObservationCommand,
) -> None:
    if (
        observation.candidate_id != command.candidate_id
        or observation.candidate_revision != command.candidate_revision
        or observation.field_key != command.field_key
        or observation.value_text != command.value_text
        or observation.actor_id != command.actor_id
        or observation.reason_code != command.reason_code
        or observation.supersedes_observation_id != command.supersedes_observation_id
    ):
        raise ReviewConflict(
            "A manual-observation command digest is bound to different immutable content.",
            "Investigate the command identity collision before retrying.",
        )


def _verify_suppression_replay(
    suppression: SuppressionRevision,
    command: SuppressionCommand,
) -> None:
    expected_revision = 0 if command.expected_revision is None else command.expected_revision + 1
    if (
        suppression.revision != expected_revision
        or suppression.target_kind != command.target_kind
        or suppression.target_id != command.target_id
        or suppression.scopes != command.scopes
        or suppression.reason_code != command.reason_code
        or suppression.actor_id != command.actor_id
        or suppression.evidence_reference != command.evidence_reference
        or suppression.expires_at_utc != command.expires_at_utc
    ):
        raise ReviewConflict(
            "A suppression command digest is bound to different immutable content.",
            "Investigate the command identity collision before retrying.",
        )
