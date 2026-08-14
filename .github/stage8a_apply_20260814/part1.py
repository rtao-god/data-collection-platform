from __future__ import annotations

import re
from pathlib import Path

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def clone_project(
    source: str,
    target: str,
    *,
    source_distribution: str,
    target_distribution: str,
    source_module: str,
    target_module: str,
    dependencies: tuple[str, ...],
) -> None:
    text = (ROOT / source / "pyproject.toml").read_text(encoding="utf-8")
    text = text.replace(source_distribution, target_distribution)
    text = text.replace(source_module, target_module)
    rendered = "dependencies = [\n" + "".join(
        f'  "{dependency}",\n' for dependency in dependencies
    ) + "]"
    text, count = re.subn(r"(?ms)^dependencies = \[.*?^\]", rendered, text, count=1)
    if count != 1:
        text, count = re.subn(
            r"(?m)^dependencies = \[[^\n]*\]$", rendered, text, count=1
        )
    if count != 1:
        raise RuntimeError(f"{source}: dependencies declaration was not found")
    target_path = ROOT / target / "pyproject.toml"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(text, encoding="utf-8")


def main() -> int:
    if not (ROOT / "docs/proofs/stage7-resolution-quality-ci.md").exists():
        raise RuntimeError("Stage 7 exact-head proof is required before Stage 8A")

    clone_project(
        "packages/collection_contracts",
        "packages/review_contracts",
        source_distribution="collection-contracts",
        target_distribution="review-contracts",
        source_module="collection_contracts",
        target_module="review_contracts",
        dependencies=("pydantic==2.13.4",),
    )
    clone_project(
        "packages/manual_import_core",
        "packages/review_core",
        source_distribution="manual-import-core",
        target_distribution="review-core",
        source_module="manual_import_core",
        target_module="review_core",
        dependencies=("review-contracts",),
    )

    write(
        "packages/review_contracts/src/review_contracts/contracts.py",
        '''from __future__ import annotations

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
                None
                if supersedes_observation_id is None
                else str(supersedes_observation_id)
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
''',
    )

    write(
        "packages/review_contracts/src/review_contracts/__init__.py",
        '''from review_contracts.contracts import (
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
    candidate_snapshot_digest,
    canonical_digest,
    deterministic_case_id,
    deterministic_decision_id,
    deterministic_manual_observation_id,
    deterministic_suppression_id,
    manual_observation_command_digest,
    review_decision_command_digest,
    suppression_command_digest,
)

__all__ = [
    "CandidateEvidence",
    "CandidateRevision",
    "ManualObservation",
    "ManualObservationCommand",
    "QualityRecord",
    "ReviewCase",
    "ReviewDecision",
    "ReviewDecisionCommand",
    "SuppressionCommand",
    "SuppressionRevision",
    "candidate_snapshot_digest",
    "canonical_digest",
    "deterministic_case_id",
    "deterministic_decision_id",
    "deterministic_manual_observation_id",
    "deterministic_suppression_id",
    "manual_observation_command_digest",
    "review_decision_command_digest",
    "suppression_command_digest",
]
''',
    )

    write(
        "packages/review_core/src/review_core/transitions.py",
        '''from __future__ import annotations

from datetime import UTC, datetime

from review_contracts import (
    CandidateRevision,
    ManualObservation,
    ManualObservationCommand,
    ReviewCase,
    ReviewDecision,
    ReviewDecisionCommand,
    SuppressionCommand,
    SuppressionRevision,
    canonical_digest,
    deterministic_case_id,
    deterministic_decision_id,
    deterministic_manual_observation_id,
    deterministic_suppression_id,
)


class ReviewTransitionError(ValueError):
    code: str = "REVIEW_TRANSITION_INVALID"


class StaleReviewRevision(ReviewTransitionError):
    code = "REVIEW_REVISION_STALE"


class ReviewDecisionConflict(ReviewTransitionError):
    code = "REVIEW_DECISION_CONFLICT"


class SuppressionTransitionError(ReviewTransitionError):
    code = "SUPPRESSION_TRANSITION_INVALID"


def open_review_case(
    candidate: CandidateRevision,
    *,
    reason_codes: tuple[str, ...],
    now_utc: datetime,
    correlation_id: str,
) -> ReviewCase:
    canonical_reasons = tuple(sorted(set(reason_codes)))
    return ReviewCase(
        case_id=deterministic_case_id(
            candidate.candidate_id,
            candidate.revision,
            canonical_reasons,
        ),
        candidate_id=candidate.candidate_id,
        candidate_revision=candidate.revision,
        revision=0,
        state="open",
        reason_codes=canonical_reasons,
        current_decision_id=None,
        opened_at_utc=_utc(now_utc),
        recorded_at_utc=_utc(now_utc),
        correlation_id=correlation_id,
    )


def decide_review_case(
    case: ReviewCase,
    command: ReviewDecisionCommand,
    *,
    now_utc: datetime,
) -> tuple[ReviewCase, ReviewDecision]:
    if command.case_id != case.case_id:
        raise ReviewDecisionConflict("decision command targets another review case")
    if command.expected_case_revision != case.revision:
        raise StaleReviewRevision(
            f"expected review revision {command.expected_case_revision}, actual {case.revision}"
        )
    if case.state == "open":
        if command.supersedes_decision_id is not None:
            raise ReviewDecisionConflict("an initial decision cannot supersede another decision")
    else:
        if case.current_decision_id is None:
            raise ReviewDecisionConflict("decided case is missing its current decision")
        if command.supersedes_decision_id != case.current_decision_id:
            raise ReviewDecisionConflict(
                "a replacement decision must supersede the current decision"
            )

    next_revision = case.revision + 1
    decision_id = deterministic_decision_id(
        case.case_id,
        next_revision,
        command.command_digest,
    )
    decision = ReviewDecision(
        decision_id=decision_id,
        case_id=case.case_id,
        case_revision=next_revision,
        outcome=command.outcome,
        actor_id=command.actor_id,
        rationale=command.rationale,
        evidence_references=command.evidence_references,
        supersedes_decision_id=command.supersedes_decision_id,
        command_digest=command.command_digest,
        decided_at_utc=_utc(now_utc),
        correlation_id=command.correlation_id,
    )
    revision = ReviewCase(
        case_id=case.case_id,
        candidate_id=case.candidate_id,
        candidate_revision=case.candidate_revision,
        revision=next_revision,
        state="decided",
        reason_codes=case.reason_codes,
        current_decision_id=decision_id,
        opened_at_utc=case.opened_at_utc,
        recorded_at_utc=_utc(now_utc),
        correlation_id=command.correlation_id,
    )
    return revision, decision


def create_manual_observation(
    command: ManualObservationCommand,
    *,
    now_utc: datetime,
) -> ManualObservation:
    value_digest = canonical_digest({"value": command.value_text})
    return ManualObservation(
        observation_id=deterministic_manual_observation_id(
            command.candidate_id,
            command.field_key,
            value_digest,
            command.command_digest,
        ),
        candidate_id=command.candidate_id,
        candidate_revision=command.candidate_revision,
        field_key=command.field_key,
        value_text=command.value_text,
        value_digest=value_digest,
        actor_id=command.actor_id,
        reason_code=command.reason_code,
        supersedes_observation_id=command.supersedes_observation_id,
        command_digest=command.command_digest,
        recorded_at_utc=_utc(now_utc),
        correlation_id=command.correlation_id,
    )


def activate_suppression(
    command: SuppressionCommand,
    *,
    now_utc: datetime,
) -> SuppressionRevision:
    if command.expected_revision is not None:
        raise StaleReviewRevision("initial suppression activation requires no revision")
    suppression_id = deterministic_suppression_id(
        command.target_kind,
        command.target_id,
        command.scopes,
        command.reason_code,
    )
    return SuppressionRevision(
        suppression_id=suppression_id,
        revision=0,
        state="active",
        target_kind=command.target_kind,
        target_id=command.target_id,
        scopes=command.scopes,
        reason_code=command.reason_code,
        actor_id=command.actor_id,
        evidence_reference=command.evidence_reference,
        starts_at_utc=_utc(now_utc),
        expires_at_utc=command.expires_at_utc,
        resolved_at_utc=None,
        command_digest=command.command_digest,
        correlation_id=command.correlation_id,
    )


def resolve_suppression(
    current: SuppressionRevision,
    command: SuppressionCommand,
    *,
    now_utc: datetime,
) -> SuppressionRevision:
    if command.expected_revision != current.revision:
        raise StaleReviewRevision(
            f"expected suppression revision {command.expected_revision}, actual {current.revision}"
        )
    if current.state != "active":
        raise SuppressionTransitionError("only an active suppression can be resolved")
    identity = deterministic_suppression_id(
        command.target_kind,
        command.target_id,
        command.scopes,
        command.reason_code,
    )
    if identity != current.suppression_id:
        raise SuppressionTransitionError("suppression identity cannot change on resolution")
    return SuppressionRevision(
        suppression_id=current.suppression_id,
        revision=current.revision + 1,
        state="resolved",
        target_kind=current.target_kind,
        target_id=current.target_id,
        scopes=current.scopes,
        reason_code=current.reason_code,
        actor_id=command.actor_id,
        evidence_reference=command.evidence_reference,
        starts_at_utc=current.starts_at_utc,
        expires_at_utc=current.expires_at_utc,
        resolved_at_utc=_utc(now_utc),
        command_digest=command.command_digest,
        correlation_id=command.correlation_id,
    )


def suppression_applies(
    suppression: SuppressionRevision,
    *,
    scope: str,
    at_utc: datetime,
) -> bool:
    current_time = _utc(at_utc)
    return (
        suppression.state == "active"
        and scope in suppression.scopes
        and suppression.starts_at_utc <= current_time
        and (
            suppression.expires_at_utc is None
            or current_time < suppression.expires_at_utc
        )
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
''',
    )
    write(
        "packages/review_core/src/review_core/__init__.py",
        '''from review_core.transitions import (
    ReviewDecisionConflict,
    ReviewTransitionError,
    StaleReviewRevision,
    SuppressionTransitionError,
    activate_suppression,
    create_manual_observation,
    decide_review_case,
    open_review_case,
    resolve_suppression,
    suppression_applies,
)

__all__ = [
    "ReviewDecisionConflict",
    "ReviewTransitionError",
    "StaleReviewRevision",
    "SuppressionTransitionError",
    "activate_suppression",
    "create_manual_observation",
    "decide_review_case",
    "open_review_case",
    "resolve_suppression",
    "suppression_applies",
]
''',
    )

    write(
        "packages/review_contracts/tests/test_contracts.py",
        '''from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from review_contracts import (
    CandidateEvidence,
    CandidateRevision,
    ReviewDecisionCommand,
    SuppressionCommand,
    candidate_snapshot_digest,
    review_decision_command_digest,
    suppression_command_digest,
)

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def candidate_revision() -> CandidateRevision:
    candidate_id = uuid4()
    cluster_id = uuid4()
    evidence = (
        CandidateEvidence(
            position=0,
            evidence_kind="source_observation",
            evidence_digest=DIGEST_A,
        ),
    )
    digest = candidate_snapshot_digest(
        candidate_id=candidate_id,
        revision=0,
        entity_kind="place",
        cluster_id=cluster_id,
        resolution_state="review",
        normalized_payload={"displayName": "Studio"},
        evidence=evidence,
        source_lineage_digest=DIGEST_B,
    )
    return CandidateRevision(
        candidate_id=candidate_id,
        revision=0,
        entity_kind="place",
        cluster_id=cluster_id,
        resolution_state="review",
        snapshot_digest=digest,
        source_lineage_digest=DIGEST_B,
        normalized_payload={"displayName": "Studio"},
        evidence=evidence,
        recorded_at_utc=NOW,
        correlation_id="candidate-test",
    )


def test_candidate_snapshot_digest_is_wire_enforced() -> None:
    value = candidate_revision()
    with pytest.raises(ValidationError):
        CandidateRevision.model_validate(
            {**value.model_dump(), "snapshot_digest": DIGEST_A}
        )


def test_candidate_evidence_must_be_contiguous() -> None:
    value = candidate_revision()
    evidence = (
        CandidateEvidence(
            position=1,
            evidence_kind="source_observation",
            evidence_digest=DIGEST_A,
        ),
    )
    with pytest.raises(ValidationError):
        CandidateRevision.model_validate({**value.model_dump(), "evidence": evidence})


def test_decision_command_rejects_markup() -> None:
    case_id = uuid4()
    with pytest.raises(ValidationError):
        ReviewDecisionCommand(
            case_id=case_id,
            expected_case_revision=0,
            outcome="accept_candidate",
            actor_id="reviewer",
            rationale="<b>unsafe</b>",
            evidence_references=(DIGEST_A,),
            command_digest=DIGEST_B,
            correlation_id="review-test",
        )


def test_decision_command_digest_is_enforced() -> None:
    case_id = uuid4()
    digest = review_decision_command_digest(
        case_id=case_id,
        expected_case_revision=0,
        outcome="accept_candidate",
        actor_id="reviewer",
        rationale="Verified against source evidence.",
        evidence_references=(DIGEST_A,),
        supersedes_decision_id=None,
    )
    command = ReviewDecisionCommand(
        case_id=case_id,
        expected_case_revision=0,
        outcome="accept_candidate",
        actor_id="reviewer",
        rationale="Verified against source evidence.",
        evidence_references=(DIGEST_A,),
        command_digest=digest,
        correlation_id="review-test",
    )
    assert command.command_digest == digest


def test_suppression_scopes_are_canonical() -> None:
    digest = suppression_command_digest(
        target_kind="candidate",
        target_id="candidate-1",
        scopes=("discovery", "export"),
        reason_code="LEGAL_REVIEW",
        actor_id="reviewer",
        evidence_reference=DIGEST_A,
        expected_revision=None,
        expires_at_utc=None,
    )
    with pytest.raises(ValidationError):
        SuppressionCommand(
            target_kind="candidate",
            target_id="candidate-1",
            scopes=("export", "discovery"),
            reason_code="LEGAL_REVIEW",
            actor_id="reviewer",
            evidence_reference=DIGEST_A,
            expected_revision=None,
            command_digest=digest,
            correlation_id="suppression-test",
        )
''',
    )

    write(
        "packages/review_core/tests/test_transitions.py",
        '''from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from review_contracts import (
    CandidateEvidence,
    CandidateRevision,
    ManualObservationCommand,
    ReviewDecisionCommand,
    SuppressionCommand,
    candidate_snapshot_digest,
    manual_observation_command_digest,
    review_decision_command_digest,
    suppression_command_digest,
)
from review_core import (
    ReviewDecisionConflict,
    StaleReviewRevision,
    activate_suppression,
    create_manual_observation,
    decide_review_case,
    open_review_case,
    resolve_suppression,
    suppression_applies,
)

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def candidate() -> CandidateRevision:
    candidate_id = uuid4()
    cluster_id = uuid4()
    evidence = (
        CandidateEvidence(
            position=0,
            evidence_kind="source_observation",
            evidence_digest=DIGEST_A,
        ),
    )
    digest = candidate_snapshot_digest(
        candidate_id=candidate_id,
        revision=0,
        entity_kind="place",
        cluster_id=cluster_id,
        resolution_state="review",
        normalized_payload={"displayName": "Studio"},
        evidence=evidence,
        source_lineage_digest=DIGEST_B,
    )
    return CandidateRevision(
        candidate_id=candidate_id,
        revision=0,
        entity_kind="place",
        cluster_id=cluster_id,
        resolution_state="review",
        snapshot_digest=digest,
        source_lineage_digest=DIGEST_B,
        normalized_payload={"displayName": "Studio"},
        evidence=evidence,
        recorded_at_utc=NOW,
        correlation_id="candidate-test",
    )


def decision_command(
    case_id: object,
    *,
    expected_revision: int,
    supersedes: object | None = None,
) -> ReviewDecisionCommand:
    digest = review_decision_command_digest(
        case_id=case_id,  # type: ignore[arg-type]
        expected_case_revision=expected_revision,
        outcome="accept_candidate",
        actor_id="reviewer",
        rationale="Verified against exact evidence.",
        evidence_references=(DIGEST_A,),
        supersedes_decision_id=supersedes,  # type: ignore[arg-type]
    )
    return ReviewDecisionCommand(
        case_id=case_id,  # type: ignore[arg-type]
        expected_case_revision=expected_revision,
        outcome="accept_candidate",
        actor_id="reviewer",
        rationale="Verified against exact evidence.",
        evidence_references=(DIGEST_A,),
        supersedes_decision_id=supersedes,  # type: ignore[arg-type]
        command_digest=digest,
        correlation_id="review-test",
    )


def suppression_command(
    *,
    expected_revision: int | None,
    evidence: str = DIGEST_A,
) -> SuppressionCommand:
    values = {
        "target_kind": "candidate",
        "target_id": "candidate-1",
        "scopes": ("discovery", "export"),
        "reason_code": "LEGAL_REVIEW",
        "actor_id": "reviewer",
        "evidence_reference": evidence,
        "expected_revision": expected_revision,
        "expires_at_utc": NOW + timedelta(days=1),
    }
    digest = suppression_command_digest(**values)
    return SuppressionCommand(
        **values,
        command_digest=digest,
        correlation_id="suppression-test",
    )


def test_open_case_has_deterministic_identity() -> None:
    value = candidate()
    first = open_review_case(
        value,
        reason_codes=("MATCH_REVIEW",),
        now_utc=NOW,
        correlation_id="review-test",
    )
    second = open_review_case(
        value,
        reason_codes=("MATCH_REVIEW",),
        now_utc=NOW,
        correlation_id="review-test",
    )
    assert first == second


def test_decision_advances_revision_and_is_immutable_value() -> None:
    case = open_review_case(
        candidate(),
        reason_codes=("MATCH_REVIEW",),
        now_utc=NOW,
        correlation_id="review-test",
    )
    revision, decision = decide_review_case(
        case,
        decision_command(case.case_id, expected_revision=0),
        now_utc=NOW + timedelta(minutes=1),
    )
    assert revision.revision == 1
    assert revision.current_decision_id == decision.decision_id
    assert decision.case_revision == 1


def test_stale_review_revision_is_rejected() -> None:
    case = open_review_case(
        candidate(),
        reason_codes=("MATCH_REVIEW",),
        now_utc=NOW,
        correlation_id="review-test",
    )
    with pytest.raises(StaleReviewRevision):
        decide_review_case(
            case,
            decision_command(case.case_id, expected_revision=1),
            now_utc=NOW + timedelta(minutes=1),
        )


def test_replacement_decision_must_supersede_current_decision() -> None:
    case = open_review_case(
        candidate(),
        reason_codes=("MATCH_REVIEW",),
        now_utc=NOW,
        correlation_id="review-test",
    )
    decided, decision = decide_review_case(
        case,
        decision_command(case.case_id, expected_revision=0),
        now_utc=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ReviewDecisionConflict):
        decide_review_case(
            decided,
            decision_command(decided.case_id, expected_revision=1),
            now_utc=NOW + timedelta(minutes=2),
        )
    replacement, new_decision = decide_review_case(
        decided,
        decision_command(
            decided.case_id,
            expected_revision=1,
            supersedes=decision.decision_id,
        ),
        now_utc=NOW + timedelta(minutes=2),
    )
    assert replacement.revision == 2
    assert new_decision.supersedes_decision_id == decision.decision_id


def test_manual_observation_is_new_evidence_not_candidate_mutation() -> None:
    value = candidate()
    digest = manual_observation_command_digest(
        candidate_id=value.candidate_id,
        candidate_revision=value.revision,
        field_key="website",
        value_text="https://example.test",
        actor_id="reviewer",
        reason_code="MANUAL_VERIFICATION",
        supersedes_observation_id=None,
    )
    observation = create_manual_observation(
        ManualObservationCommand(
            candidate_id=value.candidate_id,
            candidate_revision=value.revision,
            field_key="website",
            value_text="https://example.test",
            actor_id="reviewer",
            reason_code="MANUAL_VERIFICATION",
            command_digest=digest,
            correlation_id="manual-observation-test",
        ),
        now_utc=NOW,
    )
    assert value.normalized_payload == {"displayName": "Studio"}
    assert observation.candidate_revision == value.revision
    assert observation.value_digest.startswith("sha256:")


def test_suppression_applies_only_to_owned_scope_and_time() -> None:
    active = activate_suppression(suppression_command(expected_revision=None), now_utc=NOW)
    assert suppression_applies(active, scope="export", at_utc=NOW) is True
    assert suppression_applies(active, scope="normalization", at_utc=NOW) is False
    assert suppression_applies(
        active,
        scope="export",
        at_utc=NOW + timedelta(days=2),
    ) is False


def test_suppression_resolution_is_revision_guarded() -> None:
    active = activate_suppression(suppression_command(expected_revision=None), now_utc=NOW)
    with pytest.raises(StaleReviewRevision):
        resolve_suppression(
            active,
            suppression_command(expected_revision=1, evidence=DIGEST_B),
            now_utc=NOW + timedelta(minutes=1),
        )
    resolved = resolve_suppression(
        active,
        suppression_command(expected_revision=0, evidence=DIGEST_B),
        now_utc=NOW + timedelta(minutes=1),
    )
    assert resolved.state == "resolved"
    assert resolved.revision == 1
''',
    )

    write(
        "tools/review_contract_generation/generate.py",
        '''from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

from review_contracts import (
    CandidateRevision,
    ManualObservation,
    ReviewCase,
    ReviewDecision,
    ReviewDecisionCommand,
    SuppressionCommand,
    SuppressionRevision,
)

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "contracts/review"
MODELS = {
    "candidate-revision.schema.json": CandidateRevision,
    "manual-observation.schema.json": ManualObservation,
    "review-case.schema.json": ReviewCase,
    "review-decision-command.schema.json": ReviewDecisionCommand,
    "review-decision.schema.json": ReviewDecision,
    "suppression-command.schema.json": SuppressionCommand,
    "suppression-revision.schema.json": SuppressionRevision,
}


def render() -> dict[str, str]:
    rendered = {
        name: json.dumps(
            model.model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
        for name, model in MODELS.items()
    }
    manifest = {
        "contract": "collector-review-contract-manifest",
        "contractRevision": "review-contract-manifest-v1",
        "files": {
            name: f"sha256:{sha256(content.encode('utf-8')).hexdigest()}"
            for name, content in sorted(rendered.items())
        },
    }
    rendered["manifest.json"] = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.check:
        drift = [
            name
            for name, content in expected.items()
            if not (OUTPUT / name).exists()
            or (OUTPUT / name).read_text(encoding="utf-8") != content
        ]
        if drift:
            raise SystemExit("review contract drift: " + ", ".join(drift))
        return 0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, content in expected.items():
        (OUTPUT / name).write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
    )
    write(
        "tools/review_contract_generation/tests/test_generate.py",
        '''from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_review_contract_artifacts_are_current() -> None:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [sys.executable, "tools/review_contract_generation/generate.py", "--check"],
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
''',
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
