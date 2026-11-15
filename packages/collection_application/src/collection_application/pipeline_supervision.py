from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from collection_application.pipeline_advancement import (
    ApplyPipelineAdvancement,
    BlockPipelineAdvancement,
    ClaimPipelineAdvancement,
    PipelineAdvancementService,
    PipelineAdvancementState,
    PipelineAdvancementStatus,
    PipelineBlocker,
    SucceededWorkOutput,
)


@dataclass(frozen=True, slots=True)
class PipelineDiscoveryFailure:
    source_work_unit_id: UUID
    code: str
    message: str
    required_action: str
    context: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SuccessfulWorkDiscoveryBatch:
    outputs: tuple[SucceededWorkOutput, ...]
    failures: tuple[PipelineDiscoveryFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class PipelineSynchronizationResult:
    registered: tuple[PipelineAdvancementStatus, ...]
    discovery_failures: tuple[PipelineDiscoveryFailure, ...]


class SuccessfulWorkDiscoveryPort(Protocol):
    def list_unregistered_succeeded(
        self,
        *,
        limit: int,
        correlation_id: str,
    ) -> SuccessfulWorkDiscoveryBatch: ...


class PipelineTransitionPreviewer(Protocol):
    def preview_result_digest(
        self,
        source_work_unit_id: UUID,
        transition_plan_digest: str,
        *,
        correlation_id: str,
    ) -> str: ...


class PipelinePreviewBlocked(Exception):
    def __init__(self, blocker: PipelineBlocker) -> None:
        self.blocker = blocker
        super().__init__(blocker.message)


@dataclass(frozen=True, slots=True)
class PipelineSupervisorTick:
    registered_count: int
    claimed_advancement_id: UUID | None
    terminal_status: PipelineAdvancementStatus | None
    discovery_failures: tuple[PipelineDiscoveryFailure, ...] = ()

    def __post_init__(self) -> None:
        if self.registered_count < 0:
            raise ValueError("registered advancement count cannot be negative")
        if self.claimed_advancement_id is None and self.terminal_status is not None:
            raise ValueError("unclaimed supervisor tick cannot contain terminal status")
        if self.terminal_status is not None:
            if self.terminal_status.advancement_id != self.claimed_advancement_id:
                raise ValueError("supervisor terminal status belongs to another advancement")
            if self.terminal_status.state not in {
                PipelineAdvancementState.APPLIED,
                PipelineAdvancementState.BLOCKED,
            }:
                raise ValueError("supervisor terminal status must be applied or blocked")


class PipelineSupervisorService:
    def __init__(
        self,
        advancement: PipelineAdvancementService,
        discovery: SuccessfulWorkDiscoveryPort,
        previewers: Mapping[str, PipelineTransitionPreviewer],
    ) -> None:
        self._advancement = advancement
        self._discovery = discovery
        self._previewers = dict(previewers)

    def synchronize(
        self,
        *,
        limit: int,
        correlation_id: str,
    ) -> PipelineSynchronizationResult:
        _require_limit(limit)
        batch = self._discovery.list_unregistered_succeeded(
            limit=limit,
            correlation_id=correlation_id,
        )
        if len(batch.outputs) + len(batch.failures) > limit:
            raise ValueError("successful work discovery exceeded its requested limit")
        source_ids = tuple(item.source_work_unit_id for item in batch.outputs)
        failure_ids = tuple(item.source_work_unit_id for item in batch.failures)
        if len(source_ids) != len(set(source_ids)) or len(failure_ids) != len(set(failure_ids)):
            raise ValueError("successful work discovery returned duplicate work identities")
        if set(source_ids) & set(failure_ids):
            raise ValueError(
                "successful work discovery reported one identity as both output and failure"
            )
        statuses = tuple(
            self._advancement.register(source, correlation_id=correlation_id)
            for source in batch.outputs
        )
        return PipelineSynchronizationResult(
            registered=statuses,
            discovery_failures=batch.failures,
        )

    def run_once(
        self,
        *,
        registration_limit: int,
        worker_id: str,
        dagster_execution_id: str,
        dagster_build_id: str,
        lease_duration: timedelta,
        correlation_id: str,
    ) -> PipelineSupervisorTick:
        synchronization = self.synchronize(
            limit=registration_limit,
            correlation_id=correlation_id,
        )
        registered = synchronization.registered
        lease = self._advancement.claim(
            ClaimPipelineAdvancement(
                worker_id=worker_id,
                dagster_execution_id=dagster_execution_id,
                dagster_build_id=dagster_build_id,
                lease_duration=lease_duration,
                correlation_id=correlation_id,
            )
        )
        if lease is None:
            return PipelineSupervisorTick(
                registered_count=len(registered),
                claimed_advancement_id=None,
                terminal_status=None,
                discovery_failures=synchronization.discovery_failures,
            )
        previewer = self._previewers.get(lease.transition_key)
        if previewer is None:
            status = self._advancement.block(
                BlockPipelineAdvancement(
                    advancement_id=lease.advancement_id,
                    expected_revision=lease.revision,
                    lease_id=lease.lease_id,
                    lease_token=lease.lease_token,
                    dagster_execution_id=lease.dagster_execution_id,
                    transition_plan_digest=lease.transition_plan_digest,
                    blocker=PipelineBlocker(
                        owner="PipelineAdvancement",
                        code="PIPELINE_PREVIEWER_UNAVAILABLE",
                        message="The leased transition has no deterministic preview owner.",
                        required_action=(
                            "Install the exact transition previewer before leasing this route."
                        ),
                        context={"transitionKey": lease.transition_key},
                    ),
                    correlation_id=correlation_id,
                )
            )
            return PipelineSupervisorTick(
                registered_count=len(registered),
                claimed_advancement_id=lease.advancement_id,
                terminal_status=status,
                discovery_failures=synchronization.discovery_failures,
            )
        try:
            result_digest = previewer.preview_result_digest(
                lease.source_work_unit_id,
                lease.transition_plan_digest,
                correlation_id=correlation_id,
            )
        except PipelinePreviewBlocked as exc:
            status = self._advancement.block(
                BlockPipelineAdvancement(
                    advancement_id=lease.advancement_id,
                    expected_revision=lease.revision,
                    lease_id=lease.lease_id,
                    lease_token=lease.lease_token,
                    dagster_execution_id=lease.dagster_execution_id,
                    transition_plan_digest=lease.transition_plan_digest,
                    blocker=exc.blocker,
                    correlation_id=correlation_id,
                )
            )
        else:
            status = self._advancement.apply(
                ApplyPipelineAdvancement(
                    advancement_id=lease.advancement_id,
                    expected_revision=lease.revision,
                    lease_id=lease.lease_id,
                    lease_token=lease.lease_token,
                    dagster_execution_id=lease.dagster_execution_id,
                    transition_plan_digest=lease.transition_plan_digest,
                    result_digest=result_digest,
                    correlation_id=correlation_id,
                )
            )
        return PipelineSupervisorTick(
            registered_count=len(registered),
            claimed_advancement_id=lease.advancement_id,
            terminal_status=status,
            discovery_failures=synchronization.discovery_failures,
        )


def _require_limit(value: int) -> None:
    if not 1 <= value <= 1_000:
        raise ValueError("pipeline registration limit must be between 1 and 1000")
