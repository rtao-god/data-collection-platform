"""Read-only projection of operator pair decisions into match-relevant evidence.

Kept apart from the command owner so the resolution scheduler and the snapshot applier can read
persisted decisions without depending on the writer, and so no reader ever mutates decision
history.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping

from collection_application.canonicalization import sha256_json
from collection_contracts import owner_error
from collection_infrastructure.resolution import (
    ResolutionAssemblyError,
    ResolutionCandidateSource,
    candidate_from_persisted,
)
from collection_infrastructure.resolution.context import pair_context_digest
from resolution_contracts import (
    ManualDecisionAction,
    ManualResolutionDecision,
    ManualResolutionDecisionPayload,
    ResolutionCandidate,
    seal_manual_decision,
)

_ACTION_TO_CONTRACT = {
    "match": ManualDecisionAction.MATCH,
    "separate": ManualDecisionAction.SEPARATE,
}


@dataclass(frozen=True, slots=True)
class PairDecisionRecord:
    """One persisted decision revision as the operator sees it."""

    decision_id: UUID
    pair_id: str
    run_id: UUID
    left_candidate_id: UUID
    left_candidate_revision: int
    right_candidate_id: UUID
    right_candidate_revision: int
    action: str
    reason_code: str
    rationale: str
    evidence_references: tuple[str, ...]
    context_digest: str
    decision_digest: str
    revision: int
    actor_id: str
    supersedes_decision_id: UUID | None
    decided_at_utc: datetime
    correlation_id: str


def manual_decisions_for_revisions(
    connection: Connection,
    revisions: Sequence[ResolutionCandidateSource],
) -> tuple[ManualResolutionDecision, ...]:
    """The active manual decisions that still describe this exact candidate set.

    Only the newest decision revision of a pair counts. A decision is included only when its
    recorded context digest equals the context of the current facts, so a decision can never be
    applied to evidence the operator did not judge.
    """

    if len(revisions) < 2:
        return ()
    return _decisions_for_candidates(
        connection,
        {
            item.candidate_id: candidate_from_persisted(
                item.normalized_payload,
                candidate_id=item.candidate_id,
                entity_kind=item.entity_kind,
                revision=item.revision,
            )
            for item in revisions
        },
    )


def decisions_for_persisted_revisions(
    connection: Connection,
    revisions: Mapping[UUID, int],
) -> tuple[ManualResolutionDecision, ...]:
    """The active manual decisions for an already-persisted candidate revision set."""

    if len(revisions) < 2:
        return ()
    return _decisions_for_candidates(connection, persisted_candidates(connection, revisions))


def decision_basis_digest(decisions: Sequence[ManualResolutionDecision]) -> str:
    """The canonical identity of the decision set a resolution was derived from.

    A resolution is only authoritative for the decisions it actually reflected, so the digest of
    that exact set is what an export compares against the run's current decisions.
    """

    return sha256_json(sorted(item.decision_digest for item in decisions))


def recorded_decision_basis(connection: Connection, run_id: UUID) -> str | None:
    """The decision basis the run's current resolution was applied from."""

    row = connection.execute(
        sa.text(
            "SELECT decision_basis_digest FROM candidates.resolution_runs AS r"
            " WHERE r.run_id = :run_id AND NOT EXISTS ("
            "  SELECT 1 FROM candidates.resolution_runs AS s"
            "  WHERE s.supersedes_resolution_id = r.resolution_id)"
        ),
        {"run_id": run_id},
    ).one_or_none()
    return None if row is None else str(row[0])


def current_decision_basis(connection: Connection, run_id: UUID) -> str | None:
    """The decision basis the run's current resolution must reflect right now."""

    resolution_id = current_resolution_id(connection, run_id)
    if resolution_id is None:
        return None
    revisions = resolution_revisions(connection, resolution_id)
    return decision_basis_digest(decisions_for_persisted_revisions(connection, revisions))


def _decisions_for_candidates(
    connection: Connection,
    candidates: Mapping[UUID, ResolutionCandidate],
) -> tuple[ManualResolutionDecision, ...]:
    decisions = latest_pair_decisions(connection, tuple(candidates))
    selected: list[ManualResolutionDecision] = []
    for record in decisions:
        left = candidates.get(record.left_candidate_id)
        right = candidates.get(record.right_candidate_id)
        if left is None or right is None:
            continue
        if pair_context_digest(left, right) != record.context_digest:
            continue
        selected.append(
            seal_manual_decision(
                ManualResolutionDecisionPayload(
                    decision_id=record.decision_id,
                    left_candidate_id=record.left_candidate_id,
                    right_candidate_id=record.right_candidate_id,
                    action=_ACTION_TO_CONTRACT[record.action],
                    revision=record.revision,
                    actor_reference=record.actor_id,
                    reason_code=record.reason_code,
                )
            )
        )
    return tuple(selected)


def latest_pair_decisions(
    connection: Connection,
    candidate_ids: Sequence[UUID],
) -> tuple[PairDecisionRecord, ...]:
    if not candidate_ids:
        return ()
    rows = (
        connection.execute(
            sa.text(
                "SELECT DISTINCT ON (pair_id) decision_id, pair_id, run_id,"
                " left_candidate_id, left_candidate_revision, right_candidate_id,"
                " right_candidate_revision, action, reason_code, rationale,"
                " evidence_references, context_digest, decision_digest, revision, actor_id,"
                " supersedes_decision_id, decided_at_utc, correlation_id"
                " FROM candidates.resolution_pair_decisions"
                " WHERE left_candidate_id = ANY(:candidate_ids)"
                "   AND right_candidate_id = ANY(:candidate_ids)"
                " ORDER BY pair_id, revision DESC"
            ).bindparams(
                sa.bindparam(
                    "candidate_ids",
                    type_=sa.ARRAY(postgresql.UUID(as_uuid=True)),
                )
            ),
            {"candidate_ids": list(candidate_ids)},
        )
        .mappings()
        .all()
    )
    return tuple(pair_decision_record(row) for row in rows)


def pair_decision_record(row: RowMapping) -> PairDecisionRecord:
    supersedes = row["supersedes_decision_id"]
    return PairDecisionRecord(
        decision_id=UUID(str(row["decision_id"])),
        pair_id=str(row["pair_id"]),
        run_id=UUID(str(row["run_id"])),
        left_candidate_id=UUID(str(row["left_candidate_id"])),
        left_candidate_revision=int(str(row["left_candidate_revision"])),
        right_candidate_id=UUID(str(row["right_candidate_id"])),
        right_candidate_revision=int(str(row["right_candidate_revision"])),
        action=str(row["action"]),
        reason_code=str(row["reason_code"]),
        rationale=str(row["rationale"]),
        evidence_references=tuple(str(item) for item in row["evidence_references"]),
        context_digest=str(row["context_digest"]),
        decision_digest=str(row["decision_digest"]),
        revision=int(str(row["revision"])),
        actor_id=str(row["actor_id"]),
        supersedes_decision_id=None if supersedes is None else UUID(str(supersedes)),
        decided_at_utc=row["decided_at_utc"],
        correlation_id=str(row["correlation_id"]),
    )


def current_resolution_id(connection: Connection, run_id: UUID) -> UUID | None:
    rows = (
        connection.execute(
            sa.text(
                "SELECT resolution_id FROM candidates.resolution_runs AS r"
                " WHERE r.run_id = :run_id AND NOT EXISTS ("
                "  SELECT 1 FROM candidates.resolution_runs AS s"
                "  WHERE s.supersedes_resolution_id = r.resolution_id)"
            ),
            {"run_id": run_id},
        )
        .scalars()
        .all()
    )
    if len(rows) > 1:
        raise owner_error(
            error_type="collection/resolution-pair-context-invalid",
            owner="ResolutionPairDecisions",
            code="PAIR_DECISION_RESOLUTION_AMBIGUOUS",
            message="The run has more than one current resolution.",
            context={"runId": str(run_id)},
            required_action="Repair the resolution supersession chain under its owner.",
            correlation_id=f"pair-context:{run_id}",
        )
    return None if not rows else UUID(str(rows[0]))


def resolution_revisions(connection: Connection, resolution_id: UUID) -> dict[UUID, int]:
    """The exact candidate revisions one resolution evaluated."""

    rows = connection.execute(
        sa.text(
            "SELECT candidate_id, candidate_revision FROM quality.quality_evaluations"
            " WHERE resolution_id = :resolution_id"
        ),
        {"resolution_id": resolution_id},
    ).all()
    return {UUID(str(row[0])): int(row[1]) for row in rows}


def persisted_candidates(
    connection: Connection,
    revisions: Mapping[UUID, int],
) -> dict[UUID, ResolutionCandidate]:
    candidates: dict[UUID, ResolutionCandidate] = {}
    for candidate_id, revision in revisions.items():
        row = (
            connection.execute(
                sa.text(
                    "SELECT r.normalized_payload, c.entity_kind"
                    " FROM candidates.candidate_revisions AS r"
                    " JOIN candidates.candidates AS c USING (candidate_id)"
                    " WHERE r.candidate_id = :candidate_id AND r.revision = :revision"
                ),
                {"candidate_id": candidate_id, "revision": revision},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            continue
        try:
            candidates[candidate_id] = candidate_from_persisted(
                row["normalized_payload"],
                candidate_id=candidate_id,
                entity_kind=str(row["entity_kind"]),
                revision=revision,
            )
        except ResolutionAssemblyError as exc:
            raise owner_error(
                error_type="collection/resolution-pair-context-invalid",
                owner="ResolutionPairDecisions",
                code=exc.code,
                message=str(exc) or "A persisted candidate revision cannot be projected.",
                context={"candidateId": str(candidate_id), **exc.context},
                required_action="Re-materialize the candidate revision under its profile.",
                correlation_id=f"pair-context:{candidate_id}",
            ) from exc
    return candidates
