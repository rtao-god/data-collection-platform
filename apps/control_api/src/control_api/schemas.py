from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from collection_application import CollectionRunStatus, RunCoverageReport
from review_application import ReviewCaseDetail, ReviewQueuePage, encode_cursor
from review_contracts import (
    CandidateRevision,
    ManualObservation,
    QualityRecord,
    ReviewCase,
    ReviewDecision,
    SuppressionRevision,
)

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Code = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")]
Key = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")]


class ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ErrorResponse(ApiModel):
    code: Code
    owner: str
    message: str
    required_action: str = Field(alias="requiredAction")
    correlation_id: str = Field(alias="correlationId")
    context: dict[str, object] = Field(default_factory=dict)


class ReviewQueueItemResponse(ApiModel):
    case_id: UUID = Field(alias="caseId")
    candidate_id: UUID = Field(alias="candidateId")
    candidate_revision: int = Field(alias="candidateRevision")
    revision: int
    state: str
    reason_codes: tuple[str, ...] = Field(alias="reasonCodes")
    current_decision_id: UUID | None = Field(alias="currentDecisionId")
    recorded_at_utc: datetime = Field(alias="recordedAtUtc")


class ReviewQueueResponse(ApiModel):
    items: tuple[ReviewQueueItemResponse, ...]
    next_cursor: str | None = Field(alias="nextCursor")

    @classmethod
    def from_page(cls, page: ReviewQueuePage) -> ReviewQueueResponse:
        return cls(
            items=tuple(
                ReviewQueueItemResponse(
                    case_id=item.case_id,
                    candidate_id=item.candidate_id,
                    candidate_revision=item.candidate_revision,
                    revision=item.revision,
                    state=item.state,
                    reason_codes=item.reason_codes,
                    current_decision_id=item.current_decision_id,
                    recorded_at_utc=item.recorded_at_utc,
                )
                for item in page.items
            ),
            next_cursor=encode_cursor(page.next_cursor),
        )


class ReviewCaseDetailResponse(ApiModel):
    case: ReviewCase
    candidate: CandidateRevision
    quality: QualityRecord | None
    decisions: tuple[ReviewDecision, ...]
    manual_observations: tuple[ManualObservation, ...] = Field(alias="manualObservations")
    active_suppressions: tuple[SuppressionRevision, ...] = Field(alias="activeSuppressions")

    @classmethod
    def from_detail(cls, detail: ReviewCaseDetail) -> ReviewCaseDetailResponse:
        return cls(
            case=detail.case,
            candidate=detail.candidate,
            quality=detail.quality,
            decisions=detail.decisions,
            manual_observations=detail.manual_observations,
            active_suppressions=detail.active_suppressions,
        )


class SubmitDecisionRequest(ApiModel):
    expected_revision: Annotated[int, Field(ge=0)] = Field(alias="expectedRevision")
    outcome: Literal[
        "accept_candidate",
        "reject_candidate",
        "approve_merge",
        "reject_merge",
        "request_recollection",
        "block_export",
    ]
    rationale: Annotated[str, Field(min_length=1, max_length=4000)]
    evidence_references: tuple[Digest, ...] = Field(alias="evidenceReferences")
    supersedes_decision_id: UUID | None = Field(
        default=None,
        alias="supersedesDecisionId",
    )

    @field_validator("rationale")
    @classmethod
    def require_plain_rationale(cls, value: str) -> str:
        return _plain_text(value)

    @field_validator("evidence_references")
    @classmethod
    def require_canonical_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical or value != canonical:
            raise ValueError("evidenceReferences must be non-empty, unique, and ordered")
        return value


class DecisionResponse(ApiModel):
    case: ReviewCase
    decision: ReviewDecision


class ManualObservationRequest(ApiModel):
    candidate_revision: Annotated[int, Field(ge=0)] = Field(alias="candidateRevision")
    field_key: Key = Field(alias="fieldKey")
    value_text: Annotated[str, Field(min_length=1, max_length=4000)] = Field(alias="valueText")
    reason_code: Code = Field(alias="reasonCode")
    supersedes_observation_id: UUID | None = Field(
        default=None,
        alias="supersedesObservationId",
    )

    @field_validator("value_text")
    @classmethod
    def require_plain_value(cls, value: str) -> str:
        return _plain_text(value)


class ActivateSuppressionRequest(ApiModel):
    target_kind: Literal["candidate", "source_observation", "artifact", "source"] = Field(
        alias="targetKind"
    )
    target_id: Annotated[str, Field(min_length=1, max_length=500)] = Field(alias="targetId")
    scopes: tuple[Literal["discovery", "normalization", "export"], ...]
    reason_code: Code = Field(alias="reasonCode")
    evidence_reference: Digest = Field(alias="evidenceReference")
    expires_at_utc: datetime | None = Field(default=None, alias="expiresAtUtc")

    @field_validator("scopes")
    @classmethod
    def require_canonical_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical or value != canonical:
            raise ValueError("scopes must be non-empty, unique, and ordered")
        return value


class ResolveSuppressionRequest(ApiModel):
    expected_revision: Annotated[int, Field(ge=0)] = Field(alias="expectedRevision")
    evidence_reference: Digest = Field(alias="evidenceReference")


class CreateRunRequest(ApiModel):
    run_id: UUID = Field(alias="runId")
    campaign_key: Key = Field(alias="campaignKey")


class RunTransitionRequest(ApiModel):
    expected_revision: Annotated[int, Field(ge=0)] = Field(alias="expectedRevision")
    reason: Annotated[str, Field(min_length=1, max_length=1000)]

    @field_validator("reason")
    @classmethod
    def require_plain_reason(cls, value: str) -> str:
        return _plain_text(value)


class WorkStateCountResponse(ApiModel):
    state: str
    count: Annotated[int, Field(ge=0)]


class StageRunResponse(ApiModel):
    stage_run_id: UUID = Field(alias="stageRunId")
    stage: str
    state: str
    revision: Annotated[int, Field(ge=0)]
    work_counts: tuple[WorkStateCountResponse, ...] = Field(alias="workCounts")


class RunResponse(ApiModel):
    run_id: UUID = Field(alias="runId")
    campaign_key: str = Field(alias="campaignKey")
    config_bundle_digest: Digest = Field(alias="configBundleDigest")
    state: str
    revision: Annotated[int, Field(ge=0)]
    created_at_utc: datetime = Field(alias="createdAtUtc")
    updated_at_utc: datetime = Field(alias="updatedAtUtc")
    stages: tuple[StageRunResponse, ...]

    @classmethod
    def from_status(cls, status: CollectionRunStatus) -> RunResponse:
        return cls(
            run_id=status.run_id,
            campaign_key=status.campaign_key,
            config_bundle_digest=status.config_bundle_digest,
            state=status.state.value,
            revision=status.revision,
            created_at_utc=status.created_at_utc,
            updated_at_utc=status.updated_at_utc,
            stages=tuple(
                StageRunResponse(
                    stage_run_id=stage.stage_run_id,
                    stage=stage.stage.value,
                    state=stage.state.value,
                    revision=stage.revision,
                    work_counts=tuple(
                        WorkStateCountResponse(state=item.state.value, count=item.count)
                        for item in stage.work_counts
                    ),
                )
                for stage in status.stages
            ),
        )


class StageCoverageResponse(ApiModel):
    stage: str
    total: Annotated[int, Field(ge=0)]
    pending: Annotated[int, Field(ge=0)]
    leased: Annotated[int, Field(ge=0)]
    retry_wait: Annotated[int, Field(ge=0)] = Field(alias="retryWait")
    succeeded: Annotated[int, Field(ge=0)]
    dead_letter: Annotated[int, Field(ge=0)] = Field(alias="deadLetter")
    blocked_by_policy: Annotated[int, Field(ge=0)] = Field(alias="blockedByPolicy")
    cancelled: Annotated[int, Field(ge=0)]
    superseded: Annotated[int, Field(ge=0)]
    terminal: Annotated[int, Field(ge=0)]


class RunCoverageBlockerResponse(ApiModel):
    code: Code
    stage: str | None
    count: Annotated[int, Field(ge=1)]
    message: str
    required_action: str = Field(alias="requiredAction")


class RunCoverageResponse(ApiModel):
    run_id: UUID = Field(alias="runId")
    state: str
    revision: Annotated[int, Field(ge=0)]
    total: Annotated[int, Field(ge=0)]
    terminal: Annotated[int, Field(ge=0)]
    succeeded: Annotated[int, Field(ge=0)]
    stages: tuple[StageCoverageResponse, ...]
    blockers: tuple[RunCoverageBlockerResponse, ...]

    @classmethod
    def from_report(cls, report: RunCoverageReport) -> RunCoverageResponse:
        return cls(
            run_id=report.run_id,
            state=report.state.value,
            revision=report.revision,
            total=report.total,
            terminal=report.terminal,
            succeeded=report.succeeded,
            stages=tuple(
                StageCoverageResponse(
                    stage=item.stage.value,
                    total=item.total,
                    pending=item.pending,
                    leased=item.leased,
                    retry_wait=item.retry_wait,
                    succeeded=item.succeeded,
                    dead_letter=item.dead_letter,
                    blocked_by_policy=item.blocked_by_policy,
                    cancelled=item.cancelled,
                    superseded=item.superseded,
                    terminal=item.terminal,
                )
                for item in report.stages
            ),
            blockers=tuple(
                RunCoverageBlockerResponse(
                    code=item.code,
                    stage=item.stage.value if item.stage is not None else None,
                    count=item.count,
                    message=item.message,
                    required_action=item.required_action,
                )
                for item in report.blockers
            ),
        )


def _plain_text(value: str) -> str:
    if "<" in value or ">" in value:
        raise ValueError("plain text must not contain markup delimiters")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ValueError("plain text contains a forbidden control character")
    return value
