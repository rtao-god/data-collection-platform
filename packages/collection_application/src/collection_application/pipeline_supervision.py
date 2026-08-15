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


class SuccessfulWorkDiscoveryPort(Protocol):
    def list_unregistered_succeeded(
        self,
        *,
        limit: int,
        correlation_id: str,
    ) -> tuple[SucceededWorkOutput, ...]: ...


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
    ) -> tuple[PipelineAdvancementStatus, ...]:
        _require_limit(limit)
        sources = self._discovery.list_unregistered_succeeded(
            limit=limit,
            correlation_id=correlation_id,
        )
        if len(sources) > limit:
            raise ValueError("successful work discovery exceeded its requested limit")
        source_ids = tuple(item.source_work_unit_id for item in sources)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("successful work discovery returned duplicate work identities")
        return tuple(
            self._advancement.register(source, correlation_id=correlation_id) for source in sources
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
        registered = self.synchronize(
            limit=registration_limit,
            correlation_id=correlation_id,
        )
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
        )


def _require_limit(value: int) -> None:
    if not 1 <= value <= 1_000:
        raise ValueError("pipeline registration limit must be between 1 and 1000")
