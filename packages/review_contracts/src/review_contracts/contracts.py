from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Code = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")]
Key = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")]
PlainText = Annotated[str, Field(min_length=1, max_length=4000)]
CandidateResolutionState = Literal["resolved", "review", "blocked"]
ReviewCaseState = Literal["open", "decided"]
ReviewOutcome = Literal[
    "accept_candidate",
    "reject_candidate",
    "approve_merge",
    "reject_merge",
    "request_recollection",
    "block_export",
]
SuppressionState = Literal["active", "resolved"]
SuppressionScope = Literal["discovery", "normalization", "export"]
SuppressionTargetKind = Literal["candidate", "source_observation", "artifact", "source"]

_CASE_NAMESPACE = UUID("01cf920c-9a4f-4fd3-9cb0-39e4475d24f5")
_DECISION_NAMESPACE = UUID("ca22a3d1-0be5-4476-8834-d3b13938bb93")
_OBSERVATION_NAMESPACE = UUID("a91821fd-3537-43e4-b2a4-05fc01ff7786")
_SUPPRESSION_NAMESPACE = UUID("8d4d7db5-f641-44de-8606-fe2976495957")


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return f"sha256:{sha256(payload).hexdigest()}"


def deterministic_case_id(
    candidate_id: UUID,
    candidate_revision: int,
    reason_codes: tuple[str, ...],
) -> UUID:
    canonical = tuple(sorted(set(reason_codes)))
    if not canonical:
        raise ValueError("a review case requires at least one reason")
    return uuid5(
        _CASE_NAMESPACE,
        f"{candidate_id}:{candidate_revision}:{canonical_digest(canonical)}",
    )


def deterministic_decision_id(
    case_id: UUID,
    case_revision: int,
    command_digest: str,
) -> UUID:
    return uuid5(_DECISION_NAMESPACE, f"{case_id}:{case_revision}:{command_digest}")


def deterministic_manual_observation_id(
    candidate_id: UUID,
    field_key: str,
    value_digest: str,
    command_digest: str,
) -> UUID:
    return uuid5(
        _OBSERVATION_NAMESPACE,
        f"{candidate_id}:{field_key}:{value_digest}:{command_digest}",
    )


def deterministic_suppression_id(
    target_kind: str,
    target_id: str,
    scopes: tuple[str, ...],
    reason_code: str,
) -> UUID:
    canonical_scopes = tuple(sorted(set(scopes)))
    if not canonical_scopes:
        raise ValueError("a suppression requires at least one scope")
    return uuid5(
        _SUPPRESSION_NAMESPACE,
        f"{target_kind}:{target_id}:{canonical_digest(canonical_scopes)}:{reason_code}",
    )


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _require_plain_text(value: str) -> str:
    if "<" in value or ">" in value:
        raise ValueError("plain text must not contain markup delimiters")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ValueError("plain text contains a forbidden control character")
    return value


class CandidateEvidence(StrictContract):
    position: Annotated[int, Field(ge=0)]
    evidence_kind: Literal["source_observation", "manual_observation", "artifact"]
    evidence_digest: Digest


class CandidateRevision(StrictContract):
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
        return _require_aware_utc(value)

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


class QualityRecord(StrictContract):
    evaluation_id: UUID
    candidate_id: UUID
    candidate_revision: Annotated[int, Field(ge=0)]
    policy_digest: Digest
    export_eligible: bool
    blockers: tuple[Code, ...]
    evaluation_digest: Digest
    evaluated_at_utc: datetime
    correlation_id: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("evaluated_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)

    @model_validator(mode="after")
    def require_canonical_result(self) -> Self:
        canonical = tuple(sorted(set(self.blockers)))
        if self.blockers != canonical:
            raise ValueError("quality blockers must be unique and ordered")
        if self.export_eligible == bool(self.blockers):
            raise ValueError("export eligibility must be inverse of blocker presence")
        return self


class ReviewCase(StrictContract):
    case_id: UUID
    candidate_id: UUID
    candidate_revision: Annotated[int, Field(ge=0)]
    revision: Annotated[int, Field(ge=0)]
    state: ReviewCaseState
    reason_codes: tuple[Code, ...]
    current_decision_id: UUID | None
    opened_at_utc: datetime
    recorded_at_utc: datetime
    correlation_id: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("opened_at_utc", "recorded_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)

    @model_validator(mode="after")
    def require_case_shape(self) -> Self:
        canonical = tuple(sorted(set(self.reason_codes)))
        if not canonical or self.reason_codes != canonical:
            raise ValueError("review reasons must be non-empty, unique, and ordered")
        expected = deterministic_case_id(
            self.candidate_id,
            self.candidate_revision,
            self.reason_codes,
        )
        if self.case_id != expected:
            raise ValueError("case_id does not match deterministic case identity")
        if (self.state == "open") != (self.current_decision_id is None):
            raise ValueError("open cases cannot reference a decision and decided cases must")
        if self.recorded_at_utc < self.opened_at_utc:
            raise ValueError("case revision cannot predate case opening")
        return self


class ReviewDecisionCommand(StrictContract):
    case_id: UUID
    expected_case_revision: Annotated[int, Field(ge=0)]
    outcome: ReviewOutcome
    actor_id: Annotated[str, Field(min_length=1, max_length=200)]
    rationale: PlainText
    evidence_references: tuple[Digest, ...]
    supersedes_decision_id: UUID | None = None
    command_digest: Digest
    correlation_id: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("rationale")
    @classmethod
    def require_plain_text(cls, value: str) -> str:
        return _require_plain_text(value)

    @field_validator("evidence_references")
    @classmethod
    def require_canonical_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical or value != canonical:
            raise ValueError("decision evidence must be non-empty, unique, and ordered")
        return value

    @model_validator(mode="after")
    def require_command_digest(self) -> Self:
        expected = review_decision_command_digest(
            case_id=self.case_id,
            expected_case_revision=self.expected_case_revision,
            outcome=self.outcome,
            actor_id=self.actor_id,
            rationale=self.rationale,
            evidence_references=self.evidence_references,
            supersedes_decision_id=self.supersedes_decision_id,
        )
        if self.command_digest != expected:
            raise ValueError("command_digest does not match decision command")
        return self


class ReviewDecision(StrictContract):
    decision_id: UUID
    case_id: UUID
    case_revision: Annotated[int, Field(ge=1)]
    outcome: ReviewOutcome
    actor_id: Annotated[str, Field(min_length=1, max_length=200)]
    rationale: PlainText
    evidence_references: tuple[Digest, ...]
    supersedes_decision_id: UUID | None
    command_digest: Digest
    decided_at_utc: datetime
    correlation_id: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("rationale")
    @classmethod
    def require_plain_text(cls, value: str) -> str:
        return _require_plain_text(value)

    @field_validator("decided_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)

    @model_validator(mode="after")
    def require_deterministic_identity(self) -> Self:
        expected = deterministic_decision_id(
            self.case_id,
            self.case_revision,
            self.command_digest,
        )
        if self.decision_id != expected:
            raise ValueError("decision_id does not match deterministic decision identity")
        return self


class ManualObservationCommand(StrictContract):
    candidate_id: UUID
    candidate_revision: Annotated[int, Field(ge=0)]
    field_key: Key
    value_text: PlainText
    actor_id: Annotated[str, Field(min_length=1, max_length=200)]
    reason_code: Code
    supersedes_observation_id: UUID | None = None
    command_digest: Digest
    correlation_id: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("value_text")
    @classmethod
    def require_plain_text(cls, value: str) -> str:
        return _require_plain_text(value)

    @model_validator(mode="after")
    def require_command_digest(self) -> Self:
        expected = manual_observation_command_digest(
            candidate_id=self.candidate_id,
            candidate_revision=self.candidate_revision,
            field_key=self.field_key,
            value_text=self.value_text,
            actor_id=self.actor_id,
            reason_code=self.reason_code,
            supersedes_observation_id=self.supersedes_observation_id,
        )
        if self.command_digest != expected:
            raise ValueError("command_digest does not match manual observation command")
        return self


class ManualObservation(StrictContract):
    observation_id: UUID
    candidate_id: UUID
    candidate_revision: Annotated[int, Field(ge=0)]
    field_key: Key
    value_text: PlainText
    value_digest: Digest
    actor_id: Annotated[str, Field(min_length=1, max_length=200)]
    reason_code: Code
    supersedes_observation_id: UUID | None
    command_digest: Digest
    recorded_at_utc: datetime
    correlation_id: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("value_text")
    @classmethod
    def require_plain_text(cls, value: str) -> str:
        return _require_plain_text(value)

    @field_validator("recorded_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)

    @model_validator(mode="after")
    def require_deterministic_identity(self) -> Self:
        expected_value_digest = canonical_digest({"value": self.value_text})
        if self.value_digest != expected_value_digest:
            raise ValueError("value_digest does not match manual observation value")
        expected_id = deterministic_manual_observation_id(
            self.candidate_id,
            self.field_key,
            self.value_digest,
            self.command_digest,
        )
        if self.observation_id != expected_id:
            raise ValueError("observation_id does not match deterministic identity")
        return self


class SuppressionCommand(StrictContract):
    target_kind: SuppressionTargetKind
    target_id: Annotated[str, Field(min_length=1, max_length=500)]
    scopes: tuple[SuppressionScope, ...]
    reason_code: Code
    actor_id: Annotated[str, Field(min_length=1, max_length=200)]
    evidence_reference: Digest
    expected_revision: Annotated[int, Field(ge=0)] | None
    expires_at_utc: datetime | None = None
    command_digest: Digest
    correlation_id: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("scopes")
    @classmethod
    def require_canonical_scopes(
        cls,
        value: tuple[SuppressionScope, ...],
    ) -> tuple[SuppressionScope, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical or value != canonical:
            raise ValueError("suppression scopes must be non-empty, unique, and ordered")
        return value

    @field_validator("expires_at_utc")
    @classmethod
    def require_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware_utc(value)

    @model_validator(mode="after")
    def require_command_digest(self) -> Self:
        expected = suppression_command_digest(
            target_kind=self.target_kind,
            target_id=self.target_id,
            scopes=self.scopes,
            reason_code=self.reason_code,
            actor_id=self.actor_id,
            evidence_reference=self.evidence_reference,
            expected_revision=self.expected_revision,
            expires_at_utc=self.expires_at_utc,
        )
        if self.command_digest != expected:
            raise ValueError("command_digest does not match suppression command")
        return self


class SuppressionRevision(StrictContract):
    suppression_id: UUID
    revision: Annotated[int, Field(ge=0)]
    state: SuppressionState
    target_kind: SuppressionTargetKind
    target_id: Annotated[str, Field(min_length=1, max_length=500)]
    scopes: tuple[SuppressionScope, ...]
    reason_code: Code
    actor_id: Annotated[str, Field(min_length=1, max_length=200)]
    evidence_reference: Digest
    starts_at_utc: datetime
    expires_at_utc: datetime | None
    resolved_at_utc: datetime | None
    command_digest: Digest
    correlation_id: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("starts_at_utc", "expires_at_utc", "resolved_at_utc")
    @classmethod
    def require_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware_utc(value)

    @model_validator(mode="after")
    def require_suppression_shape(self) -> Self:
        canonical = tuple(sorted(set(self.scopes)))
        if not canonical or self.scopes != canonical:
            raise ValueError("suppression scopes must be non-empty, unique, and ordered")
        expected_id = deterministic_suppression_id(
            self.target_kind,
            self.target_id,
            self.scopes,
            self.reason_code,
        )
        if self.suppression_id != expected_id:
            raise ValueError("suppression_id does not match deterministic identity")
        if self.state == "active" and self.resolved_at_utc is not None:
            raise ValueError("active suppression cannot be resolved")
        if self.state == "resolved" and self.resolved_at_utc is None:
            raise ValueError("resolved suppression requires resolved_at_utc")
        if self.expires_at_utc is not None and self.expires_at_utc <= self.starts_at_utc:
            raise ValueError("suppression expiry must follow activation")
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


def review_decision_command_digest(
    *,
    case_id: UUID,
    expected_case_revision: int,
    outcome: str,
    actor_id: str,
    rationale: str,
    evidence_references: tuple[str, ...],
    supersedes_decision_id: UUID | None,
) -> str:
    return canonical_digest(
        {
            "actorId": actor_id,
            "caseId": str(case_id),
            "evidenceReferences": list(evidence_references),
            "expectedCaseRevision": expected_case_revision,
            "outcome": outcome,
            "rationale": rationale,
            "supersedesDecisionId": (
                None if supersedes_decision_id is None else str(supersedes_decision_id)
            ),
        }
    )


def manual_observation_command_digest(
    *,
    candidate_id: UUID,
    candidate_revision: int,
    field_key: str,
    value_text: str,
    actor_id: str,
    reason_code: str,
    supersedes_observation_id: UUID | None,
) -> str:
    return canonical_digest(
        {
            "actorId": actor_id,
            "candidateId": str(candidate_id),
            "candidateRevision": candidate_revision,
            "fieldKey": field_key,
            "reasonCode": reason_code,
            "supersedesObservationId": (
                None if supersedes_observation_id is None else str(supersedes_observation_id)
            ),
            "value": value_text,
        }
    )


def suppression_command_digest(
    *,
    target_kind: str,
    target_id: str,
    scopes: tuple[str, ...],
    reason_code: str,
    actor_id: str,
    evidence_reference: str,
    expected_revision: int | None,
    expires_at_utc: datetime | None,
) -> str:
    return canonical_digest(
        {
            "actorId": actor_id,
            "evidenceReference": evidence_reference,
            "expectedRevision": expected_revision,
            "expiresAtUtc": expires_at_utc,
            "reasonCode": reason_code,
            "scopes": list(scopes),
            "targetId": target_id,
            "targetKind": target_kind,
        }
    )
