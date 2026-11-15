from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Token = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
PipelineAdvancementState = Literal["pending", "leased", "applied", "blocked"]


class PipelineAdvancementRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    contract: Literal["worker-gateway-pipeline-advancement-run"]
    contract_revision: Literal["pipeline-advancement-run-v1"] = Field(alias="contractRevision")
    dagster_execution_id: Token = Field(alias="dagsterExecutionId")
    dagster_build_id: Token = Field(alias="dagsterBuildId")


class PipelineAdvancementBlockerPayload(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        from_attributes=True,
        populate_by_name=True,
    )

    owner: Token
    code: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")]
    message: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    required_action: Annotated[str, StringConstraints(min_length=1, max_length=1_000)] = Field(
        alias="requiredAction"
    )
    context: dict[str, object]


class PipelineAdvancementStatusPayload(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        from_attributes=True,
        populate_by_name=True,
    )

    advancement_id: UUID = Field(alias="advancementId")
    source_work_unit_id: UUID = Field(alias="sourceWorkUnitId")
    run_id: UUID = Field(alias="runId")
    state: PipelineAdvancementState
    transition_key: Token = Field(alias="transitionKey")
    transition_plan_digest: Digest = Field(alias="transitionPlanDigest")
    revision: Annotated[int, Field(ge=0)]
    attempt_count: Annotated[int, Field(ge=0)] = Field(alias="attemptCount")
    result_digest: Digest | None = Field(alias="resultDigest")
    blocker: PipelineAdvancementBlockerPayload | None
    created_at_utc: datetime = Field(alias="createdAtUtc")
    updated_at_utc: datetime = Field(alias="updatedAtUtc")


class PipelineDiscoveryFailurePayload(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        from_attributes=True,
        populate_by_name=True,
    )

    source_work_unit_id: UUID = Field(alias="sourceWorkUnitId")
    code: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")]
    message: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    required_action: Annotated[str, StringConstraints(min_length=1, max_length=1_000)] = Field(
        alias="requiredAction"
    )
    context: dict[str, object]


class PipelineAdvancementRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    contract: Literal["worker-gateway-pipeline-advancement-tick"] = Field(
        default="worker-gateway-pipeline-advancement-tick"
    )
    contract_revision: Literal["pipeline-advancement-tick-v1"] = Field(
        default="pipeline-advancement-tick-v1", alias="contractRevision"
    )
    registered_count: Annotated[int, Field(ge=0)] = Field(alias="registeredCount")
    claimed_advancement_id: UUID | None = Field(alias="claimedAdvancementId")
    terminal_status: PipelineAdvancementStatusPayload | None = Field(alias="terminalStatus")
    discovery_failures: tuple[PipelineDiscoveryFailurePayload, ...] = Field(
        alias="discoveryFailures"
    )

    @model_validator(mode="after")
    def validate_terminal_status_identity(self) -> Self:
        if self.claimed_advancement_id is None and self.terminal_status is not None:
            raise ValueError("an unclaimed tick cannot contain a terminal status")
        if self.terminal_status is not None:
            if self.terminal_status.advancement_id != self.claimed_advancement_id:
                raise ValueError("terminal status belongs to another advancement")
            if self.terminal_status.state not in {"applied", "blocked"}:
                raise ValueError("terminal status must be applied or blocked")
        return self
