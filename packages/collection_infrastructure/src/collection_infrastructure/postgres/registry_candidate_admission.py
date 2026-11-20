"""Admit a verified registry candidate as an immutable collector candidate revision.

This is the second entry into the collector's candidate storage, next to source-record intake.
It deliberately reuses the intake applier's own persistence primitives so both paths keep the
same invariants (one row lock per candidate identity, append-only revisions, unique positions,
an explicit open review case). Nothing here decides quality: the review case stays open.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from collection_application.pipeline_advancement import PipelineAdvancementConflict
from collection_contracts.candidate import CandidateRevision, candidate_snapshot_digest
from collection_infrastructure.postgres.candidate_intake_applier import (
    PostgresCandidateIntakeApplier,
)

ENTITY_KIND = "place"
REVIEW_REASON = "registry_import_pending_review"


@dataclass(frozen=True)
class RegistryCandidateAdmission:
    candidate_id: str
    revision: int
    replayed: bool


def _conflict(code: str, message: str, context: dict[str, Any]) -> PipelineAdvancementConflict:
    return PipelineAdvancementConflict(
        code=code,
        message=message,
        context=context,
        required_action="Investigate the diverging registry candidate at its owner.",
    )


def _snapshot_for(revision: CandidateRevision, number: int) -> str:
    return candidate_snapshot_digest(
        candidate_id=revision.candidate_id,
        revision=number,
        entity_kind=revision.entity_kind,
        cluster_id=revision.cluster_id,
        resolution_state=revision.resolution_state,
        normalized_payload=revision.normalized_payload,
        evidence=revision.evidence,
        source_lineage_digest=revision.source_lineage_digest,
    )


def _admit(
    connection: Connection,
    revision: CandidateRevision,
    *,
    now_utc: datetime,
    correlation_id: str,
) -> RegistryCandidateAdmission:
    applier = PostgresCandidateIntakeApplier
    applier._insert_candidate(
        connection,
        entity_kind=type("_Kind", (), {"value": ENTITY_KIND})(),
        now_utc=now_utc,
        correlation_id=correlation_id,
        action=type("_Action", (), {"candidate_id": revision.candidate_id})(),
    )
    existing = connection.execute(
        sa.text(
            "SELECT revision, snapshot_digest, source_lineage_digest, normalized_payload "
            "FROM candidates.candidate_revisions WHERE candidate_id = :candidate_id "
            "ORDER BY revision FOR UPDATE"
        ),
        {"candidate_id": revision.candidate_id},
    ).fetchall()
    payload = json.dumps(revision.normalized_payload, ensure_ascii=False, sort_keys=True)
    for row in existing:
        if str(row[2]) != revision.source_lineage_digest:
            continue
        stored_payload = row[3] if isinstance(row[3], str) else json.dumps(row[3], sort_keys=True)
        if json.loads(stored_payload) != json.loads(payload):
            raise _conflict(
                "REGISTRY_CANDIDATE_REVISION_CONFLICT",
                "The same evidence lineage produced a different candidate revision.",
                {"candidateId": str(revision.candidate_id), "revision": int(row[0])},
            )
        if str(row[1]) != _snapshot_for(revision, int(row[0])):
            raise _conflict(
                "REGISTRY_CANDIDATE_REVISION_CONFLICT",
                "The stored revision digest differs from the exact recomputation.",
                {"candidateId": str(revision.candidate_id), "revision": int(row[0])},
            )
        return RegistryCandidateAdmission(str(revision.candidate_id), int(row[0]), True)

    number = max((int(row[0]) for row in existing), default=-1) + 1
    stored = revision.model_copy(update={"revision": number, "snapshot_digest": _snapshot_for(revision, number)})
    applier._insert_revision(connection, revision=stored)
    applier._insert_evidence(connection, revision=stored)
    applier._insert_review_case(
        connection,
        revision=stored,
        now_utc=now_utc,
        correlation_id=correlation_id,
        extra_reason_codes=(REVIEW_REASON,),
    )
    return RegistryCandidateAdmission(str(revision.candidate_id), number, False)


def admit_registry_candidate(
    engine: Engine,
    revision: CandidateRevision,
    *,
    now_utc: datetime | None = None,
    correlation_id: str = "registry-candidate-admission",
) -> RegistryCandidateAdmission:
    """One transaction: candidate identity, revision, evidence, open review case."""
    with engine.begin() as connection:
        return _admit(
            connection,
            revision,
            now_utc=now_utc or datetime.now(UTC),
            correlation_id=correlation_id,
        )
