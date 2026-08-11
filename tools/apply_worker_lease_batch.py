from __future__ import annotations

from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]


def write(relative: str, content: str) -> None:
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip(), encoding="utf-8")


run_admission_owner = (
    ROOT
    / "packages/collection_application/src/collection_application/run_admission.py"
)
if not run_admission_owner.is_file():
    raise RuntimeError("RunAdmission owner must be present before operational lease work")

write(
    "packages/collection_application/src/collection_application/work_engine.py",
    r'''
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
        SourcePermit,
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
            if self.initial_state not in {
                CollectionRunState.CREATED,
                CollectionRunState.RUNNING,
            }:
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
            _require_lease_timing(
                self.lease_duration_seconds,
                self.heartbeat_interval_seconds,
            )


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
            _require_lease_timing(
                self.lease_duration_seconds,
                self.heartbeat_interval_seconds,
            )


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
    class LeaseExpirySweep:
        limit: int
        correlation_id: str

        def __post_init__(self) -> None:
            if not 1 <= self.limit <= 10_000:
                raise ValueError("lease expiry sweep limit must be between 1 and 10000")
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


    @dataclass(frozen=True, slots=True)
    class LeaseExpiryResult:
        processed_count: int
        retry_scheduled_count: int
        dead_lettered_count: int


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
        def register_worker(
            self,
            command: WorkerRegistration,
        ) -> WorkerRegistrationResult: ...

        def configure_source(self, command: SourceCapacitySpec) -> None: ...

        def acquire_lease(self, command: LeaseRequest) -> WorkLease | None: ...

        def heartbeat(self, command: LeaseHeartbeat) -> WorkLease: ...

        def complete(self, command: WorkCompletion) -> WorkCompletionResult: ...

        def fail(self, command: WorkFailure) -> WorkMutationResult: ...

        def release(self, command: WorkRelease) -> WorkMutationResult: ...

        def expire_leases(self, command: LeaseExpirySweep) -> LeaseExpiryResult: ...


    class WorkEngineService:
        def __init__(self, port: WorkEnginePort) -> None:
            self._port = port

        def register_worker(
            self,
            command: WorkerRegistration,
        ) -> WorkerRegistrationResult:
            return self._invoke(
                command.correlation_id,
                lambda: self._port.register_worker(command),
            )

        def configure_source(self, command: SourceCapacitySpec) -> None:
            self._invoke(
                command.correlation_id,
                lambda: self._port.configure_source(command),
            )

        def acquire_lease(self, command: LeaseRequest) -> WorkLease | None:
            return self._invoke(
                command.correlation_id,
                lambda: self._port.acquire_lease(command),
            )

        def heartbeat(self, command: LeaseHeartbeat) -> WorkLease:
            return self._invoke(
                command.correlation_id,
                lambda: self._port.heartbeat(command),
            )

        def complete(self, command: WorkCompletion) -> WorkCompletionResult:
            return self._invoke(
                command.correlation_id,
                lambda: self._port.complete(command),
            )

        def fail(self, command: WorkFailure) -> WorkMutationResult:
            return self._invoke(
                command.correlation_id,
                lambda: self._port.fail(command),
            )

        def release(self, command: WorkRelease) -> WorkMutationResult:
            return self._invoke(
                command.correlation_id,
                lambda: self._port.release(command),
            )

        def expire_leases(self, command: LeaseExpirySweep) -> LeaseExpiryResult:
            return self._invoke(
                command.correlation_id,
                lambda: self._port.expire_leases(command),
            )

        @staticmethod
        def _invoke[ResultT](
            correlation_id: str,
            operation: Callable[[], ResultT],
        ) -> ResultT:
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
            raise ValueError(
                f"{name} must be non-empty and at most {maximum_length} characters"
            )


    def _require_lease_timing(
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> None:
        if not 5 <= lease_duration_seconds <= 86_400:
            raise ValueError("lease duration must be between 5 and 86400 seconds")
        if not 1 <= heartbeat_interval_seconds < lease_duration_seconds:
            raise ValueError("heartbeat interval must be positive and below lease duration")
    ''',
)

write(
    "packages/collection_application/src/collection_application/__init__.py",
    r'''
    from collection_application.campaign_snapshot_service import CampaignSnapshotService
    from collection_application.ports import CampaignBundleSource, RawCampaignBundle
    from collection_application.run_admission import (
        RunAdmissionConflict,
        RunAdmissionPlan,
        RunAdmissionPort,
        RunAdmissionResult,
        RunAdmissionService,
        RunAdmissionStatus,
        run_admission_plan_from_payload,
    )
    from collection_application.work_engine import (
        CollectionRunSpec,
        LeaseExpiryResult,
        LeaseExpirySweep,
        LeaseHeartbeat,
        LeaseRequest,
        SourceCapacitySpec,
        StageRunSpec,
        WorkCompletion,
        WorkCompletionResult,
        WorkCompletionStatus,
        WorkEngineConflict,
        WorkEnginePort,
        WorkEngineService,
        WorkerRegistration,
        WorkerRegistrationResult,
        WorkerRegistrationStatus,
        WorkFailure,
        WorkMutationResult,
        WorkRelease,
        WorkUnitSpec,
    )
    from collection_domain import (
        CollectionRunState,
        RetryPolicy,
        SourceOperationalState,
        SourcePermit,
        StageRunState,
        WorkAttemptOutcome,
        WorkCapability,
        WorkFailureKind,
        WorkLease,
        WorkStage,
        WorkUnitState,
        capability_belongs_to_stage,
    )

    __all__ = [
        "CampaignBundleSource",
        "CampaignSnapshotService",
        "CollectionRunSpec",
        "CollectionRunState",
        "LeaseExpiryResult",
        "LeaseExpirySweep",
        "LeaseHeartbeat",
        "LeaseRequest",
        "RawCampaignBundle",
        "RetryPolicy",
        "RunAdmissionConflict",
        "RunAdmissionPlan",
        "RunAdmissionPort",
        "RunAdmissionResult",
        "RunAdmissionService",
        "RunAdmissionStatus",
        "SourceCapacitySpec",
        "SourceOperationalState",
        "SourcePermit",
        "StageRunSpec",
        "StageRunState",
        "WorkAttemptOutcome",
        "WorkCapability",
        "WorkCompletion",
        "WorkCompletionResult",
        "WorkCompletionStatus",
        "WorkEngineConflict",
        "WorkEnginePort",
        "WorkEngineService",
        "WorkFailure",
        "WorkFailureKind",
        "WorkLease",
        "WorkMutationResult",
        "WorkRelease",
        "WorkStage",
        "WorkUnitSpec",
        "WorkUnitState",
        "WorkerRegistration",
        "WorkerRegistrationResult",
        "WorkerRegistrationStatus",
        "capability_belongs_to_stage",
        "run_admission_plan_from_payload",
    ]
    ''',
)

write(
    "packages/collection_infrastructure/src/collection_infrastructure/postgres/work_engine.py",
    r'''
    from __future__ import annotations

    import json
    from collections.abc import Callable, Mapping
    from datetime import UTC, datetime, timedelta
    from hashlib import sha256
    from typing import Self, cast
    from uuid import UUID, uuid4

    import sqlalchemy as sa
    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.sql.schema import Table

    from collection_application import (
        LeaseExpiryResult,
        LeaseExpirySweep,
        LeaseHeartbeat,
        LeaseRequest,
        RetryPolicy,
        SourceCapacitySpec,
        SourceOperationalState,
        SourcePermit,
        WorkCapability,
        WorkCompletion,
        WorkCompletionResult,
        WorkCompletionStatus,
        WorkEngineConflict,
        WorkerRegistration,
        WorkerRegistrationResult,
        WorkerRegistrationStatus,
        WorkFailure,
        WorkFailureKind,
        WorkLease,
        WorkMutationResult,
        WorkRelease,
        WorkStage,
        WorkUnitState,
    )
    from collection_infrastructure.postgres.work_metadata import (
        collection_runs,
        dead_letters,
        source_capacity_states,
        stage_runs,
        work_attempts,
        work_units,
        worker_capabilities,
        worker_heartbeats,
        worker_registrations,
    )

    _ELIGIBLE_WORK_STATES = ("pending", "retry_wait")
    _ELIGIBLE_STAGE_STATES = ("pending", "running")


    class PostgresWorkEngineStore:
        def __init__(
            self,
            engine: Engine,
            *,
            clock: Callable[[], datetime] | None = None,
            uuid_factory: Callable[[], UUID] | None = None,
        ) -> None:
            self._engine = engine
            self._clock = clock or _utc_now
            self._uuid_factory = uuid_factory or uuid4

        @classmethod
        def from_url(
            cls,
            database_url: str,
            *,
            clock: Callable[[], datetime] | None = None,
            uuid_factory: Callable[[], UUID] | None = None,
        ) -> Self:
            return cls(
                sa.create_engine(database_url, pool_pre_ping=True),
                clock=clock,
                uuid_factory=uuid_factory,
            )

        def register_worker(
            self,
            command: WorkerRegistration,
        ) -> WorkerRegistrationResult:
            return self._execute(
                command.correlation_id,
                lambda connection, now_utc: self._register_worker(
                    connection,
                    command,
                    now_utc,
                ),
            )

        def configure_source(self, command: SourceCapacitySpec) -> None:
            self._execute(
                command.correlation_id,
                lambda connection, now_utc: self._configure_source(
                    connection,
                    command,
                    now_utc,
                ),
            )

        def acquire_lease(self, command: LeaseRequest) -> WorkLease | None:
            return self._execute(
                command.correlation_id,
                lambda connection, now_utc: self._acquire_lease(
                    connection,
                    command,
                    now_utc,
                ),
            )

        def heartbeat(self, command: LeaseHeartbeat) -> WorkLease:
            return self._execute(
                command.correlation_id,
                lambda connection, now_utc: self._heartbeat(
                    connection,
                    command,
                    now_utc,
                ),
            )

        def complete(self, command: WorkCompletion) -> WorkCompletionResult:
            return self._execute(
                command.correlation_id,
                lambda connection, now_utc: self._complete(
                    connection,
                    command,
                    now_utc,
                ),
            )

        def fail(self, command: WorkFailure) -> WorkMutationResult:
            return self._execute(
                command.correlation_id,
                lambda connection, now_utc: self._fail(
                    connection,
                    command,
                    now_utc,
                ),
            )

        def release(self, command: WorkRelease) -> WorkMutationResult:
            return self._execute(
                command.correlation_id,
                lambda connection, now_utc: self._release(
                    connection,
                    command,
                    now_utc,
                ),
            )

        def expire_leases(self, command: LeaseExpirySweep) -> LeaseExpiryResult:
            return self._execute(
                command.correlation_id,
                lambda connection, now_utc: self._expire_leases(
                    connection,
                    command,
                    now_utc,
                ),
            )

        def _execute[ResultT](
            self,
            correlation_id: str,
            operation: Callable[[Connection, datetime], ResultT],
        ) -> ResultT:
            now_utc = self._clock()
            _require_utc(now_utc)
            try:
                with self._engine.begin() as connection:
                    return operation(connection, now_utc)
            except WorkEngineConflict:
                raise
            except sa.exc.IntegrityError as exc:
                raise WorkEngineConflict(
                    code="WORK_ENGINE_STORAGE_CONFLICT",
                    message="PostgreSQL rejected the atomic work-engine transition.",
                    context={"constraint": _constraint_name(exc)},
                    required_action=(
                        "Inspect the conflicting durable owner record before retrying the command."
                    ),
                ) from exc
            except sa.exc.DBAPIError as exc:
                raise WorkEngineConflict(
                    code="WORK_ENGINE_STORAGE_UNAVAILABLE",
                    message="PostgreSQL could not complete the work-engine transaction.",
                    context={"databaseError": type(exc.orig).__name__},
                    required_action=(
                        "Restore the control-plane database and retry with the same correlation id."
                    ),
                ) from exc

        def _register_worker(
            self,
            connection: Connection,
            command: WorkerRegistration,
            now_utc: datetime,
        ) -> WorkerRegistrationResult:
            _advisory_lock(connection, f"worker:{command.worker_id}")
            registration_digest = _registration_digest(command)
            existing = connection.execute(
                sa.select(worker_registrations)
                .where(worker_registrations.c.worker_id == command.worker_id)
                .with_for_update()
            ).mappings().one_or_none()
            if existing is not None:
                capabilities = frozenset(
                    WorkCapability(value)
                    for value in connection.scalars(
                        sa.select(worker_capabilities.c.capability).where(
                            worker_capabilities.c.worker_id == command.worker_id
                        )
                    )
                )
                if (
                    existing["registration_digest"] == registration_digest
                    and existing["build_identity"] == command.build_identity
                    and existing["max_concurrency"] == command.max_concurrency
                    and existing["resource_profile"] == command.resource_profile
                    and capabilities == command.capabilities
                ):
                    return WorkerRegistrationResult(
                        worker_id=command.worker_id,
                        status=WorkerRegistrationStatus.ALREADY_REGISTERED,
                    )
                raise _conflict(
                    "WORKER_REGISTRATION_CONFLICT",
                    "The worker id is already owned by another registration contract.",
                    {
                        "workerId": command.worker_id,
                        "storedRegistrationDigest": existing["registration_digest"],
                        "requestedRegistrationDigest": registration_digest,
                    },
                    "Use a new worker id or restore the exact registered build contract.",
                )

            connection.execute(
                worker_registrations.insert().values(
                    worker_id=command.worker_id,
                    registration_digest=registration_digest,
                    build_identity=command.build_identity,
                    max_concurrency=command.max_concurrency,
                    resource_profile=command.resource_profile,
                    registered_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
            )
            connection.execute(
                worker_capabilities.insert(),
                [
                    {
                        "worker_id": command.worker_id,
                        "capability": capability.value,
                    }
                    for capability in sorted(
                        command.capabilities,
                        key=lambda value: value.value,
                    )
                ],
            )
            connection.execute(
                worker_heartbeats.insert().values(
                    worker_id=command.worker_id,
                    last_seen_at_utc=now_utc,
                    active_lease_count=0,
                    correlation_id=command.correlation_id,
                )
            )
            return WorkerRegistrationResult(
                worker_id=command.worker_id,
                status=WorkerRegistrationStatus.REGISTERED,
            )

        def _configure_source(
            self,
            connection: Connection,
            command: SourceCapacitySpec,
            now_utc: datetime,
        ) -> None:
            _advisory_lock(connection, f"source:{command.source_key}")
            existing = connection.execute(
                sa.select(source_capacity_states)
                .where(source_capacity_states.c.source_key == command.source_key)
                .with_for_update()
            ).mappings().one_or_none()
            if existing is None:
                connection.execute(
                    source_capacity_states.insert().values(
                        source_key=command.source_key,
                        policy_digest=command.policy_digest,
                        operational_state=command.state.value,
                        max_active_requests=command.max_active_requests,
                        active_requests=0,
                        minimum_interval_milliseconds=(
                            command.minimum_interval_milliseconds
                        ),
                        next_allowed_request_at_utc=now_utc,
                        retry_after_utc=None,
                        revision=0,
                        updated_at_utc=now_utc,
                        correlation_id=command.correlation_id,
                    )
                )
                return

            changed = (
                existing["policy_digest"] != command.policy_digest
                or existing["operational_state"] != command.state.value
                or existing["max_active_requests"] != command.max_active_requests
                or existing["minimum_interval_milliseconds"]
                != command.minimum_interval_milliseconds
            )
            if changed and existing["active_requests"] != 0:
                raise _conflict(
                    "SOURCE_CAPACITY_ACTIVE_CONFLICT",
                    "Source capacity cannot change while permits are active.",
                    {
                        "sourceKey": command.source_key,
                        "activeRequests": existing["active_requests"],
                    },
                    "Wait for active leases to finish or expire before changing the policy.",
                )
            if not changed:
                return
            connection.execute(
                source_capacity_states.update()
                .where(source_capacity_states.c.source_key == command.source_key)
                .values(
                    policy_digest=command.policy_digest,
                    operational_state=command.state.value,
                    max_active_requests=command.max_active_requests,
                    minimum_interval_milliseconds=(
                        command.minimum_interval_milliseconds
                    ),
                    retry_after_utc=None,
                    revision=source_capacity_states.c.revision + 1,
                    updated_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
            )

        def _acquire_lease(
            self,
            connection: Connection,
            command: LeaseRequest,
            now_utc: datetime,
        ) -> WorkLease | None:
            worker = self._worker_for_update(connection, command.worker_id)
            capabilities = frozenset(
                connection.scalars(
                    sa.select(worker_capabilities.c.capability).where(
                        worker_capabilities.c.worker_id == command.worker_id
                    )
                )
            )
            if command.capability.value not in capabilities:
                raise _conflict(
                    "WORKER_CAPABILITY_NOT_REGISTERED",
                    "The worker requested a capability outside its registration.",
                    {
                        "workerId": command.worker_id,
                        "capability": command.capability.value,
                    },
                    "Register a worker identity that explicitly owns this capability.",
                )
            heartbeat = self._worker_heartbeat_for_update(connection, command.worker_id)
            actual_active = connection.scalar(
                sa.select(sa.func.count())
                .select_from(work_units)
                .where(
                    work_units.c.state == "leased",
                    work_units.c.active_worker_id == command.worker_id,
                )
            )
            if heartbeat["active_lease_count"] != actual_active:
                raise _conflict(
                    "WORKER_LEASE_COUNT_DRIFT",
                    "The worker heartbeat count differs from durable active leases.",
                    {
                        "workerId": command.worker_id,
                        "recorded": heartbeat["active_lease_count"],
                        "actual": actual_active,
                    },
                    "Run the reviewed work-engine reconciliation before issuing another lease.",
                )
            if actual_active >= worker["max_concurrency"]:
                return None

            candidates = connection.execute(
                sa.select(work_units)
                .join(
                    collection_runs,
                    collection_runs.c.run_id == work_units.c.run_id,
                )
                .join(
                    stage_runs,
                    sa.and_(
                        stage_runs.c.stage_run_id == work_units.c.stage_run_id,
                        stage_runs.c.run_id == work_units.c.run_id,
                        stage_runs.c.stage == work_units.c.stage,
                    ),
                )
                .where(
                    work_units.c.state.in_(_ELIGIBLE_WORK_STATES),
                    work_units.c.capability == command.capability.value,
                    work_units.c.available_at_utc <= now_utc,
                    collection_runs.c.state == "running",
                    stage_runs.c.state.in_(_ELIGIBLE_STAGE_STATES),
                )
                .order_by(
                    work_units.c.priority.desc(),
                    work_units.c.available_at_utc,
                    work_units.c.created_at_utc,
                    work_units.c.work_id,
                )
                .limit(50)
                .with_for_update(of=work_units, skip_locked=True)
            ).mappings()

            for work in candidates:
                source = None
                permit = None
                if work["source_key"] is not None:
                    source = connection.execute(
                        sa.select(source_capacity_states)
                        .where(
                            source_capacity_states.c.source_key == work["source_key"]
                        )
                        .with_for_update()
                    ).mappings().one_or_none()
                    if source is None:
                        raise _conflict(
                            "WORK_SOURCE_CAPACITY_MISSING",
                            "A source-bound work unit has no durable capacity owner.",
                            {
                                "workId": str(work["work_id"]),
                                "sourceKey": work["source_key"],
                            },
                            "Configure the source owner before retrying lease acquisition.",
                        )
                    if source["operational_state"] != SourceOperationalState.ACTIVE.value:
                        continue
                    permit_not_before = max(
                        value
                        for value in (
                            now_utc,
                            source["next_allowed_request_at_utc"],
                            source["retry_after_utc"],
                        )
                        if value is not None
                    )
                    if permit_not_before > now_utc:
                        continue
                    if source["active_requests"] >= source["max_active_requests"]:
                        continue
                    permit = SourcePermit(
                        source_key=cast(str, source["source_key"]),
                        policy_digest=cast(str, source["policy_digest"]),
                        permit_not_before_utc=permit_not_before,
                    )

                lease_id = self._uuid_factory()
                lease_token = self._uuid_factory()
                attempt_id = self._uuid_factory()
                issued_at_utc = now_utc
                expires_at_utc = now_utc + timedelta(
                    seconds=command.lease_duration_seconds
                )
                heartbeat_deadline_utc = now_utc + timedelta(
                    seconds=command.heartbeat_interval_seconds
                )
                attempt_number = int(work["attempt_count"]) + 1

                if source is not None and permit is not None:
                    connection.execute(
                        source_capacity_states.update()
                        .where(
                            source_capacity_states.c.source_key == source["source_key"]
                        )
                        .values(
                            active_requests=source_capacity_states.c.active_requests + 1,
                            next_allowed_request_at_utc=now_utc
                            + timedelta(
                                milliseconds=source[
                                    "minimum_interval_milliseconds"
                                ]
                            ),
                            revision=source_capacity_states.c.revision + 1,
                            updated_at_utc=now_utc,
                            correlation_id=command.correlation_id,
                        )
                    )

                connection.execute(
                    work_units.update()
                    .where(work_units.c.work_id == work["work_id"])
                    .values(
                        state="leased",
                        attempt_count=attempt_number,
                        active_lease_id=lease_id,
                        active_lease_token=lease_token,
                        active_worker_id=command.worker_id,
                        lease_issued_at_utc=issued_at_utc,
                        lease_expires_at_utc=expires_at_utc,
                        heartbeat_deadline_utc=heartbeat_deadline_utc,
                        source_policy_digest=(
                            permit.policy_digest if permit is not None else None
                        ),
                        source_permit_not_before_utc=(
                            permit.permit_not_before_utc
                            if permit is not None
                            else None
                        ),
                        revision=work_units.c.revision + 1,
                        updated_at_utc=now_utc,
                        correlation_id=command.correlation_id,
                    )
                )
                connection.execute(
                    work_attempts.insert().values(
                        **_table_values(
                            work_attempts,
                            _attempt_candidates(
                                attempt_id=attempt_id,
                                work=work,
                                attempt_number=attempt_number,
                                lease_id=lease_id,
                                lease_token=lease_token,
                                worker_id=command.worker_id,
                                worker_build_identity=cast(str, worker["build_identity"]),
                                issued_at_utc=issued_at_utc,
                                expires_at_utc=expires_at_utc,
                                heartbeat_deadline_utc=heartbeat_deadline_utc,
                                permit=permit,
                                correlation_id=command.correlation_id,
                            ),
                        )
                    )
                )
                connection.execute(
                    worker_heartbeats.update()
                    .where(worker_heartbeats.c.worker_id == command.worker_id)
                    .values(
                        last_seen_at_utc=now_utc,
                        active_lease_count=worker_heartbeats.c.active_lease_count + 1,
                        correlation_id=command.correlation_id,
                    )
                )
                connection.execute(
                    stage_runs.update()
                    .where(
                        stage_runs.c.stage_run_id == work["stage_run_id"],
                        stage_runs.c.state == "pending",
                    )
                    .values(
                        state="running",
                        revision=stage_runs.c.revision + 1,
                        updated_at_utc=now_utc,
                        correlation_id=command.correlation_id,
                    )
                )
                return WorkLease(
                    lease_id=lease_id,
                    work_id=cast(UUID, work["work_id"]),
                    lease_token=lease_token,
                    worker_id=command.worker_id,
                    stage=WorkStage(cast(str, work["stage"])),
                    capability=WorkCapability(cast(str, work["capability"])),
                    input_digest=cast(str, work["input_digest"]),
                    expected_output_contract=cast(
                        str,
                        work["expected_output_contract"],
                    ),
                    issued_at_utc=issued_at_utc,
                    expires_at_utc=expires_at_utc,
                    heartbeat_deadline_utc=heartbeat_deadline_utc,
                    source_permit=permit,
                    correlation_id=command.correlation_id,
                )
            return None

        def _heartbeat(
            self,
            connection: Connection,
            command: LeaseHeartbeat,
            now_utc: datetime,
        ) -> WorkLease:
            work = self._active_work(connection, command, now_utc)
            expires_at_utc = now_utc + timedelta(
                seconds=command.lease_duration_seconds
            )
            heartbeat_deadline_utc = now_utc + timedelta(
                seconds=command.heartbeat_interval_seconds
            )
            connection.execute(
                work_units.update()
                .where(work_units.c.work_id == command.work_id)
                .values(
                    lease_expires_at_utc=expires_at_utc,
                    heartbeat_deadline_utc=heartbeat_deadline_utc,
                    revision=work_units.c.revision + 1,
                    updated_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
            )
            connection.execute(
                work_attempts.update()
                .where(
                    work_attempts.c.work_id == command.work_id,
                    work_attempts.c.lease_id == command.lease_id,
                    work_attempts.c.lease_token == command.lease_token,
                )
                .values(
                    **_existing_table_values(
                        work_attempts,
                        {
                            "expires_at_utc": expires_at_utc,
                            "heartbeat_deadline_utc": heartbeat_deadline_utc,
                            "correlation_id": command.correlation_id,
                        },
                    )
                )
            )
            connection.execute(
                worker_heartbeats.update()
                .where(worker_heartbeats.c.worker_id == command.worker_id)
                .values(
                    last_seen_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
            )
            return _lease_from_work(
                work,
                expires_at_utc=expires_at_utc,
                heartbeat_deadline_utc=heartbeat_deadline_utc,
                correlation_id=command.correlation_id,
            )

        def _complete(
            self,
            connection: Connection,
            command: WorkCompletion,
            now_utc: datetime,
        ) -> WorkCompletionResult:
            work = self._work_for_update(connection, command.work_id)
            if work["state"] == "succeeded":
                attempt = self._attempt_for_lease(
                    connection,
                    command.work_id,
                    command.lease_id,
                    command.lease_token,
                )
                if (
                    attempt is not None
                    and attempt["outcome"] == "succeeded"
                    and work["output_contract"] == command.output_contract
                    and work["output_digest"] == command.output_digest
                ):
                    return WorkCompletionResult(
                        work_id=command.work_id,
                        status=WorkCompletionStatus.ALREADY_APPLIED,
                        output_digest=command.output_digest,
                        revision=int(work["revision"]),
                    )
                raise _stale_conflict(work, command)

            self._require_active_identity(work, command, now_utc)
            self._require_worker_build(
                connection,
                command.worker_id,
                command.worker_build_identity,
            )
            if work["expected_output_contract"] != command.output_contract:
                raise _conflict(
                    "WORK_OUTPUT_CONTRACT_MISMATCH",
                    "The worker returned an output contract not owned by this work unit.",
                    {
                        "workId": str(command.work_id),
                        "expected": work["expected_output_contract"],
                        "actual": command.output_contract,
                    },
                    "Discard the output and execute the expected contract implementation.",
                )

            new_revision = int(work["revision"]) + 1
            connection.execute(
                work_attempts.update()
                .where(
                    work_attempts.c.work_id == command.work_id,
                    work_attempts.c.lease_id == command.lease_id,
                    work_attempts.c.lease_token == command.lease_token,
                )
                .values(
                    **_existing_table_values(
                        work_attempts,
                        {
                            "outcome": "succeeded",
                            "completed_at_utc": now_utc,
                            "output_contract": command.output_contract,
                            "output_digest": command.output_digest,
                            "worker_build_identity": command.worker_build_identity,
                            "correlation_id": command.correlation_id,
                        },
                    )
                )
            )
            connection.execute(
                work_units.update()
                .where(work_units.c.work_id == command.work_id)
                .values(
                    state="succeeded",
                    active_lease_id=None,
                    active_lease_token=None,
                    active_worker_id=None,
                    lease_issued_at_utc=None,
                    lease_expires_at_utc=None,
                    heartbeat_deadline_utc=None,
                    source_policy_digest=None,
                    source_permit_not_before_utc=None,
                    output_contract=command.output_contract,
                    output_digest=command.output_digest,
                    completed_at_utc=now_utc,
                    revision=new_revision,
                    updated_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
            )
            self._release_capacity_and_worker(
                connection,
                work,
                command.worker_id,
                command.correlation_id,
                now_utc,
            )
            self._complete_owner_lifecycles(
                connection,
                work,
                command.correlation_id,
                now_utc,
            )
            return WorkCompletionResult(
                work_id=command.work_id,
                status=WorkCompletionStatus.APPLIED,
                output_digest=command.output_digest,
                revision=new_revision,
            )

        def _fail(
            self,
            connection: Connection,
            command: WorkFailure,
            now_utc: datetime,
        ) -> WorkMutationResult:
            work = self._active_work(connection, command, now_utc)
            self._require_worker_build(
                connection,
                command.worker_id,
                command.worker_build_identity,
            )
            policy = _retry_policy(work)
            decision = policy.decide(
                command.failure_kind,
                int(work["attempt_count"]),
            )
            available_at_utc = (
                now_utc + timedelta(seconds=decision.retry_delay_seconds)
                if decision.retry_delay_seconds is not None
                else now_utc
            )
            self._record_failure(
                connection,
                work=work,
                lease_id=command.lease_id,
                lease_token=command.lease_token,
                worker_id=command.worker_id,
                worker_build_identity=command.worker_build_identity,
                failure_kind=command.failure_kind,
                code=command.code,
                owner=command.owner,
                message=command.message,
                required_action=command.required_action,
                outcome=decision.attempt_outcome.value,
                target_state=decision.target_state,
                available_at_utc=available_at_utc,
                correlation_id=command.correlation_id,
                now_utc=now_utc,
            )
            return WorkMutationResult(
                work_id=command.work_id,
                state=decision.target_state,
                revision=int(work["revision"]) + 1,
                available_at_utc=(
                    available_at_utc
                    if decision.target_state is WorkUnitState.RETRY_WAIT
                    else None
                ),
            )

        def _release(
            self,
            connection: Connection,
            command: WorkRelease,
            now_utc: datetime,
        ) -> WorkMutationResult:
            work = self._active_work(connection, command, now_utc)
            self._require_worker_build(
                connection,
                command.worker_id,
                command.worker_build_identity,
            )
            connection.execute(
                work_attempts.update()
                .where(
                    work_attempts.c.work_id == command.work_id,
                    work_attempts.c.lease_id == command.lease_id,
                    work_attempts.c.lease_token == command.lease_token,
                )
                .values(
                    **_existing_table_values(
                        work_attempts,
                        {
                            "outcome": "released",
                            "completed_at_utc": now_utc,
                            "release_reason_code": command.reason_code,
                            "worker_build_identity": command.worker_build_identity,
                            "correlation_id": command.correlation_id,
                        },
                    )
                )
            )
            new_revision = int(work["revision"]) + 1
            connection.execute(
                work_units.update()
                .where(work_units.c.work_id == command.work_id)
                .values(
                    state="pending",
                    available_at_utc=now_utc,
                    active_lease_id=None,
                    active_lease_token=None,
                    active_worker_id=None,
                    lease_issued_at_utc=None,
                    lease_expires_at_utc=None,
                    heartbeat_deadline_utc=None,
                    source_policy_digest=None,
                    source_permit_not_before_utc=None,
                    revision=new_revision,
                    updated_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
            )
            self._release_capacity_and_worker(
                connection,
                work,
                command.worker_id,
                command.correlation_id,
                now_utc,
            )
            return WorkMutationResult(
                work_id=command.work_id,
                state=WorkUnitState.PENDING,
                revision=new_revision,
                available_at_utc=now_utc,
            )

        def _expire_leases(
            self,
            connection: Connection,
            command: LeaseExpirySweep,
            now_utc: datetime,
        ) -> LeaseExpiryResult:
            expired = tuple(
                connection.execute(
                    sa.select(work_units)
                    .where(
                        work_units.c.state == "leased",
                        sa.or_(
                            work_units.c.lease_expires_at_utc <= now_utc,
                            work_units.c.heartbeat_deadline_utc <= now_utc,
                        ),
                    )
                    .order_by(
                        work_units.c.lease_expires_at_utc,
                        work_units.c.work_id,
                    )
                    .limit(command.limit)
                    .with_for_update(skip_locked=True)
                ).mappings()
            )
            retry_count = 0
            dead_letter_count = 0
            for work in expired:
                policy = _retry_policy(work)
                decision = policy.decide(
                    WorkFailureKind.TRANSIENT,
                    int(work["attempt_count"]),
                )
                available_at_utc = (
                    now_utc + timedelta(seconds=decision.retry_delay_seconds)
                    if decision.retry_delay_seconds is not None
                    else now_utc
                )
                worker_id = cast(str, work["active_worker_id"])
                worker = self._worker_for_update(connection, worker_id)
                self._record_failure(
                    connection,
                    work=work,
                    lease_id=cast(UUID, work["active_lease_id"]),
                    lease_token=cast(UUID, work["active_lease_token"]),
                    worker_id=worker_id,
                    worker_build_identity=cast(str, worker["build_identity"]),
                    failure_kind=WorkFailureKind.TRANSIENT,
                    code="WORK_LEASE_EXPIRED",
                    owner="WorkEngine",
                    message="The lease expired or missed its heartbeat deadline.",
                    required_action="Acquire a new lease before producing another result.",
                    outcome=decision.attempt_outcome.value,
                    target_state=decision.target_state,
                    available_at_utc=available_at_utc,
                    correlation_id=command.correlation_id,
                    now_utc=now_utc,
                )
                if decision.target_state is WorkUnitState.RETRY_WAIT:
                    retry_count += 1
                elif decision.target_state is WorkUnitState.DEAD_LETTER:
                    dead_letter_count += 1
            return LeaseExpiryResult(
                processed_count=len(expired),
                retry_scheduled_count=retry_count,
                dead_lettered_count=dead_letter_count,
            )

        def _record_failure(
            self,
            connection: Connection,
            *,
            work: Mapping[str, object],
            lease_id: UUID,
            lease_token: UUID,
            worker_id: str,
            worker_build_identity: str,
            failure_kind: WorkFailureKind,
            code: str,
            owner: str,
            message: str,
            required_action: str,
            outcome: str,
            target_state: WorkUnitState,
            available_at_utc: datetime,
            correlation_id: str,
            now_utc: datetime,
        ) -> None:
            attempt = self._attempt_for_lease(
                connection,
                cast(UUID, work["work_id"]),
                lease_id,
                lease_token,
            )
            if attempt is None:
                raise _conflict(
                    "WORK_ATTEMPT_MISSING",
                    "The active lease has no immutable attempt record.",
                    {"workId": str(work["work_id"]), "leaseId": str(lease_id)},
                    "Reconcile durable work history before applying another transition.",
                )
            connection.execute(
                work_attempts.update()
                .where(work_attempts.c.attempt_id == attempt["attempt_id"])
                .values(
                    **_existing_table_values(
                        work_attempts,
                        {
                            "outcome": outcome,
                            "completed_at_utc": now_utc,
                            "failure_kind": failure_kind.value,
                            "failure_code": code,
                            "failure_owner": owner,
                            "failure_message": message,
                            "required_action": required_action,
                            "retry_at_utc": (
                                available_at_utc
                                if target_state is WorkUnitState.RETRY_WAIT
                                else None
                            ),
                            "worker_build_identity": worker_build_identity,
                            "correlation_id": correlation_id,
                        },
                    )
                )
            )
            new_revision = int(work["revision"]) + 1
            connection.execute(
                work_units.update()
                .where(work_units.c.work_id == work["work_id"])
                .values(
                    state=target_state.value,
                    available_at_utc=available_at_utc,
                    active_lease_id=None,
                    active_lease_token=None,
                    active_worker_id=None,
                    lease_issued_at_utc=None,
                    lease_expires_at_utc=None,
                    heartbeat_deadline_utc=None,
                    source_policy_digest=None,
                    source_permit_not_before_utc=None,
                    output_contract=None,
                    output_digest=None,
                    completed_at_utc=None,
                    revision=new_revision,
                    updated_at_utc=now_utc,
                    correlation_id=correlation_id,
                )
            )
            if target_state is WorkUnitState.DEAD_LETTER:
                connection.execute(
                    dead_letters.insert().values(
                        **_table_values(
                            dead_letters,
                            {
                                "work_id": work["work_id"],
                                "attempt_id": attempt["attempt_id"],
                                "run_id": work["run_id"],
                                "stage_run_id": work["stage_run_id"],
                                "stage": work["stage"],
                                "capability": work["capability"],
                                "source_key": work["source_key"],
                                "input_digest": work["input_digest"],
                                "failure_kind": failure_kind.value,
                                "failure_code": code,
                                "code": code,
                                "failure_owner": owner,
                                "owner": owner,
                                "failure_message": message,
                                "message": message,
                                "required_action": required_action,
                                "worker_id": worker_id,
                                "worker_build_identity": worker_build_identity,
                                "dead_lettered_at_utc": now_utc,
                                "created_at_utc": now_utc,
                                "correlation_id": correlation_id,
                            },
                        )
                    )
                )
            self._release_capacity_and_worker(
                connection,
                work,
                worker_id,
                correlation_id,
                now_utc,
            )

        def _active_work(
            self,
            connection: Connection,
            command: LeaseHeartbeat | WorkCompletion | WorkFailure | WorkRelease,
            now_utc: datetime,
        ) -> Mapping[str, object]:
            work = self._work_for_update(connection, command.work_id)
            self._require_active_identity(work, command, now_utc)
            return work

        @staticmethod
        def _require_active_identity(
            work: Mapping[str, object],
            command: LeaseHeartbeat | WorkCompletion | WorkFailure | WorkRelease,
            now_utc: datetime,
        ) -> None:
            if work["state"] != "leased":
                raise _stale_conflict(work, command)
            comparisons = {
                "lease_id_mismatch": (work["active_lease_id"], command.lease_id),
                "lease_token_mismatch": (
                    work["active_lease_token"],
                    command.lease_token,
                ),
                "worker_id_mismatch": (work["active_worker_id"], command.worker_id),
                "input_digest_mismatch": (work["input_digest"], command.input_digest),
            }
            for reason, (stored, actual) in comparisons.items():
                if stored != actual:
                    raise _conflict(
                        "WORK_LEASE_STALE",
                        "The command does not own the active work lease.",
                        {
                            "workId": str(command.work_id),
                            "reason": reason,
                        },
                        "Discard the result and acquire a new lease.",
                    )
            if now_utc >= cast(datetime, work["lease_expires_at_utc"]):
                raise _conflict(
                    "WORK_LEASE_STALE",
                    "The command arrived after lease expiry.",
                    {"workId": str(command.work_id), "reason": "lease_expired"},
                    "Discard the result and acquire a new lease.",
                )
            if now_utc >= cast(datetime, work["heartbeat_deadline_utc"]):
                raise _conflict(
                    "WORK_LEASE_STALE",
                    "The command arrived after the heartbeat deadline.",
                    {"workId": str(command.work_id), "reason": "heartbeat_overdue"},
                    "Discard the result and acquire a new lease.",
                )

        @staticmethod
        def _work_for_update(
            connection: Connection,
            work_id: UUID,
        ) -> Mapping[str, object]:
            work = connection.execute(
                sa.select(work_units)
                .where(work_units.c.work_id == work_id)
                .with_for_update()
            ).mappings().one_or_none()
            if work is None:
                raise _conflict(
                    "WORK_UNIT_NOT_FOUND",
                    "The requested work unit does not exist.",
                    {"workId": str(work_id)},
                    "Use a work id returned by the Worker Gateway.",
                )
            return work

        @staticmethod
        def _worker_for_update(
            connection: Connection,
            worker_id: str,
        ) -> Mapping[str, object]:
            worker = connection.execute(
                sa.select(worker_registrations)
                .where(worker_registrations.c.worker_id == worker_id)
                .with_for_update()
            ).mappings().one_or_none()
            if worker is None:
                raise _conflict(
                    "WORKER_NOT_REGISTERED",
                    "The worker identity is not registered.",
                    {"workerId": worker_id},
                    "Register the exact worker build before requesting work.",
                )
            return worker

        @staticmethod
        def _worker_heartbeat_for_update(
            connection: Connection,
            worker_id: str,
        ) -> Mapping[str, object]:
            heartbeat = connection.execute(
                sa.select(worker_heartbeats)
                .where(worker_heartbeats.c.worker_id == worker_id)
                .with_for_update()
            ).mappings().one_or_none()
            if heartbeat is None:
                raise _conflict(
                    "WORKER_HEARTBEAT_MISSING",
                    "The registered worker has no heartbeat owner row.",
                    {"workerId": worker_id},
                    "Reconcile the worker registration before issuing a lease.",
                )
            return heartbeat

        def _require_worker_build(
            self,
            connection: Connection,
            worker_id: str,
            build_identity: str,
        ) -> None:
            worker = self._worker_for_update(connection, worker_id)
            if worker["build_identity"] != build_identity:
                raise _conflict(
                    "WORKER_BUILD_IDENTITY_MISMATCH",
                    "The result was produced by a build outside the registered identity.",
                    {
                        "workerId": worker_id,
                        "registeredBuildIdentity": worker["build_identity"],
                        "actualBuildIdentity": build_identity,
                    },
                    "Discard the result and register the exact running build.",
                )

        @staticmethod
        def _attempt_for_lease(
            connection: Connection,
            work_id: UUID,
            lease_id: UUID,
            lease_token: UUID,
        ) -> Mapping[str, object] | None:
            return connection.execute(
                sa.select(work_attempts)
                .where(
                    work_attempts.c.work_id == work_id,
                    work_attempts.c.lease_id == lease_id,
                    work_attempts.c.lease_token == lease_token,
                )
                .with_for_update()
            ).mappings().one_or_none()

        @staticmethod
        def _release_capacity_and_worker(
            connection: Connection,
            work: Mapping[str, object],
            worker_id: str,
            correlation_id: str,
            now_utc: datetime,
        ) -> None:
            heartbeat = connection.execute(
                sa.select(worker_heartbeats)
                .where(worker_heartbeats.c.worker_id == worker_id)
                .with_for_update()
            ).mappings().one_or_none()
            if heartbeat is None or heartbeat["active_lease_count"] <= 0:
                raise _conflict(
                    "WORKER_LEASE_COUNT_UNDERFLOW",
                    "Completing a lease would underflow the worker active count.",
                    {"workerId": worker_id, "workId": str(work["work_id"])},
                    "Reconcile worker heartbeat ownership before applying the transition.",
                )
            connection.execute(
                worker_heartbeats.update()
                .where(worker_heartbeats.c.worker_id == worker_id)
                .values(
                    last_seen_at_utc=now_utc,
                    active_lease_count=worker_heartbeats.c.active_lease_count - 1,
                    correlation_id=correlation_id,
                )
            )
            if work["source_key"] is None:
                return
            source = connection.execute(
                sa.select(source_capacity_states)
                .where(source_capacity_states.c.source_key == work["source_key"])
                .with_for_update()
            ).mappings().one_or_none()
            if source is None or source["active_requests"] <= 0:
                raise _conflict(
                    "SOURCE_CAPACITY_UNDERFLOW",
                    "Completing a source lease would underflow active requests.",
                    {
                        "sourceKey": work["source_key"],
                        "workId": str(work["work_id"]),
                    },
                    "Reconcile source capacity ownership before applying the transition.",
                )
            if source["policy_digest"] != work["source_policy_digest"]:
                raise _conflict(
                    "SOURCE_POLICY_DIGEST_DRIFT",
                    "The active source permit no longer matches the capacity owner.",
                    {
                        "sourceKey": work["source_key"],
                        "leasedPolicyDigest": work["source_policy_digest"],
                        "currentPolicyDigest": source["policy_digest"],
                    },
                    "Restore the leased policy identity before applying the transition.",
                )
            connection.execute(
                source_capacity_states.update()
                .where(
                    source_capacity_states.c.source_key == work["source_key"]
                )
                .values(
                    active_requests=source_capacity_states.c.active_requests - 1,
                    revision=source_capacity_states.c.revision + 1,
                    updated_at_utc=now_utc,
                    correlation_id=correlation_id,
                )
            )

        @staticmethod
        def _complete_owner_lifecycles(
            connection: Connection,
            work: Mapping[str, object],
            correlation_id: str,
            now_utc: datetime,
        ) -> None:
            remaining_work = connection.scalar(
                sa.select(sa.func.count())
                .select_from(work_units)
                .where(
                    work_units.c.stage_run_id == work["stage_run_id"],
                    work_units.c.state != "succeeded",
                )
            )
            if remaining_work != 0:
                return
            connection.execute(
                stage_runs.update()
                .where(stage_runs.c.stage_run_id == work["stage_run_id"])
                .values(
                    state="succeeded",
                    revision=stage_runs.c.revision + 1,
                    updated_at_utc=now_utc,
                    correlation_id=correlation_id,
                )
            )
            remaining_stages = connection.scalar(
                sa.select(sa.func.count())
                .select_from(stage_runs)
                .where(
                    stage_runs.c.run_id == work["run_id"],
                    stage_runs.c.state != "succeeded",
                )
            )
            if remaining_stages == 0:
                connection.execute(
                    collection_runs.update()
                    .where(collection_runs.c.run_id == work["run_id"])
                    .values(
                        state="succeeded",
                        revision=collection_runs.c.revision + 1,
                        updated_at_utc=now_utc,
                        correlation_id=correlation_id,
                    )
                )


    def _attempt_candidates(
        *,
        attempt_id: UUID,
        work: Mapping[str, object],
        attempt_number: int,
        lease_id: UUID,
        lease_token: UUID,
        worker_id: str,
        worker_build_identity: str,
        issued_at_utc: datetime,
        expires_at_utc: datetime,
        heartbeat_deadline_utc: datetime,
        permit: SourcePermit | None,
        correlation_id: str,
    ) -> dict[str, object]:
        return {
            "attempt_id": attempt_id,
            "work_id": work["work_id"],
            "run_id": work["run_id"],
            "stage_run_id": work["stage_run_id"],
            "stage": work["stage"],
            "capability": work["capability"],
            "attempt_number": attempt_number,
            "lease_id": lease_id,
            "lease_token": lease_token,
            "worker_id": worker_id,
            "worker_build_identity": worker_build_identity,
            "input_digest": work["input_digest"],
            "expected_output_contract": work["expected_output_contract"],
            "source_key": work["source_key"],
            "source_policy_digest": permit.policy_digest if permit else None,
            "source_permit_not_before_utc": (
                permit.permit_not_before_utc if permit else None
            ),
            "issued_at_utc": issued_at_utc,
            "expires_at_utc": expires_at_utc,
            "heartbeat_deadline_utc": heartbeat_deadline_utc,
            "outcome": "leased",
            "completed_at_utc": None,
            "output_contract": None,
            "output_digest": None,
            "failure_kind": None,
            "failure_code": None,
            "failure_owner": None,
            "failure_message": None,
            "required_action": None,
            "retry_at_utc": None,
            "release_reason_code": None,
            "correlation_id": correlation_id,
        }


    def _table_values(table: Table, candidates: Mapping[str, object]) -> dict[str, object]:
        values = {
            column.name: candidates[column.name]
            for column in table.columns
            if column.name in candidates
        }
        missing = [
            column.name
            for column in table.columns
            if column.name not in values
            and not column.nullable
            and column.default is None
            and column.server_default is None
            and not column.autoincrement
        ]
        if missing:
            raise RuntimeError(
                f"{table.fullname} adapter is missing required values: {missing}"
            )
        return values


    def _existing_table_values(
        table: Table,
        candidates: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            column.name: candidates[column.name]
            for column in table.columns
            if column.name in candidates
        }


    def _lease_from_work(
        work: Mapping[str, object],
        *,
        expires_at_utc: datetime,
        heartbeat_deadline_utc: datetime,
        correlation_id: str,
    ) -> WorkLease:
        permit = None
        if work["source_key"] is not None:
            permit = SourcePermit(
                source_key=cast(str, work["source_key"]),
                policy_digest=cast(str, work["source_policy_digest"]),
                permit_not_before_utc=cast(
                    datetime,
                    work["source_permit_not_before_utc"],
                ),
            )
        return WorkLease(
            lease_id=cast(UUID, work["active_lease_id"]),
            work_id=cast(UUID, work["work_id"]),
            lease_token=cast(UUID, work["active_lease_token"]),
            worker_id=cast(str, work["active_worker_id"]),
            stage=WorkStage(cast(str, work["stage"])),
            capability=WorkCapability(cast(str, work["capability"])),
            input_digest=cast(str, work["input_digest"]),
            expected_output_contract=cast(str, work["expected_output_contract"]),
            issued_at_utc=cast(datetime, work["lease_issued_at_utc"]),
            expires_at_utc=expires_at_utc,
            heartbeat_deadline_utc=heartbeat_deadline_utc,
            source_permit=permit,
            correlation_id=correlation_id,
        )


    def _retry_policy(work: Mapping[str, object]) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=int(work["max_attempts"]),
            initial_delay_seconds=int(work["retry_initial_delay_seconds"]),
            multiplier=int(work["retry_multiplier"]),
            max_delay_seconds=int(work["retry_max_delay_seconds"]),
        )


    def _registration_digest(command: WorkerRegistration) -> str:
        canonical = json.dumps(
            {
                "buildIdentity": command.build_identity,
                "capabilities": sorted(value.value for value in command.capabilities),
                "maxConcurrency": command.max_concurrency,
                "resourceProfile": command.resource_profile,
                "workerId": command.worker_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{sha256(canonical).hexdigest()}"


    def _advisory_lock(connection: Connection, key: str) -> None:
        connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )


    def _stale_conflict(
        work: Mapping[str, object],
        command: LeaseHeartbeat | WorkCompletion | WorkFailure | WorkRelease,
    ) -> WorkEngineConflict:
        return _conflict(
            "WORK_LEASE_STALE",
            "The command no longer owns an active lease.",
            {
                "workId": str(command.work_id),
                "actualState": work["state"],
            },
            "Discard the result and acquire a new lease.",
        )


    def _conflict(
        code: str,
        message: str,
        context: Mapping[str, object],
        required_action: str,
    ) -> WorkEngineConflict:
        return WorkEngineConflict(
            code=code,
            message=message,
            context=context,
            required_action=required_action,
        )


    def _utc_now() -> datetime:
        return datetime.now(UTC)


    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("work-engine clock must return timezone-aware UTC")


    def _constraint_name(exc: sa.exc.IntegrityError) -> str:
        diagnostic = getattr(exc.orig, "diag", None)
        value = getattr(diagnostic, "constraint_name", None)
        return value if isinstance(value, str) and value else "unknown"
    ''',
)

write(
    "packages/collection_infrastructure/src/collection_infrastructure/postgres/__init__.py",
    r'''
    from collection_infrastructure.postgres.metadata import (
        CONFIG_SCHEMA,
        CONFIG_TABLES,
        collector_metadata,
        config_bundle_blockers,
        config_bundle_components,
        config_bundles,
    )
    from collection_infrastructure.postgres.migrations import upgrade_database
    from collection_infrastructure.postgres.run_admission import PostgresRunAdmissionStore
    from collection_infrastructure.postgres.run_admission_metadata import (
        RUN_ADMISSION_TABLES,
        run_admissions,
    )
    from collection_infrastructure.postgres.work_engine import PostgresWorkEngineStore
    from collection_infrastructure.postgres.work_metadata import (
        RUNS_SCHEMA,
        RUN_TABLES,
        SOURCES_SCHEMA,
        SOURCE_TABLES,
        WORK_ENGINE_TABLES,
        WORK_SCHEMA,
        WORK_TABLES,
        collection_runs,
        dead_letters,
        source_capacity_states,
        stage_runs,
        work_attempts,
        work_units,
        worker_capabilities,
        worker_heartbeats,
        worker_registrations,
    )

    __all__ = [
        "CONFIG_SCHEMA",
        "CONFIG_TABLES",
        "PostgresRunAdmissionStore",
        "PostgresWorkEngineStore",
        "RUNS_SCHEMA",
        "RUN_ADMISSION_TABLES",
        "RUN_TABLES",
        "SOURCES_SCHEMA",
        "SOURCE_TABLES",
        "WORK_ENGINE_TABLES",
        "WORK_SCHEMA",
        "WORK_TABLES",
        "collection_runs",
        "collector_metadata",
        "config_bundle_blockers",
        "config_bundle_components",
        "config_bundles",
        "dead_letters",
        "run_admissions",
        "source_capacity_states",
        "stage_runs",
        "upgrade_database",
        "work_attempts",
        "work_units",
        "worker_capabilities",
        "worker_heartbeats",
        "worker_registrations",
    ]
    ''',
)

write(
    "packages/collection_application/tests/test_work_engine.py",
    r'''
    from __future__ import annotations

    from uuid import UUID

    import pytest

    from collection_application import (
        LeaseExpiryResult,
        LeaseExpirySweep,
        LeaseHeartbeat,
        LeaseRequest,
        SourceCapacitySpec,
        WorkCompletion,
        WorkCompletionResult,
        WorkEngineConflict,
        WorkEngineService,
        WorkerRegistration,
        WorkerRegistrationResult,
        WorkerRegistrationStatus,
        WorkFailure,
        WorkLease,
        WorkMutationResult,
        WorkRelease,
    )
    from collection_contracts import OwnerContextError
    from collection_domain import WorkCapability

    _ID1 = UUID("019c0000-0000-7000-8000-000000000001")
    _DIGEST = "sha256:" + ("a" * 64)


    class FakePort:
        conflict: WorkEngineConflict | None = None

        def register_worker(
            self,
            command: WorkerRegistration,
        ) -> WorkerRegistrationResult:
            return WorkerRegistrationResult(
                command.worker_id,
                WorkerRegistrationStatus.REGISTERED,
            )

        def configure_source(self, command: SourceCapacitySpec) -> None:
            del command

        def acquire_lease(self, command: LeaseRequest) -> WorkLease | None:
            del command
            if self.conflict is not None:
                raise self.conflict
            return None

        def heartbeat(self, command: LeaseHeartbeat) -> WorkLease:
            del command
            raise AssertionError("heartbeat was not expected")

        def complete(self, command: WorkCompletion) -> WorkCompletionResult:
            del command
            raise AssertionError("completion was not expected")

        def fail(self, command: WorkFailure) -> WorkMutationResult:
            del command
            raise AssertionError("failure was not expected")

        def release(self, command: WorkRelease) -> WorkMutationResult:
            del command
            raise AssertionError("release was not expected")

        def expire_leases(self, command: LeaseExpirySweep) -> LeaseExpiryResult:
            del command
            return LeaseExpiryResult(0, 0, 0)


    def test_worker_registration_requires_capability() -> None:
        with pytest.raises(ValueError, match="at least one capability"):
            WorkerRegistration(
                worker_id="worker-1",
                build_identity="build-1",
                capabilities=frozenset(),
                max_concurrency=1,
                resource_profile="http-small",
                correlation_id="correlation-1",
            )


    def test_no_eligible_work_is_valid_none_result() -> None:
        result = WorkEngineService(FakePort()).acquire_lease(
            LeaseRequest(
                worker_id="worker-1",
                capability=WorkCapability.HTTP_FETCH,
                lease_duration_seconds=300,
                heartbeat_interval_seconds=60,
                correlation_id="correlation-1",
            )
        )

        assert result is None


    def test_expiry_sweep_is_an_operational_port_not_topology_creation() -> None:
        result = WorkEngineService(FakePort()).expire_leases(
            LeaseExpirySweep(limit=50, correlation_id="correlation-1")
        )

        assert result == LeaseExpiryResult(0, 0, 0)
        assert not hasattr(WorkEngineService, "create_run")
        assert not hasattr(WorkEngineService, "create_stage")
        assert not hasattr(WorkEngineService, "enqueue_work")


    def test_port_conflict_becomes_owner_context_error() -> None:
        port = FakePort()
        port.conflict = WorkEngineConflict(
            code="WORK_LEASE_STALE",
            message="The worker no longer owns this lease.",
            context={"workId": str(_ID1), "reason": "lease_token_mismatch"},
            required_action="Discard the result and acquire a new lease.",
        )

        with pytest.raises(OwnerContextError) as raised:
            WorkEngineService(port).acquire_lease(
                LeaseRequest(
                    worker_id="worker-1",
                    capability=WorkCapability.HTTP_FETCH,
                    lease_duration_seconds=300,
                    heartbeat_interval_seconds=60,
                    correlation_id="correlation-1",
                )
            )

        assert raised.value.envelope.owner == "WorkEngine"
        assert raised.value.envelope.code == "WORK_LEASE_STALE"
        assert raised.value.envelope.correlation_id == "correlation-1"
        assert raised.value.envelope.context["reason"] == "lease_token_mismatch"
    ''',
)

write(
    "database/tests/test_postgres_work_engine_integration.py",
    r'''
    from __future__ import annotations

    import os
    from datetime import UTC, datetime, timedelta
    from hashlib import sha256
    from uuid import UUID, uuid4

    import pytest
    import sqlalchemy as sa
    from sqlalchemy.engine import Engine

    from collection_application import (
        LeaseExpirySweep,
        LeaseHeartbeat,
        LeaseRequest,
        RunAdmissionPlan,
        RunAdmissionService,
        SourceCapacitySpec,
        WorkCompletion,
        WorkEngineConflict,
        WorkerRegistration,
        WorkFailure,
        WorkRelease,
        WorkUnitSpec,
    )
    from collection_domain import (
        CollectionRunState,
        RetryPolicy,
        SourceOperationalState,
        WorkCapability,
        WorkFailureKind,
        WorkStage,
        WorkUnitState,
    )
    from collection_application import CollectionRunSpec, StageRunSpec
    from collection_infrastructure.postgres import (
        PostgresRunAdmissionStore,
        PostgresWorkEngineStore,
        collection_runs,
        dead_letters,
        source_capacity_states,
        stage_runs,
        work_attempts,
        work_units,
        worker_heartbeats,
    )
    from collection_infrastructure.postgres.metadata import (
        config_bundle_components,
        config_bundles,
    )

    pytestmark = pytest.mark.integration


    class MutableClock:
        def __init__(self, value: datetime) -> None:
            self.value = value

        def __call__(self) -> datetime:
            return self.value


    class UUIDSequence:
        def __init__(self) -> None:
            self._value = 1000

        def __call__(self) -> UUID:
            self._value += 1
            return UUID(int=self._value)


    def _engine() -> Engine:
        database_url = os.environ.get("COLLECTOR_DATABASE_URL")
        if not database_url:
            pytest.skip("COLLECTOR_DATABASE_URL is required")
        return sa.create_engine(database_url)


    def _digest(seed: str) -> str:
        return f"sha256:{sha256(seed.encode('utf-8')).hexdigest()}"


    def _ready_bundle(engine: Engine, campaign_key: str) -> str:
        bundle_digest = _digest(f"bundle-{uuid4()}")
        with engine.begin() as connection:
            connection.execute(
                config_bundle_components.insert().values(
                    bundle_digest=bundle_digest,
                    position=0,
                    path="campaign.yaml",
                    component_digest=_digest(f"component-{bundle_digest}"),
                )
            )
            connection.execute(
                config_bundles.insert().values(
                    bundle_digest=bundle_digest,
                    campaign_key=campaign_key,
                    contract="collector-campaign-snapshot",
                    contract_revision="campaign-snapshot-v1",
                    readiness="ready",
                    recorded_at_utc=datetime.now(UTC),
                )
            )
        return bundle_digest


    def _admit_work(
        engine: Engine,
        *,
        campaign_key: str,
        bundle_digest: str,
        capability: WorkCapability,
        source_key: str | None,
        max_attempts: int = 3,
    ) -> tuple[UUID, UUID, UUID]:
        run_id = uuid4()
        stage_run_id = uuid4()
        work_id = uuid4()
        plan = RunAdmissionPlan(
            run=CollectionRunSpec(
                run_id=run_id,
                campaign_key=campaign_key,
                config_bundle_digest=bundle_digest,
                initial_state=CollectionRunState.RUNNING,
                correlation_id=f"correlation-{run_id}",
            ),
            stages=(
                StageRunSpec(
                    stage_run_id=stage_run_id,
                    run_id=run_id,
                    stage=(
                        WorkStage.DISCOVERY
                        if capability in {
                            WorkCapability.MANUAL_IMPORT,
                            WorkCapability.OSM_QUERY,
                        }
                        else WorkStage.ACQUISITION
                    ),
                    correlation_id=f"correlation-{run_id}",
                ),
            ),
            work_units=(
                WorkUnitSpec(
                    work_id=work_id,
                    run_id=run_id,
                    stage_run_id=stage_run_id,
                    stage=(
                        WorkStage.DISCOVERY
                        if capability in {
                            WorkCapability.MANUAL_IMPORT,
                            WorkCapability.OSM_QUERY,
                        }
                        else WorkStage.ACQUISITION
                    ),
                    capability=capability,
                    source_key=source_key,
                    semantic_key=_digest(f"semantic-{work_id}"),
                    input_digest=_digest(f"input-{work_id}"),
                    expected_output_contract="worker-output",
                    priority=0,
                    retry_policy=RetryPolicy(
                        max_attempts=max_attempts,
                        initial_delay_seconds=10,
                        multiplier=2,
                        max_delay_seconds=60,
                    ),
                    correlation_id=f"correlation-{run_id}",
                ),
            ),
        )
        RunAdmissionService(PostgresRunAdmissionStore(engine)).admit(plan)
        return run_id, stage_run_id, work_id


    def _register(
        store: PostgresWorkEngineStore,
        worker_id: str,
        capability: WorkCapability,
    ) -> None:
        store.register_worker(
            WorkerRegistration(
                worker_id=worker_id,
                build_identity="build-1",
                capabilities=frozenset({capability}),
                max_concurrency=1,
                resource_profile="small",
                correlation_id=f"register-{worker_id}",
            )
        )


    def _lease(
        store: PostgresWorkEngineStore,
        worker_id: str,
        capability: WorkCapability,
    ):
        lease = store.acquire_lease(
            LeaseRequest(
                worker_id=worker_id,
                capability=capability,
                lease_duration_seconds=300,
                heartbeat_interval_seconds=60,
                correlation_id=f"lease-{worker_id}",
            )
        )
        assert lease is not None
        return lease


    def test_claim_heartbeat_completion_and_owner_lifecycle_are_atomic() -> None:
        engine = _engine()
        now = datetime(2026, 8, 11, 14, 0, tzinfo=UTC)
        clock = MutableClock(now)
        store = PostgresWorkEngineStore(
            engine,
            clock=clock,
            uuid_factory=UUIDSequence(),
        )
        campaign_key = f"campaign_{uuid4().hex[:12]}"
        bundle_digest = _ready_bundle(engine, campaign_key)
        run_id, stage_run_id, work_id = _admit_work(
            engine,
            campaign_key=campaign_key,
            bundle_digest=bundle_digest,
            capability=WorkCapability.MANUAL_IMPORT,
            source_key=None,
        )
        _register(store, "worker-complete", WorkCapability.MANUAL_IMPORT)

        lease = _lease(store, "worker-complete", WorkCapability.MANUAL_IMPORT)
        assert lease.work_id == work_id
        assert (
            store.acquire_lease(
                LeaseRequest(
                    worker_id="worker-complete",
                    capability=WorkCapability.MANUAL_IMPORT,
                    lease_duration_seconds=300,
                    heartbeat_interval_seconds=60,
                    correlation_id="second-claim",
                )
            )
            is None
        )
        clock.value += timedelta(seconds=30)
        renewed = store.heartbeat(
            LeaseHeartbeat(
                work_id=work_id,
                lease_id=lease.lease_id,
                lease_token=lease.lease_token,
                worker_id=lease.worker_id,
                input_digest=lease.input_digest,
                lease_duration_seconds=300,
                heartbeat_interval_seconds=60,
                correlation_id="heartbeat-1",
            )
        )
        result = store.complete(
            WorkCompletion(
                work_id=work_id,
                lease_id=renewed.lease_id,
                lease_token=renewed.lease_token,
                worker_id=renewed.worker_id,
                input_digest=renewed.input_digest,
                output_contract="worker-output",
                output_digest=_digest("output-complete"),
                worker_build_identity="build-1",
                correlation_id="complete-1",
            )
        )
        repeated = store.complete(
            WorkCompletion(
                work_id=work_id,
                lease_id=renewed.lease_id,
                lease_token=renewed.lease_token,
                worker_id=renewed.worker_id,
                input_digest=renewed.input_digest,
                output_contract="worker-output",
                output_digest=_digest("output-complete"),
                worker_build_identity="build-1",
                correlation_id="complete-repeat",
            )
        )

        assert result.status.value == "applied"
        assert repeated.status.value == "already_applied"
        with engine.connect() as connection:
            assert connection.scalar(
                sa.select(work_units.c.state).where(work_units.c.work_id == work_id)
            ) == "succeeded"
            assert connection.scalar(
                sa.select(stage_runs.c.state).where(
                    stage_runs.c.stage_run_id == stage_run_id
                )
            ) == "succeeded"
            assert connection.scalar(
                sa.select(collection_runs.c.state).where(
                    collection_runs.c.run_id == run_id
                )
            ) == "succeeded"
            assert connection.scalar(
                sa.select(worker_heartbeats.c.active_lease_count).where(
                    worker_heartbeats.c.worker_id == "worker-complete"
                )
            ) == 0
            assert connection.scalar(
                sa.select(work_attempts.c.outcome).where(
                    work_attempts.c.work_id == work_id
                )
            ) == "succeeded"


    def test_source_permit_is_centralized_and_release_returns_capacity() -> None:
        engine = _engine()
        now = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
        store = PostgresWorkEngineStore(
            engine,
            clock=lambda: now,
            uuid_factory=UUIDSequence(),
        )
        source_key = f"source_{uuid4().hex[:12]}"
        store.configure_source(
            SourceCapacitySpec(
                source_key=source_key,
                policy_digest=_digest("source-policy"),
                state=SourceOperationalState.ACTIVE,
                max_active_requests=1,
                minimum_interval_milliseconds=0,
                correlation_id="source-config",
            )
        )
        campaign_key = f"campaign_{uuid4().hex[:12]}"
        bundle_digest = _ready_bundle(engine, campaign_key)
        _, _, work_id = _admit_work(
            engine,
            campaign_key=campaign_key,
            bundle_digest=bundle_digest,
            capability=WorkCapability.OSM_QUERY,
            source_key=source_key,
        )
        _register(store, "worker-source", WorkCapability.OSM_QUERY)

        lease = _lease(store, "worker-source", WorkCapability.OSM_QUERY)
        assert lease.source_permit is not None
        assert lease.source_permit.source_key == source_key
        store.release(
            WorkRelease(
                work_id=work_id,
                lease_id=lease.lease_id,
                lease_token=lease.lease_token,
                worker_id=lease.worker_id,
                input_digest=lease.input_digest,
                reason_code="OPERATOR_RELEASE",
                worker_build_identity="build-1",
                correlation_id="release-1",
            )
        )

        with engine.connect() as connection:
            source = connection.execute(
                sa.select(source_capacity_states).where(
                    source_capacity_states.c.source_key == source_key
                )
            ).mappings().one()
            work = connection.execute(
                sa.select(work_units).where(work_units.c.work_id == work_id)
            ).mappings().one()
        assert source["active_requests"] == 0
        assert work["state"] == "pending"
        assert work["active_lease_id"] is None


    def test_transient_failure_retries_then_expiry_dead_letters() -> None:
        engine = _engine()
        clock = MutableClock(datetime(2026, 8, 11, 16, 0, tzinfo=UTC))
        store = PostgresWorkEngineStore(
            engine,
            clock=clock,
            uuid_factory=UUIDSequence(),
        )
        campaign_key = f"campaign_{uuid4().hex[:12]}"
        bundle_digest = _ready_bundle(engine, campaign_key)
        _, _, work_id = _admit_work(
            engine,
            campaign_key=campaign_key,
            bundle_digest=bundle_digest,
            capability=WorkCapability.MANUAL_IMPORT,
            source_key=None,
            max_attempts=2,
        )
        _register(store, "worker-retry", WorkCapability.MANUAL_IMPORT)

        first = _lease(store, "worker-retry", WorkCapability.MANUAL_IMPORT)
        mutation = store.fail(
            WorkFailure(
                work_id=work_id,
                lease_id=first.lease_id,
                lease_token=first.lease_token,
                worker_id=first.worker_id,
                input_digest=first.input_digest,
                failure_kind=WorkFailureKind.TRANSIENT,
                code="SOURCE_TIMEOUT",
                owner="TestWorker",
                message="The source timed out.",
                required_action="Retry after the owned delay.",
                worker_build_identity="build-1",
                correlation_id="fail-1",
            )
        )
        assert mutation.state is WorkUnitState.RETRY_WAIT

        clock.value += timedelta(seconds=11)
        second = _lease(store, "worker-retry", WorkCapability.MANUAL_IMPORT)
        clock.value = second.heartbeat_deadline_utc
        expired = store.expire_leases(
            LeaseExpirySweep(limit=10, correlation_id="expiry-1")
        )

        assert expired.processed_count == 1
        assert expired.dead_lettered_count == 1
        with engine.connect() as connection:
            assert connection.scalar(
                sa.select(work_units.c.state).where(work_units.c.work_id == work_id)
            ) == "dead_letter"
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(dead_letters).where(
                    dead_letters.c.work_id == work_id
                )
            ) == 1


    def test_stale_lease_token_is_rejected_without_mutation() -> None:
        engine = _engine()
        now = datetime(2026, 8, 11, 17, 0, tzinfo=UTC)
        store = PostgresWorkEngineStore(
            engine,
            clock=lambda: now,
            uuid_factory=UUIDSequence(),
        )
        campaign_key = f"campaign_{uuid4().hex[:12]}"
        bundle_digest = _ready_bundle(engine, campaign_key)
        _, _, work_id = _admit_work(
            engine,
            campaign_key=campaign_key,
            bundle_digest=bundle_digest,
            capability=WorkCapability.MANUAL_IMPORT,
            source_key=None,
        )
        _register(store, "worker-stale", WorkCapability.MANUAL_IMPORT)
        lease = _lease(store, "worker-stale", WorkCapability.MANUAL_IMPORT)

        with pytest.raises(WorkEngineConflict) as raised:
            store.heartbeat(
                LeaseHeartbeat(
                    work_id=work_id,
                    lease_id=lease.lease_id,
                    lease_token=uuid4(),
                    worker_id=lease.worker_id,
                    input_digest=lease.input_digest,
                    lease_duration_seconds=300,
                    heartbeat_interval_seconds=60,
                    correlation_id="stale-heartbeat",
                )
            )

        assert raised.value.code == "WORK_LEASE_STALE"
        with engine.connect() as connection:
            assert connection.scalar(
                sa.select(work_units.c.active_lease_token).where(
                    work_units.c.work_id == work_id
                )
            ) == lease.lease_token
    ''',
)

write(
    "docs/operations/work-engine.md",
    r'''
    # Work Engine

    `RunAdmission` exclusively creates collection-run topology. The operational WorkEngine begins
    only after durable run, stage, and work rows exist.

    ## Transaction owners

    `PostgresWorkEngineStore` implements the application port with PostgreSQL transactions:

    - exact worker registration and immutable registration digest;
    - centralized source capacity and policy identity;
    - deterministic claim ordering with `FOR UPDATE SKIP LOCKED`;
    - one active lease id/token, input digest, worker, heartbeat deadline, and attempt record;
    - source permit reservation and release in the same transaction as the lease transition;
    - stale token, stale worker, stale input, expired lease, and overdue heartbeat rejection;
    - exact output-contract validation and idempotent identical completion;
    - retry/dead-letter classification from the persisted retry policy;
    - explicit release and bounded expiry sweep;
    - worker/source counters that fail closed on drift or underflow.

    Completing the final work unit transitions its stage and run to `succeeded` atomically. Failure
    terminal-state propagation remains a separate control-plane policy owner; this implementation
    does not guess whether one dead letter should fail an entire multi-stage run.

    ## Security boundary

    This adapter is control-plane infrastructure. Workers do not receive PostgreSQL credentials and
    do not import it. A Worker Gateway transport must authenticate the registered worker and invoke
    `WorkEngineService`; object payload transfer remains a pre-signed Object Store protocol.
    ''',
)

module_path = ROOT / ".codex/modules/work-engine.md"
module_text = module_path.read_text(encoding="utf-8")
marker = "## Operational lease ownership"
if marker not in module_text:
    module_text = module_text.rstrip() + dedent(
        r'''

        ## Operational lease ownership

        `collection_application.work_engine` owns operational commands and the transport-independent
        port. It does not create run topology; that authority belongs only to RunAdmission.

        `collection_infrastructure.postgres.work_engine.PostgresWorkEngineStore` owns atomic worker
        registration, source capacity, claim, heartbeat, completion, failure, release, and expiry
        transactions. It is a control-plane adapter, not a worker dependency. Every transition
        validates lease id, token, worker identity, input digest, timing, build identity, source
        policy identity, and durable counters before mutation.

        This owner remains `Status: in development` until an authenticated Worker Gateway and
        pre-signed Object Store protocol provide the production composition root. Direct worker SQL
        access is forbidden.
        '''
    )
    module_path.write_text(module_text.lstrip(), encoding="utf-8")

status_path = ROOT / "docs/implementation-status.md"
status_text = status_path.read_text(encoding="utf-8")
if "Operational work transactions" not in status_text:
    anchor = "| Atomic run admission |"
    line = (
        "| Operational work transactions | Worker/source registration, lease, heartbeat, "
        "completion, retry/dead-letter, release, and expiry PostgreSQL adapter |\n"
    )
    if anchor in status_text:
        line_end = status_text.index("\n", status_text.index(anchor)) + 1
        status_text = status_text[:line_end] + line + status_text[line_end:]
    else:
        status_text += "\n" + line
    status_path.write_text(status_text, encoding="utf-8")
