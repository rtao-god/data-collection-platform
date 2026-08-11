from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from collection_contracts import owner_error
from collection_domain import (
    CollectionRunState,
    RetryPolicy,
    SourceOperationalState,
    WorkCapability,
    WorkFailureKind,
    WorkLease,
    WorkStage,
    WorkUnitState,
    capability_belongs_to_stage,
)

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")


class WorkerRegistrationStatus(StrEnum):
    REGISTERED = "registered"
    ALREADY_REGISTERED = "already_registered"


class WorkCompletionStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"


@dataclass(frozen=True, slots=True)
class WorkerRegistration:
    worker_id: str
    build_identity: str
    capabilities: frozenset[WorkCapability]
    max_concurrency: int
    resource_profile: str
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("worker_id", self.worker_id)
        _require_token("build_identity", self.build_identity)
        _require_token("resource_profile", self.resource_profile)
        _require_token("correlation_id", self.correlation_id)
        if not self.capabilities:
            raise ValueError("worker registration requires at least one capability")
        if not 1 <= self.max_concurrency <= 10_000:
            raise ValueError("worker max concurrency must be between 1 and 10000")


@dataclass(frozen=True, slots=True)
class SourceCapacitySpec:
    source_key: str
    policy_digest: str
    state: SourceOperationalState
    max_active_requests: int
    minimum_interval_milliseconds: int
    correlation_id: str

    def __post_init__(self) -> None:
        _require_key("source_key", self.source_key)
        _require_digest("policy_digest", self.policy_digest)
        _require_token("correlation_id", self.correlation_id)
        if not 1 <= self.max_active_requests <= 10_000:
            raise ValueError("source max active requests must be between 1 and 10000")
        if not 0 <= self.minimum_interval_milliseconds <= 86_400_000:
            raise ValueError("source minimum interval is outside the supported range")


@dataclass(frozen=True, slots=True)
class CollectionRunSpec:
    run_id: UUID
    campaign_key: str
    config_bundle_digest: str
    initial_state: CollectionRunState
    correlation_id: str

    def __post_init__(self) -> None:
        _require_key("campaign_key", self.campaign_key)
        _require_digest("config_bundle_digest", self.config_bundle_digest)
        _require_token("correlation_id", self.correlation_id)
        if self.initial_state not in {CollectionRunState.CREATED, CollectionRunState.RUNNING}:
            raise ValueError("new collection run must start as created or running")


@dataclass(frozen=True, slots=True)
class StageRunSpec:
    stage_run_id: UUID
    run_id: UUID
    stage: WorkStage
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("correlation_id", self.correlation_id)


@dataclass(frozen=True, slots=True)
class WorkUnitSpec:
    work_id: UUID
    run_id: UUID
    stage_run_id: UUID
    stage: WorkStage
    capability: WorkCapability
    source_key: str | None
    semantic_key: str
    input_digest: str
    expected_output_contract: str
    priority: int
    retry_policy: RetryPolicy
    correlation_id: str

    def __post_init__(self) -> None:
        if not capability_belongs_to_stage(self.stage, self.capability):
            raise ValueError("work capability is not valid for the stage")
        if self.source_key is not None:
            _require_key("source_key", self.source_key)
        _require_digest("semantic_key", self.semantic_key)
        _require_digest("input_digest", self.input_digest)
        _require_token("expected_output_contract", self.expected_output_contract)
        _require_token("correlation_id", self.correlation_id)
        if not -1_000_000 <= self.priority <= 1_000_000:
            raise ValueError("work priority is outside the supported range")


@dataclass(frozen=True, slots=True)
class LeaseRequest:
    worker_id: str
    capability: WorkCapability
    lease_duration_seconds: int
    heartbeat_interval_seconds: int
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("worker_id", self.worker_id)
        _require_token("correlation_id", self.correlation_id)
        _require_lease_timing(self.lease_duration_seconds, self.heartbeat_interval_seconds)


@dataclass(frozen=True, slots=True)
class LeaseHeartbeat:
    work_id: UUID
    lease_id: UUID
    lease_token: UUID
    worker_id: str
    input_digest: str
    lease_duration_seconds: int
    heartbeat_interval_seconds: int
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("worker_id", self.worker_id)
        _require_digest("input_digest", self.input_digest)
        _require_token("correlation_id", self.correlation_id)
        _require_lease_timing(self.lease_duration_seconds, self.heartbeat_interval_seconds)


@dataclass(frozen=True, slots=True)
class WorkCompletion:
    work_id: UUID
    lease_id: UUID
    lease_token: UUID
    worker_id: str
    input_digest: str
    output_contract: str
    output_digest: str
    worker_build_identity: str
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("worker_id", self.worker_id)
        _require_digest("input_digest", self.input_digest)
        _require_token("output_contract", self.output_contract)
        _require_digest("output_digest", self.output_digest)
        _require_token("worker_build_identity", self.worker_build_identity)
        _require_token("correlation_id", self.correlation_id)


@dataclass(frozen=True, slots=True)
class WorkFailure:
    work_id: UUID
    lease_id: UUID
    lease_token: UUID
    worker_id: str
    input_digest: str
    failure_kind: WorkFailureKind
    code: str
    owner: str
    message: str
    required_action: str
    worker_build_identity: str
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("worker_id", self.worker_id)
        _require_digest("input_digest", self.input_digest)
        if _CODE_PATTERN.fullmatch(self.code) is None:
            raise ValueError("work failure code has an invalid format")
        _require_text("owner", self.owner, 100)
        _require_text("message", self.message, 500)
        _require_text("required_action", self.required_action, 500)
        _require_token("worker_build_identity", self.worker_build_identity)
        _require_token("correlation_id", self.correlation_id)


@dataclass(frozen=True, slots=True)
class WorkRelease:
    work_id: UUID
    lease_id: UUID
    lease_token: UUID
    worker_id: str
    input_digest: str
    reason_code: str
    worker_build_identity: str
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("worker_id", self.worker_id)
        _require_digest("input_digest", self.input_digest)
        if _CODE_PATTERN.fullmatch(self.reason_code) is None:
            raise ValueError("work release reason code has an invalid format")
        _require_token("worker_build_identity", self.worker_build_identity)
        _require_token("correlation_id", self.correlation_id)


@dataclass(frozen=True, slots=True)
class WorkerRegistrationResult:
    worker_id: str
    status: WorkerRegistrationStatus


@dataclass(frozen=True, slots=True)
class WorkCompletionResult:
    work_id: UUID
    status: WorkCompletionStatus
    output_digest: str
    revision: int


@dataclass(frozen=True, slots=True)
class WorkMutationResult:
    work_id: UUID
    state: WorkUnitState
    revision: int
    available_at_utc: datetime | None


class WorkEngineConflict(Exception):
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


class WorkEnginePort(Protocol):
    def register_worker(self, command: WorkerRegistration) -> WorkerRegistrationResult: ...

    def configure_source(self, command: SourceCapacitySpec) -> None: ...

    def create_run(self, command: CollectionRunSpec) -> None: ...

    def create_stage(self, command: StageRunSpec) -> None: ...

    def enqueue_work(self, command: WorkUnitSpec) -> None: ...

    def acquire_lease(self, command: LeaseRequest) -> WorkLease | None: ...

    def heartbeat(self, command: LeaseHeartbeat) -> WorkLease: ...

    def complete(self, command: WorkCompletion) -> WorkCompletionResult: ...

    def fail(self, command: WorkFailure) -> WorkMutationResult: ...

    def release(self, command: WorkRelease) -> WorkMutationResult: ...


class WorkEngineService:
    def __init__(self, port: WorkEnginePort) -> None:
        self._port = port

    def register_worker(self, command: WorkerRegistration) -> WorkerRegistrationResult:
        return self._invoke(command.correlation_id, lambda: self._port.register_worker(command))

    def configure_source(self, command: SourceCapacitySpec) -> None:
        self._invoke(command.correlation_id, lambda: self._port.configure_source(command))

    def create_run(self, command: CollectionRunSpec) -> None:
        self._invoke(command.correlation_id, lambda: self._port.create_run(command))

    def create_stage(self, command: StageRunSpec) -> None:
        self._invoke(command.correlation_id, lambda: self._port.create_stage(command))

    def enqueue_work(self, command: WorkUnitSpec) -> None:
        self._invoke(command.correlation_id, lambda: self._port.enqueue_work(command))

    def acquire_lease(self, command: LeaseRequest) -> WorkLease | None:
        return self._invoke(command.correlation_id, lambda: self._port.acquire_lease(command))

    def heartbeat(self, command: LeaseHeartbeat) -> WorkLease:
        return self._invoke(command.correlation_id, lambda: self._port.heartbeat(command))

    def complete(self, command: WorkCompletion) -> WorkCompletionResult:
        return self._invoke(command.correlation_id, lambda: self._port.complete(command))

    def fail(self, command: WorkFailure) -> WorkMutationResult:
        return self._invoke(command.correlation_id, lambda: self._port.fail(command))

    def release(self, command: WorkRelease) -> WorkMutationResult:
        return self._invoke(command.correlation_id, lambda: self._port.release(command))

    @staticmethod
    def _invoke[ResultT](correlation_id: str, operation: Callable[[], ResultT]) -> ResultT:
        try:
            return operation()
        except WorkEngineConflict as exc:
            error_type = f"collection/{exc.code.lower().replace('_', '-')}"
            raise owner_error(
                error_type=error_type,
                owner="WorkEngine",
                code=exc.code,
                message=exc.message,
                context=exc.context,
                required_action=exc.required_action,
                correlation_id=correlation_id,
            ) from exc


def _require_key(name: str, value: str) -> None:
    if _KEY_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid key format")


def _require_digest(name: str, value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be canonical SHA-256")


def _require_token(name: str, value: str) -> None:
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid token format")


def _require_text(name: str, value: str, maximum_length: int) -> None:
    if not value.strip() or len(value) > maximum_length:
        raise ValueError(f"{name} must be non-empty and at most {maximum_length} characters")


def _require_lease_timing(lease_duration_seconds: int, heartbeat_interval_seconds: int) -> None:
    if not 5 <= lease_duration_seconds <= 86_400:
        raise ValueError("lease duration must be between 5 and 86400 seconds")
    if not 1 <= heartbeat_interval_seconds < lease_duration_seconds:
        raise ValueError("heartbeat interval must be positive and below lease duration")
