from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from collection_contracts.canonical import canonical_digest

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Key = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")]
CandidateResolutionState = Literal["resolved", "review", "blocked"]


class CandidateContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateEvidence(CandidateContract):
    position: Annotated[int, Field(ge=0)]
    evidence_kind: Literal["source_observation", "manual_observation", "artifact"]
    evidence_digest: Digest


class CandidateRevision(CandidateContract):
    candidate_id: UUID
    revision: Annotated[int, Field(ge=0)]
    entity_kind: Key
    cluster_id: UUID
    resolution_state: CandidateResolutionState
    snapshot_digest: Digest
    source_lineage_digest: Digest
    normalized_payload: dict[str, object]
    evidence: tuple[CandidateEvidence, ...]
    recorded_at_utc: datetime
    correlation_id: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("recorded_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_canonical_evidence(self) -> Self:
        positions = tuple(item.position for item in self.evidence)
        if positions != tuple(range(len(self.evidence))):
            raise ValueError("candidate evidence positions must be contiguous")
        digests = tuple(item.evidence_digest for item in self.evidence)
        if len(digests) != len(set(digests)):
            raise ValueError("candidate evidence digests must be unique")
        expected = candidate_snapshot_digest(
            candidate_id=self.candidate_id,
            revision=self.revision,
            entity_kind=self.entity_kind,
            cluster_id=self.cluster_id,
            resolution_state=self.resolution_state,
            normalized_payload=self.normalized_payload,
            evidence=self.evidence,
            source_lineage_digest=self.source_lineage_digest,
        )
        if self.snapshot_digest != expected:
            raise ValueError("snapshot_digest does not match candidate revision content")
        return self


def candidate_snapshot_digest(
    *,
    candidate_id: UUID,
    revision: int,
    entity_kind: str,
    cluster_id: UUID,
    resolution_state: str,
    normalized_payload: dict[str, object],
    evidence: tuple[CandidateEvidence, ...],
    source_lineage_digest: str,
) -> str:
    return canonical_digest(
        {
            "candidateId": str(candidate_id),
            "clusterId": str(cluster_id),
            "entityKind": entity_kind,
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "normalizedPayload": normalized_payload,
            "resolutionState": resolution_state,
            "revision": revision,
            "sourceLineageDigest": source_lineage_digest,
        }
    )
