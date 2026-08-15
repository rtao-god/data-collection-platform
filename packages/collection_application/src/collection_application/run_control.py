from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from collection_contracts import owner_error
from collection_domain import (
    CollectionRunState,
    StageRunState,
    WorkStage,
    WorkUnitState,
)

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_TERMINAL_WORK_STATES = frozenset(
    {
        WorkUnitState.SUCCEEDED,
        WorkUnitState.DEAD_LETTER,
        WorkUnitState.BLOCKED_BY_POLICY,
        WorkUnitState.CANCELLED,
        WorkUnitState.SUPERSEDED,
    }
)


@dataclass(frozen=True, slots=True)
class WorkStateCount:
    state: WorkUnitState
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("work state count cannot be negative")


@dataclass(frozen=True, slots=True)
class StageRunStatus:
    stage_run_id: UUID
    stage: WorkStage
    state: StageRunState
    revision: int
    work_counts: tuple[WorkStateCount, ...]

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("stage run revision cannot be negative")
        states = tuple(item.state for item in self.work_counts)
        if len(states) != len(set(states)):
            raise ValueError("stage run work counts must contain each state at most once")


@dataclass(frozen=True, slots=True)
class CollectionRunStatus:
    run_id: UUID
    campaign_key: str
    config_bundle_digest: str
    state: CollectionRunState
    revision: int
    created_at_utc: datetime
    updated_at_utc: datetime
    stages: tuple[StageRunStatus, ...]

    def __post_init__(self) -> None:
        if not self.campaign_key:
            raise ValueError("campaign key cannot be empty")
        if _SHA256_PATTERN.fullmatch(self.config_bundle_digest) is None:
            raise ValueError("config bundle digest must be canonical SHA-256")
        if self.revision < 0:
            raise ValueError("collection run revision cannot be negative")
        _require_aware_utc("created_at_utc", self.created_at_utc)
        _require_aware_utc("updated_at_utc", self.updated_at_utc)
        if self.updated_at_utc < self.created_at_utc:
            raise ValueError("collection run update cannot precede creation")
        stage_keys = tuple(item.stage for item in self.stages)
        if len(stage_keys) != len(set(stage_keys)):
            raise ValueError("collection run status contains duplicate stages")


@dataclass(frozen=True, slots=True)
class StageCoverage:
    stage: WorkStage
    total: int
    pending: int
    leased: int
    retry_wait: int
    succeeded: int
    dead_letter: int
    blocked_by_policy: int
    cancelled: int
    superseded: int

    def __post_init__(self) -> None:
        counts = (
            self.pending,
            self.leased,
            self.retry_wait,
            self.succeeded,
            self.dead_letter,
            self.blocked_by_policy,
            self.cancelled,
            self.superseded,
        )
        if any(value < 0 for value in counts):
            raise ValueError("stage coverage counts cannot be negative")
        if self.total != sum(counts):
            raise ValueError("stage coverage total must equal the sum of work states")

    @property
    def terminal(self) -> int:
        return (
            self.succeeded
            + self.dead_letter
            + self.blocked_by_policy
            + self.cancelled
            + self.superseded
        )


@dataclass(frozen=True, slots=True)
class RunCoverageBlocker:
    code: str
    stage: WorkStage | None
    count: int
    message: str
    required_action: str

    def __post_init__(self) -> None:
        if _CODE_PATTERN.fullmatch(self.code) is None:
            raise ValueError("coverage blocker code must use canonical upper snake case")
        if self.count < 1:
            raise ValueError("coverage blocker count must be positive")
        _require_plain_text("coverage blocker message", self.message, maximum=1_000)
        _require_plain_text(
            "coverage blocker required action",
            self.required_action,
            maximum=1_000,
        )


@dataclass(frozen=True, slots=True)
class RunCoverageReport:
    run_id: UUID
    state: CollectionRunState
    revision: int
    stages: tuple[StageCoverage, ...]
    blockers: tuple[RunCoverageBlocker, ...]

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("collection run revision cannot be negative")
        stage_keys = tuple(item.stage for item in self.stages)
        if len(stage_keys) != len(set(stage_keys)):
            raise ValueError("run coverage contains duplicate stages")
        blocker_keys = tuple((item.code, item.stage) for item in self.blockers)
        if len(blocker_keys) != len(set(blocker_keys)):
            raise ValueError("run coverage contains duplicate blockers")

    @property
    def total(self) -> int:
        return sum(item.total for item in self.stages)

    @property
    def terminal(self) -> int:
        return sum(item.terminal for item in self.stages)

    @property
    def succeeded(self) -> int:
        return sum(item.succeeded for item in self.stages)


@dataclass(frozen=True, slots=True)
class TransitionCollectionRun:
    run_id: UUID
    expected_revision: int
    requested_state: CollectionRunState
    actor_id: str
    reason: str
    correlation_id: str

    def __post_init__(self) -> None:
        if self.expected_revision < 0:
            raise ValueError("expected run revision cannot be negative")
        _require_token("actor_id", self.actor_id)
        _require_token("correlation_id", self.correlation_id)
        if not 1 <= len(self.reason) <= 1_000:
            raise ValueError("run transition reason must contain between 1 and 1000 characters")
        if "<" in self.reason or ">" in self.reason:
            raise ValueError("run transition reason must be plain text")


class RunControlConflict(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: Mapping[str, object],
        required_action: str,
    ) -> None:
        self.code = code
        self.message = message
        self.context = dict(context)
        self.required_action = required_action
        super().__init__(message)


class RunControlPort(Protocol):
    def get(self, run_id: UUID) -> CollectionRunStatus: ...

    def coverage(self, run_id: UUID) -> RunCoverageReport: ...

    def transition(self, command: TransitionCollectionRun) -> CollectionRunStatus: ...


class RunControlService:
    def __init__(self, port: RunControlPort) -> None:
        self._port = port

    def get(self, run_id: UUID, *, correlation_id: str) -> CollectionRunStatus:
        return self._invoke(correlation_id, lambda: self._port.get(run_id))

    def coverage(self, run_id: UUID, *, correlation_id: str) -> RunCoverageReport:
        return self._invoke(correlation_id, lambda: self._port.coverage(run_id))

    def pause(
        self,
        run_id: UUID,
        *,
        expected_revision: int,
        actor_id: str,
        reason: str,
        correlation_id: str,
    ) -> CollectionRunStatus:
        return self._transition(
            run_id,
            expected_revision=expected_revision,
            requested_state=CollectionRunState.PAUSED,
            actor_id=actor_id,
            reason=reason,
            correlation_id=correlation_id,
        )

    def resume(
        self,
        run_id: UUID,
        *,
        expected_revision: int,
        actor_id: str,
        reason: str,
        correlation_id: str,
    ) -> CollectionRunStatus:
        return self._transition(
            run_id,
            expected_revision=expected_revision,
            requested_state=CollectionRunState.RUNNING,
            actor_id=actor_id,
            reason=reason,
            correlation_id=correlation_id,
        )

    def cancel(
        self,
        run_id: UUID,
        *,
        expected_revision: int,
        actor_id: str,
        reason: str,
        correlation_id: str,
    ) -> CollectionRunStatus:
        return self._transition(
            run_id,
            expected_revision=expected_revision,
            requested_state=CollectionRunState.CANCELLED,
            actor_id=actor_id,
            reason=reason,
            correlation_id=correlation_id,
        )

    def _transition(
        self,
        run_id: UUID,
        *,
        expected_revision: int,
        requested_state: CollectionRunState,
        actor_id: str,
        reason: str,
        correlation_id: str,
    ) -> CollectionRunStatus:
        command = TransitionCollectionRun(
            run_id=run_id,
            expected_revision=expected_revision,
            requested_state=requested_state,
            actor_id=actor_id,
            reason=reason,
            correlation_id=correlation_id,
        )
        return self._invoke(correlation_id, lambda: self._port.transition(command))

    @staticmethod
    def _invoke[ResultT](correlation_id: str, operation: Callable[[], ResultT]) -> ResultT:
        _require_token("correlation_id", correlation_id)
        try:
            return operation()
        except RunControlConflict as exc:
            raise owner_error(
                error_type=f"collection/{exc.code.lower().replace('_', '-')}",
                owner="RunControl",
                code=exc.code,
                message=exc.message,
                context=exc.context,
                required_action=exc.required_action,
                correlation_id=correlation_id,
            ) from exc


def coverage_from_status(status: CollectionRunStatus) -> RunCoverageReport:
    stages: list[StageCoverage] = []
    blockers: list[RunCoverageBlocker] = []
    run_blocker = _run_state_blocker(status.state)
    if run_blocker is not None:
        blockers.append(run_blocker)
    for stage in status.stages:
        counts = dict.fromkeys(WorkUnitState, 0)
        for item in stage.work_counts:
            counts[item.state] = item.count
        coverage = StageCoverage(
            stage=stage.stage,
            total=sum(counts.values()),
            pending=counts[WorkUnitState.PENDING],
            leased=counts[WorkUnitState.LEASED],
            retry_wait=counts[WorkUnitState.RETRY_WAIT],
            succeeded=counts[WorkUnitState.SUCCEEDED],
            dead_letter=counts[WorkUnitState.DEAD_LETTER],
            blocked_by_policy=counts[WorkUnitState.BLOCKED_BY_POLICY],
            cancelled=counts[WorkUnitState.CANCELLED],
            superseded=counts[WorkUnitState.SUPERSEDED],
        )
        stages.append(coverage)
        stage_blocker = _stage_state_blocker(stage.stage, stage.state)
        if stage_blocker is not None:
            blockers.append(stage_blocker)
        if coverage.dead_letter:
            blockers.append(
                RunCoverageBlocker(
                    code="WORK_DEAD_LETTERED",
                    stage=stage.stage,
                    count=coverage.dead_letter,
                    message="Work exhausted its retry budget and entered the dead-letter state.",
                    required_action=(
                        "Inspect the classified failures and explicitly reprocess or resolve them."
                    ),
                )
            )
        if coverage.blocked_by_policy:
            blockers.append(
                RunCoverageBlocker(
                    code="WORK_BLOCKED_BY_POLICY",
                    stage=stage.stage,
                    count=coverage.blocked_by_policy,
                    message="Work is terminally blocked by the active collection policy.",
                    required_action=(
                        "Review the source policy and publish a valid new policy revision before "
                        "creating replacement work."
                    ),
                )
            )
    return RunCoverageReport(
        run_id=status.run_id,
        state=status.state,
        revision=status.revision,
        stages=tuple(stages),
        blockers=tuple(blockers),
    )


def is_terminal_work_state(state: WorkUnitState) -> bool:
    return state in _TERMINAL_WORK_STATES


def _require_token(name: str, value: str) -> None:
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid token format")


def _require_plain_text(name: str, value: str, *, maximum: int) -> None:
    if not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} must contain between 1 and {maximum} characters")
    if "<" in value or ">" in value:
        raise ValueError(f"{name} must not contain markup delimiters")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ValueError(f"{name} contains a forbidden control character")


def _require_aware_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _run_state_blocker(state: CollectionRunState) -> RunCoverageBlocker | None:
    details = {
        CollectionRunState.CREATED: (
            "RUN_NOT_STARTED",
            "The collection run has not started.",
            "Start the run after validating its exact configuration snapshot.",
        ),
        CollectionRunState.PAUSED: (
            "RUN_PAUSED",
            "The collection run is paused and cannot issue new leases.",
            "Resolve the operator pause reason and resume against the current revision.",
        ),
        CollectionRunState.BLOCKED: (
            "RUN_BLOCKED",
            "The collection run is blocked by an unresolved owner condition.",
            "Resolve the recorded owner blocker before creating replacement work.",
        ),
        CollectionRunState.CANCELLED: (
            "RUN_CANCELLED",
            "The collection run was cancelled and cannot continue.",
            "Create a new run from an exact validated snapshot when collection must restart.",
        ),
    }.get(state)
    if details is None:
        return None
    code, message, required_action = details
    return RunCoverageBlocker(
        code=code,
        stage=None,
        count=1,
        message=message,
        required_action=required_action,
    )


def _stage_state_blocker(
    stage: WorkStage,
    state: StageRunState,
) -> RunCoverageBlocker | None:
    details = {
        StageRunState.FAILED: (
            "STAGE_FAILED",
            "The stage terminated with an owner-classified failure.",
            "Inspect the stage failure and explicitly create replacement work after resolution.",
        ),
        StageRunState.BLOCKED: (
            "STAGE_BLOCKED",
            "The stage is blocked by an unresolved owner condition.",
            "Resolve the stage blocker before advancing the pipeline.",
        ),
        StageRunState.CANCELLED: (
            "STAGE_CANCELLED",
            "The stage was cancelled and will not advance.",
            "Create a new run or explicitly reprocess the affected input when collection must "
            "continue.",
        ),
    }.get(state)
    if details is None:
        return None
    code, message, required_action = details
    return RunCoverageBlocker(
        code=code,
        stage=stage,
        count=1,
        message=message,
        required_action=required_action,
    )
