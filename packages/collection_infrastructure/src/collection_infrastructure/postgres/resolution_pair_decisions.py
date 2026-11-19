"""Durable owner of operator resolution-pair adjudication.

Resolution accepted sealed manual decisions but nothing persisted them, so an operator could not
record a judgement and no later resolution could reuse one. This module owns that state: one
authenticated command in, an append-only decision revision out, plus the deterministic
resolution it triggers. It is not a second resolver and it never decides a pair itself.

Decisions are bound to the canonical pair identity and to the match-relevant context digest of
the exact candidate revisions the operator judged. A decision is reused by a later resolution
only while that context still holds, so a re-fetch of unchanged facts keeps the judgement and a
changed name, kind, contact detail, website, address or geography returns the pair to review.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from collection_contracts import canonical_digest
from collection_infrastructure.resolution.context import pair_context_digest
from collection_infrastructure.resolution.decisions import (
    PairDecisionRecord,
    current_resolution_id,
    latest_pair_decisions,
    pair_decision_record,
    persisted_candidates,
    resolution_revisions,
)
from resolution_contracts import (
    ManualDecisionAction,
    ManualResolutionDecisionPayload,
    ResolutionCandidate,
    candidate_pair_id,
    seal_manual_decision,
)

_ACTION_TO_CONTRACT = {
    "match": ManualDecisionAction.MATCH,
    "separate": ManualDecisionAction.SEPARATE,
}
_COMMAND_ACTIONS = frozenset(_ACTION_TO_CONTRACT)


class PairDecisionScheduler(Protocol):
    """The resolution owner's entry point for re-deriving after a decision changed."""

    def schedule_after_pair_decision(
        self,
        connection: Connection,
        *,
        run_id: UUID,
        correlation_id: str,
    ) -> UUID | None: ...


class PairDecisionConflict(RuntimeError):
    """The pair decision command cannot be applied to the current owner state."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        required_action: str,
        context: Mapping[str, object] | None = None,
        status_code: int = 409,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.required_action = required_action
        self.context = dict(context or {})
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PairDecisionRequest:
    """One operator judgement inside one command batch."""

    pair_id: str
    left_candidate_id: UUID
    left_candidate_revision: int
    right_candidate_id: UUID
    right_candidate_revision: int
    action: str
    reason_code: str
    rationale: str
    evidence_references: tuple[str, ...]
    expected_decision_revision: int


@dataclass(frozen=True, slots=True)
class PendingPairView:
    """One pair of the run's current resolution and the decision history behind it."""

    pair_id: str
    left_candidate_id: UUID
    left_candidate_revision: int
    right_candidate_id: UUID
    right_candidate_revision: int
    disposition: str
    reason_codes: tuple[str, ...]
    decision: PairDecisionRecord | None
    decision_context_current: bool


class PostgresResolutionPairDecisionRepository:
    """Reads and appends operator pair decisions, then re-derives resolution."""

    def __init__(
        self,
        engine: Engine,
        *,
        scheduler: PairDecisionScheduler,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._scheduler = scheduler
        self._clock = clock or (lambda: datetime.now(UTC))

    def list_pending_pairs(self, run_id: UUID) -> tuple[PendingPairView, ...]:
        with self._engine.connect() as connection:
            resolution_id = current_resolution_id(connection, run_id)
            if resolution_id is None:
                raise PairDecisionConflict(
                    code="PAIR_DECISION_RESOLUTION_MISSING",
                    message="The run has no current resolution, so it has no pairs to decide.",
                    required_action="Apply the run's resolution before reviewing its pairs.",
                    context={"runId": str(run_id)},
                    status_code=409,
                )
            revisions = resolution_revisions(connection, resolution_id)
            pair_rows = connection.execute(
                sa.text(
                    "SELECT pair_id, left_candidate_id, right_candidate_id, disposition,"
                    " reason_codes FROM candidates.resolution_pairs"
                    " WHERE resolution_id = :resolution_id"
                    " ORDER BY pair_id"
                ),
                {"resolution_id": resolution_id},
            ).all()
            latest = {
                record.pair_id: record
                for record in latest_pair_decisions(
                    connection,
                    tuple(
                        {UUID(str(row[1])) for row in pair_rows}.union(
                            {UUID(str(row[2])) for row in pair_rows}
                        )
                    ),
                )
            }
            candidates = persisted_candidates(connection, revisions)
            views: list[PendingPairView] = []
            for row in pair_rows:
                pair_id = str(row[0])
                left_id = UUID(str(row[1]))
                right_id = UUID(str(row[2]))
                left_revision = revisions.get(left_id)
                right_revision = revisions.get(right_id)
                if left_revision is None or right_revision is None:
                    raise PairDecisionConflict(
                        code="PAIR_DECISION_REVISION_UNRESOLVED",
                        message="A resolved pair member has no recorded candidate revision.",
                        required_action=(
                            "Re-apply the run's resolution from its persisted candidate revisions."
                        ),
                        context={"runId": str(run_id), "pairId": pair_id},
                    )
                decision = latest.get(pair_id)
                context_current = False
                if decision is not None:
                    left = candidates.get(left_id)
                    right = candidates.get(right_id)
                    context_current = (
                        left is not None
                        and right is not None
                        and pair_context_digest(left, right) == decision.context_digest
                    )
                views.append(
                    PendingPairView(
                        pair_id=pair_id,
                        left_candidate_id=left_id,
                        left_candidate_revision=left_revision,
                        right_candidate_id=right_id,
                        right_candidate_revision=right_revision,
                        disposition=str(row[3]),
                        reason_codes=tuple(str(code) for code in row[4]),
                        decision=decision,
                        decision_context_current=context_current,
                    )
                )
            return tuple(views)

    def submit_decisions(
        self,
        *,
        run_id: UUID,
        actor_id: str,
        idempotency_key: str,
        requests: Sequence[PairDecisionRequest],
        correlation_id: str,
    ) -> tuple[PairDecisionRecord, ...]:
        """Apply one complete decision batch or nothing at all."""

        if not requests:
            raise PairDecisionConflict(
                code="PAIR_DECISION_COMMAND_EMPTY",
                message="A pair decision command requires at least one decision.",
                required_action="Submit at least one pair decision.",
                status_code=422,
            )
        ordered = tuple(sorted(requests, key=lambda item: item.pair_id))
        if len({item.pair_id for item in ordered}) != len(ordered):
            raise PairDecisionConflict(
                code="PAIR_DECISION_COMMAND_DUPLICATE_PAIR",
                message="A pair decision command addresses each pair at most once.",
                required_action="Submit one decision per pair.",
                status_code=422,
            )
        command_digest = _command_digest(
            run_id=run_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            requests=ordered,
        )
        now_utc = self._now_utc()
        with self._engine.begin() as connection:
            replay = _replayed_command(
                connection,
                idempotency_key=idempotency_key,
                command_digest=command_digest,
                run_id=run_id,
            )
            if replay is not None:
                return _command_decisions(connection, replay)
            _require_run(connection, run_id)
            resolution_id = current_resolution_id(connection, run_id)
            if resolution_id is None:
                raise PairDecisionConflict(
                    code="PAIR_DECISION_RESOLUTION_MISSING",
                    message="The run has no current resolution, so it has no pairs to decide.",
                    required_action="Apply the run's resolution before deciding its pairs.",
                    context={"runId": str(run_id)},
                )
            revisions = resolution_revisions(connection, resolution_id)
            candidates = persisted_candidates(connection, revisions)
            command_id = uuid4()
            connection.execute(
                sa.text(
                    "INSERT INTO candidates.resolution_pair_decision_commands ("
                    "command_id, idempotency_key, command_digest, run_id, actor_id,"
                    " decision_count, recorded_at_utc, correlation_id"
                    ") VALUES (:command_id, :idempotency_key, :command_digest, :run_id,"
                    " :actor_id, :decision_count, :recorded_at_utc, :correlation_id)"
                ),
                {
                    "command_id": command_id,
                    "idempotency_key": idempotency_key,
                    "command_digest": command_digest,
                    "run_id": run_id,
                    "actor_id": actor_id,
                    "decision_count": len(ordered),
                    "recorded_at_utc": now_utc,
                    "correlation_id": correlation_id,
                },
            )
            for request in ordered:
                self._append_decision(
                    connection,
                    command_id=command_id,
                    run_id=run_id,
                    resolution_id=resolution_id,
                    revisions=revisions,
                    candidates=candidates,
                    request=request,
                    actor_id=actor_id,
                    now_utc=now_utc,
                    correlation_id=correlation_id,
                )
            self._scheduler.schedule_after_pair_decision(
                connection,
                run_id=run_id,
                correlation_id=correlation_id,
            )
            return _command_decisions(connection, command_id)

    def _append_decision(
        self,
        connection: Connection,
        *,
        command_id: UUID,
        run_id: UUID,
        resolution_id: UUID,
        revisions: Mapping[UUID, int],
        candidates: Mapping[UUID, ResolutionCandidate],
        request: PairDecisionRequest,
        actor_id: str,
        now_utc: datetime,
        correlation_id: str,
    ) -> None:
        left_id, right_id = _canonical_pair(request)
        expected_pair_id = candidate_pair_id(left_id, right_id)
        if expected_pair_id != request.pair_id:
            raise PairDecisionConflict(
                code="PAIR_DECISION_PAIR_IDENTITY_INVALID",
                message="The declared pair identity does not match its candidate identities.",
                required_action="Read the pair from the run's current resolution and retry.",
                context={
                    "pairId": request.pair_id,
                    "expectedPairId": expected_pair_id,
                },
                status_code=422,
            )
        if (
            revisions.get(left_id) != request.left_candidate_revision
            or revisions.get(right_id) != request.right_candidate_revision
        ):
            raise PairDecisionConflict(
                code="PAIR_DECISION_CANDIDATE_REVISION_STALE",
                message="The pair decision names candidate revisions this resolution does not use.",
                required_action="Re-read the run's pending pairs and decide the current revisions.",
                context={
                    "pairId": request.pair_id,
                    "recordedRevisions": {
                        str(left_id): revisions.get(left_id),
                        str(right_id): revisions.get(right_id),
                    },
                },
            )
        if request.action not in _COMMAND_ACTIONS:
            raise PairDecisionConflict(
                code="PAIR_DECISION_ACTION_INVALID",
                message="A pair decision must be MATCH or SEPARATE.",
                required_action="Submit action match or separate.",
                status_code=422,
            )
        if not request.evidence_references:
            raise PairDecisionConflict(
                code="PAIR_DECISION_EVIDENCE_MISSING",
                message="A pair decision requires at least one evidence reference.",
                required_action="Reference the evidence the judgement rests on.",
                status_code=422,
            )
        known_pair = connection.execute(
            sa.text(
                "SELECT 1 FROM candidates.resolution_pairs"
                " WHERE resolution_id = :resolution_id AND pair_id = :pair_id"
            ),
            {"resolution_id": resolution_id, "pair_id": request.pair_id},
        ).scalar_one_or_none()
        if known_pair is None:
            raise PairDecisionConflict(
                code="PAIR_DECISION_PAIR_UNKNOWN",
                message="The pair does not belong to this run's current resolution.",
                required_action="Decide a pair the run's current resolution actually reported.",
                context={"runId": str(run_id), "pairId": request.pair_id},
            )
        active = connection.execute(
            sa.text(
                "SELECT decision_id, revision FROM candidates.resolution_pair_decisions"
                " WHERE pair_id = :pair_id ORDER BY revision DESC LIMIT 1"
            ),
            {"pair_id": request.pair_id},
        ).one_or_none()
        active_revision = 0 if active is None else int(active[1]) + 1
        if active_revision != request.expected_decision_revision:
            raise PairDecisionConflict(
                code="PAIR_DECISION_REVISION_CONFLICT",
                message="The pair has a different decision revision than the one expected.",
                required_action="Re-read the pair's current decision and decide it again.",
                context={
                    "pairId": request.pair_id,
                    "expectedDecisionRevision": request.expected_decision_revision,
                    "currentDecisionRevision": active_revision,
                },
            )
        left = candidates.get(left_id)
        right = candidates.get(right_id)
        if left is None or right is None:
            raise PairDecisionConflict(
                code="PAIR_DECISION_CANDIDATE_MISSING",
                message="A pair member has no persisted candidate revision.",
                required_action="Re-apply the run's resolution from its persisted revisions.",
                context={"pairId": request.pair_id},
            )
        context_digest = pair_context_digest(left, right)
        decision_id = uuid4()
        sealed = seal_manual_decision(
            ManualResolutionDecisionPayload(
                decision_id=decision_id,
                left_candidate_id=left_id,
                right_candidate_id=right_id,
                action=_ACTION_TO_CONTRACT[request.action],
                revision=request.expected_decision_revision,
                actor_reference=actor_id,
                reason_code=request.reason_code,
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO candidates.resolution_pair_decisions ("
                "decision_id, command_id, run_id, pair_id, left_candidate_id,"
                " right_candidate_id, left_candidate_revision, right_candidate_revision,"
                " action, reason_code, rationale, evidence_references, context_digest,"
                " decision_digest, revision, actor_id, supersedes_decision_id,"
                " decided_at_utc, correlation_id"
                ") VALUES ("
                ":decision_id, :command_id, :run_id, :pair_id, :left_candidate_id,"
                " :right_candidate_id, :left_candidate_revision, :right_candidate_revision,"
                " :action, :reason_code, :rationale, :evidence_references, :context_digest,"
                " :decision_digest, :revision, :actor_id, :supersedes_decision_id,"
                " :decided_at_utc, :correlation_id)"
            ).bindparams(sa.bindparam("evidence_references", type_=sa.ARRAY(sa.Text()))),
            {
                "decision_id": decision_id,
                "command_id": command_id,
                "run_id": run_id,
                "pair_id": request.pair_id,
                "left_candidate_id": left_id,
                "right_candidate_id": right_id,
                "left_candidate_revision": request.left_candidate_revision,
                "right_candidate_revision": request.right_candidate_revision,
                "action": request.action,
                "reason_code": request.reason_code,
                "rationale": request.rationale,
                "evidence_references": list(request.evidence_references),
                "context_digest": context_digest,
                "decision_digest": sealed.decision_digest,
                "revision": request.expected_decision_revision,
                "actor_id": actor_id,
                "supersedes_decision_id": None if active is None else UUID(str(active[0])),
                "decided_at_utc": now_utc,
                "correlation_id": correlation_id,
            },
        )

    def _now_utc(self) -> datetime:
        return self._clock().astimezone(UTC)


def _canonical_pair(request: PairDecisionRequest) -> tuple[UUID, UUID]:
    return tuple(  # type: ignore[return-value]
        sorted(
            (request.left_candidate_id, request.right_candidate_id),
            key=lambda value: value.hex,
        )
    )


def _command_digest(
    *,
    run_id: UUID,
    actor_id: str,
    idempotency_key: str,
    requests: Sequence[PairDecisionRequest],
) -> str:
    return canonical_digest(
        {
            "actorId": actor_id,
            "decisions": [
                {
                    "action": item.action,
                    "evidenceReferences": list(item.evidence_references),
                    "expectedDecisionRevision": item.expected_decision_revision,
                    "leftCandidateId": str(item.left_candidate_id),
                    "leftCandidateRevision": item.left_candidate_revision,
                    "pairId": item.pair_id,
                    "rationale": item.rationale,
                    "reasonCode": item.reason_code,
                    "rightCandidateId": str(item.right_candidate_id),
                    "rightCandidateRevision": item.right_candidate_revision,
                }
                for item in requests
            ],
            "idempotencyKey": idempotency_key,
            "runId": str(run_id),
        }
    )


def _replayed_command(
    connection: Connection,
    *,
    idempotency_key: str,
    command_digest: str,
    run_id: UUID,
) -> UUID | None:
    row = (
        connection.execute(
            sa.text(
                "SELECT command_id, command_digest, run_id"
                " FROM candidates.resolution_pair_decision_commands"
                " WHERE idempotency_key = :idempotency_key"
                " FOR UPDATE"
            ),
            {"idempotency_key": idempotency_key},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if str(row["command_digest"]) != command_digest or UUID(str(row["run_id"])) != run_id:
        raise PairDecisionConflict(
            code="PAIR_DECISION_IDEMPOTENCY_CONFLICT",
            message="The idempotency key was already used for a different decision command.",
            required_action="Reuse the key only for the identical command.",
            context={"idempotencyKey": idempotency_key},
        )
    return UUID(str(row["command_id"]))


def _command_decisions(
    connection: Connection,
    command_id: UUID,
) -> tuple[PairDecisionRecord, ...]:
    rows = (
        connection.execute(
            sa.text(
                "SELECT decision_id, pair_id, run_id, left_candidate_id,"
                " left_candidate_revision, right_candidate_id, right_candidate_revision,"
                " action, reason_code, rationale, evidence_references, context_digest,"
                " decision_digest, revision, actor_id, supersedes_decision_id, decided_at_utc,"
                " correlation_id FROM candidates.resolution_pair_decisions"
                " WHERE command_id = :command_id ORDER BY pair_id"
            ),
            {"command_id": command_id},
        )
        .mappings()
        .all()
    )
    return tuple(pair_decision_record(row) for row in rows)


def _require_run(connection: Connection, run_id: UUID) -> None:
    exists = connection.execute(
        sa.text("SELECT 1 FROM runs.collection_runs WHERE run_id = :run_id"),
        {"run_id": run_id},
    ).scalar_one_or_none()
    if exists is None:
        raise PairDecisionConflict(
            code="PAIR_DECISION_RUN_NOT_FOUND",
            message="The pair decision names a run this owner does not have.",
            required_action="Use a run that exists and has a current resolution.",
            context={"runId": str(run_id)},
            status_code=404,
        )
