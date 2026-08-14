from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TypeVar
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from collection_application import (
    CollectionRunSpec,
    CollectionRunState,
    LeaseExpirySweep,
    LeaseExpirySweepResult,
    LeaseHeartbeat,
    LeaseRequest,
    RetryPolicy,
    SourceCapacitySpec,
    SourceOperationalState,
    SourcePermit,
    StageRunSpec,
    StageRunState,
    WorkAttemptOutcome,
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
    WorkInputArtifact,
    WorkLease,
    WorkMutationResult,
    WorkRelease,
    WorkStage,
    WorkUnitSpec,
    WorkUnitState,
)
from collection_infrastructure.postgres.artifact_metadata import (
    artifact_objects,
    artifact_records,
    artifact_uploads,
    work_input_artifacts,
    work_output_artifacts,
)
from collection_infrastructure.postgres.metadata import (
    config_bundle_artifacts,
    config_bundle_blockers,
    config_bundles,
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
    worker_output_contracts,
    worker_registrations,
)

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class _CommittedConflict:
    error: WorkEngineConflict


class PostgresWorkEngine:
    """Transactional PostgreSQL implementation of the Work Engine application port."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4

    def register_worker(self, command: WorkerRegistration) -> WorkerRegistrationResult:
        return self._transaction(
            lambda connection, now_utc: self._register_worker(connection, now_utc, command)
        )

    def configure_source(self, command: SourceCapacitySpec) -> None:
        self._transaction(
            lambda connection, now_utc: self._configure_source(connection, now_utc, command)
        )

    def configure_source_in_transaction(
        self, connection: Connection, command: SourceCapacitySpec
    ) -> None:
        self._configure_source(connection, self._now_utc(), command)

    def create_run(self, command: CollectionRunSpec) -> None:
        self._transaction(
            lambda connection, now_utc: self._create_run(connection, now_utc, command)
        )

    def create_run_in_transaction(self, connection: Connection, command: CollectionRunSpec) -> None:
        self._create_run(connection, self._now_utc(), command)

    def create_stage(self, command: StageRunSpec) -> None:
        self._transaction(
            lambda connection, now_utc: self._create_stage(connection, now_utc, command)
        )

    def create_stage_in_transaction(self, connection: Connection, command: StageRunSpec) -> None:
        self._create_stage(connection, self._now_utc(), command)

    def enqueue_work(self, command: WorkUnitSpec) -> None:
        self._transaction(
            lambda connection, now_utc: self._enqueue_work(connection, now_utc, command)
        )

    def enqueue_work_in_transaction(
        self,
        connection: Connection,
        command: WorkUnitSpec,
    ) -> None:
        """Enqueue work inside a transaction owned by a higher-level use case."""
        self._enqueue_work(connection, self._now_utc(), command)

    def acquire_lease(self, command: LeaseRequest) -> WorkLease | None:
        return self._transaction(
            lambda connection, now_utc: self._acquire_lease(connection, now_utc, command)
        )

    def heartbeat(self, command: LeaseHeartbeat) -> WorkLease:
        return self._transaction(
            lambda connection, now_utc: self._heartbeat(connection, now_utc, command)
        )

    def complete(self, command: WorkCompletion) -> WorkCompletionResult:
        return self._transaction(
            lambda connection, now_utc: self._complete(connection, now_utc, command)
        )

    def fail(self, command: WorkFailure) -> WorkMutationResult:
        return self._transaction(
            lambda connection, now_utc: self._fail(connection, now_utc, command)
        )

    def release(self, command: WorkRelease) -> WorkMutationResult:
        return self._transaction(
            lambda connection, now_utc: self._release(connection, now_utc, command)
        )

    def expire_leases(self, command: LeaseExpirySweep) -> LeaseExpirySweepResult:
        return self._transaction(
            lambda connection, now_utc: self._expire_leases(connection, now_utc, command)
        )

    def _transaction(
        self,
        operation: Callable[[Connection, datetime], _ResultT | _CommittedConflict],
    ) -> _ResultT:
        now_utc = self._now_utc()
        try:
            with self._engine.begin() as connection:
                result = operation(connection, now_utc)
        except WorkEngineConflict:
            raise
        except SQLAlchemyError as exc:
            raise _conflict(
                code="WORK_ENGINE_STORAGE_FAILED",
                message="The Work Engine database operation did not complete.",
                context={"causeType": type(exc).__name__},
                required_action=(
                    "Inspect the PostgreSQL error and Work Engine state, correct the owning "
                    "schema or operation, and retry the exact command."
                ),
            ) from exc
        except ValueError as exc:
            raise _conflict(
                code="WORK_ENGINE_STATE_INVALID",
                message="Persisted Work Engine state violates the application contract.",
                context={"causeType": type(exc).__name__, "detail": str(exc)},
                required_action=(
                    "Inspect the affected run, work unit, attempt, worker, and source rows; "
                    "repair them only through an owner migration or recovery command."
                ),
            ) from exc
        if isinstance(result, _CommittedConflict):
            raise result.error
        return result

    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Work Engine clock must return timezone-aware UTC")
        return value

    def _register_worker(
        self,
        connection: Connection,
        now_utc: datetime,
        command: WorkerRegistration,
    ) -> WorkerRegistrationResult:
        _advisory_lock(connection, f"worker:{command.worker_id}")
        registration_digest = _registration_digest(command)
        existing = (
            connection.execute(
                sa.select(worker_registrations)
                .where(worker_registrations.c.worker_id == command.worker_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            capabilities = frozenset(
                WorkCapability(value)
                for value in connection.execute(
                    sa.select(worker_capabilities.c.capability).where(
                        worker_capabilities.c.worker_id == command.worker_id
                    )
                ).scalars()
            )
            output_contracts = frozenset(
                connection.execute(
                    sa.select(worker_output_contracts.c.output_contract).where(
                        worker_output_contracts.c.worker_id == command.worker_id
                    )
                ).scalars()
            )
            if (
                existing["registration_digest"] != registration_digest
                or existing["build_identity"] != command.build_identity
                or existing["max_concurrency"] != command.max_concurrency
                or existing["resource_profile"] != command.resource_profile
                or capabilities != command.capabilities
                or output_contracts != command.supported_output_contracts
            ):
                raise _conflict(
                    code="WORKER_REGISTRATION_CONFLICT",
                    message="The worker identity is already bound to another registration.",
                    context={"workerId": command.worker_id},
                    required_action=(
                        "Use a new worker instance ID for a different build, capability set, "
                        "output contract set, concurrency, or resource profile."
                    ),
                )
            updated = connection.execute(
                sa.update(worker_heartbeats)
                .where(worker_heartbeats.c.worker_id == command.worker_id)
                .values(last_seen_at_utc=now_utc, correlation_id=command.correlation_id)
            )
            if updated.rowcount != 1:
                raise _state_conflict(
                    code="WORKER_HEARTBEAT_MISSING",
                    message="The registered worker has no heartbeat state.",
                    context={"workerId": command.worker_id},
                )
            return WorkerRegistrationResult(
                worker_id=command.worker_id,
                status=WorkerRegistrationStatus.ALREADY_REGISTERED,
            )

        connection.execute(
            sa.insert(worker_registrations).values(
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
            sa.insert(worker_capabilities),
            [
                {"worker_id": command.worker_id, "capability": capability.value}
                for capability in sorted(command.capabilities, key=lambda value: value.value)
            ],
        )
        connection.execute(
            sa.insert(worker_output_contracts),
            [
                {"worker_id": command.worker_id, "output_contract": contract_identity}
                for contract_identity in sorted(command.supported_output_contracts)
            ],
        )
        connection.execute(
            sa.insert(worker_heartbeats).values(
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
        now_utc: datetime,
        command: SourceCapacitySpec,
    ) -> None:
        _advisory_lock(connection, f"source:{command.source_key}")
        existing = (
            connection.execute(
                sa.select(source_capacity_states)
                .where(source_capacity_states.c.source_key == command.source_key)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            connection.execute(
                sa.insert(source_capacity_states).values(
                    source_key=command.source_key,
                    policy_digest=command.policy_digest,
                    operational_state=command.state.value,
                    max_active_requests=command.max_active_requests,
                    active_requests=0,
                    minimum_interval_milliseconds=command.minimum_interval_milliseconds,
                    next_allowed_request_at_utc=now_utc,
                    retry_after_utc=None,
                    revision=0,
                    updated_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
            )
            return
        if existing["active_requests"] > command.max_active_requests:
            raise _conflict(
                code="SOURCE_CAPACITY_CONFLICT",
                message="Source capacity cannot be reduced below active reservations.",
                context={
                    "sourceKey": command.source_key,
                    "activeRequests": existing["active_requests"],
                    "requestedMaximum": command.max_active_requests,
                },
                required_action=(
                    "Allow active source work to finish or expire before reducing capacity."
                ),
            )
        if (
            existing["policy_digest"] == command.policy_digest
            and existing["operational_state"] == command.state.value
            and existing["max_active_requests"] == command.max_active_requests
            and existing["minimum_interval_milliseconds"] == command.minimum_interval_milliseconds
        ):
            return
        connection.execute(
            sa.update(source_capacity_states)
            .where(source_capacity_states.c.source_key == command.source_key)
            .values(
                policy_digest=command.policy_digest,
                operational_state=command.state.value,
                max_active_requests=command.max_active_requests,
                minimum_interval_milliseconds=command.minimum_interval_milliseconds,
                revision=source_capacity_states.c.revision + 1,
                updated_at_utc=now_utc,
                correlation_id=command.correlation_id,
            )
        )

    def _create_run(
        self,
        connection: Connection,
        now_utc: datetime,
        command: CollectionRunSpec,
    ) -> None:
        _advisory_lock(connection, f"run:{command.run_id}")
        bundle = (
            connection.execute(
                sa.select(
                    config_bundles.c.campaign_key,
                    config_bundles.c.readiness,
                ).where(config_bundles.c.bundle_digest == command.config_bundle_digest)
            )
            .mappings()
            .one_or_none()
        )
        if bundle is None:
            raise _conflict(
                code="RUN_CONFIG_NOT_FOUND",
                message="The requested campaign snapshot is not published.",
                context={"configBundleDigest": command.config_bundle_digest},
                required_action=(
                    "Publish the exact validated campaign snapshot before creating the run."
                ),
            )
        if bundle["campaign_key"] != command.campaign_key:
            raise _conflict(
                code="RUN_CONFIG_CAMPAIGN_MISMATCH",
                message="The campaign key does not match the published snapshot.",
                context={
                    "campaignKey": command.campaign_key,
                    "snapshotCampaignKey": bundle["campaign_key"],
                    "configBundleDigest": command.config_bundle_digest,
                },
                required_action="Create the run with the campaign key owned by the snapshot.",
            )
        artifact_id = connection.execute(
            sa.select(config_bundle_artifacts.c.artifact_id).where(
                config_bundle_artifacts.c.bundle_digest == command.config_bundle_digest
            )
        ).scalar_one_or_none()
        if artifact_id is None:
            raise _conflict(
                code="RUN_CONFIG_ARTIFACT_MISSING",
                message="The published campaign snapshot has no verified object artifact.",
                context={"configBundleDigest": command.config_bundle_digest},
                required_action=(
                    "Publish and bind the exact canonical campaign bundle artifact before "
                    "creating the run."
                ),
            )
        if bundle["readiness"] != "ready":
            blocker_codes = list(
                connection.execute(
                    sa.select(config_bundle_blockers.c.code)
                    .where(config_bundle_blockers.c.bundle_digest == command.config_bundle_digest)
                    .order_by(config_bundle_blockers.c.position)
                ).scalars()
            )
            raise _conflict(
                code="RUN_CONFIG_BLOCKED",
                message="The published campaign snapshot is blocked from runtime use.",
                context={
                    "campaignKey": command.campaign_key,
                    "configBundleDigest": command.config_bundle_digest,
                    "blockerCodes": blocker_codes,
                },
                required_action=(
                    "Resolve every snapshot blocker and publish a different ready snapshot."
                ),
            )
        existing = (
            connection.execute(
                sa.select(collection_runs)
                .where(collection_runs.c.run_id == command.run_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if (
                existing["campaign_key"] == command.campaign_key
                and existing["config_bundle_digest"] == command.config_bundle_digest
            ):
                return
            raise _conflict(
                code="RUN_IDENTITY_CONFLICT",
                message="The run ID is already bound to another campaign snapshot.",
                context={"runId": str(command.run_id)},
                required_action="Use the existing run or create a new run ID.",
            )
        connection.execute(
            sa.insert(collection_runs).values(
                run_id=command.run_id,
                campaign_key=command.campaign_key,
                config_bundle_digest=command.config_bundle_digest,
                state=command.initial_state.value,
                revision=0,
                created_at_utc=now_utc,
                updated_at_utc=now_utc,
                correlation_id=command.correlation_id,
            )
        )

    def _create_stage(
        self,
        connection: Connection,
        now_utc: datetime,
        command: StageRunSpec,
    ) -> None:
        _advisory_lock(connection, f"stage:{command.run_id}:{command.stage.value}")
        run = (
            connection.execute(
                sa.select(collection_runs)
                .where(collection_runs.c.run_id == command.run_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise _conflict(
                code="STAGE_RUN_OWNER_NOT_FOUND",
                message="The stage run has no owning collection run.",
                context={"runId": str(command.run_id), "stage": command.stage.value},
                required_action="Create the owning collection run first.",
            )
        if run["state"] not in {
            CollectionRunState.CREATED.value,
            CollectionRunState.RUNNING.value,
        }:
            raise _conflict(
                code="STAGE_RUN_OWNER_STATE_INVALID",
                message="The collection run cannot accept a new stage.",
                context={"runId": str(command.run_id), "runState": run["state"]},
                required_action="Use a created or running collection run.",
            )
        if (
            command.initial_state is StageRunState.RUNNING
            and run["state"] != CollectionRunState.RUNNING.value
        ):
            raise _conflict(
                code="STAGE_RUN_CANNOT_START",
                message="A stage cannot start before its collection run is running.",
                context={"runId": str(command.run_id), "runState": run["state"]},
                required_action="Start the collection run or create the stage as pending.",
            )
        existing_rows = (
            connection.execute(
                sa.select(stage_runs)
                .where(
                    sa.or_(
                        stage_runs.c.stage_run_id == command.stage_run_id,
                        sa.and_(
                            stage_runs.c.run_id == command.run_id,
                            stage_runs.c.stage == command.stage.value,
                        ),
                    )
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        if existing_rows:
            if len(existing_rows) == 1:
                existing = existing_rows[0]
                if (
                    existing["stage_run_id"] == command.stage_run_id
                    and existing["run_id"] == command.run_id
                    and existing["stage"] == command.stage.value
                ):
                    return
            raise _conflict(
                code="STAGE_RUN_IDENTITY_CONFLICT",
                message="The stage identity is already bound to another owner or stage.",
                context={
                    "runId": str(command.run_id),
                    "stageRunId": str(command.stage_run_id),
                    "stage": command.stage.value,
                },
                required_action="Use the existing stage identity or create a new compatible ID.",
            )
        connection.execute(
            sa.insert(stage_runs).values(
                stage_run_id=command.stage_run_id,
                run_id=command.run_id,
                stage=command.stage.value,
                state=command.initial_state.value,
                revision=0,
                created_at_utc=now_utc,
                updated_at_utc=now_utc,
                correlation_id=command.correlation_id,
            )
        )

    def _enqueue_work(
        self,
        connection: Connection,
        now_utc: datetime,
        command: WorkUnitSpec,
    ) -> None:
        _advisory_lock(connection, f"work:{command.run_id}:{command.semantic_key}")
        owner = (
            connection.execute(
                sa.select(
                    collection_runs.c.state.label("run_state"),
                    stage_runs.c.state.label("stage_state"),
                    stage_runs.c.stage,
                )
                .select_from(
                    stage_runs.join(
                        collection_runs,
                        collection_runs.c.run_id == stage_runs.c.run_id,
                    )
                )
                .where(
                    stage_runs.c.stage_run_id == command.stage_run_id,
                    stage_runs.c.run_id == command.run_id,
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if owner is None or owner["stage"] != command.stage.value:
            raise _conflict(
                code="WORK_OWNER_MISMATCH",
                message="The work unit does not belong to the requested run and stage.",
                context={
                    "workId": str(command.work_id),
                    "runId": str(command.run_id),
                    "stageRunId": str(command.stage_run_id),
                    "stage": command.stage.value,
                },
                required_action="Use the exact owning run, stage run, and stage identity.",
            )
        if owner["run_state"] not in {
            CollectionRunState.CREATED.value,
            CollectionRunState.RUNNING.value,
        } or owner["stage_state"] not in {
            StageRunState.PENDING.value,
            StageRunState.RUNNING.value,
        }:
            raise _conflict(
                code="WORK_OWNER_STATE_INVALID",
                message="The owning run or stage cannot accept new work.",
                context={
                    "runState": owner["run_state"],
                    "stageState": owner["stage_state"],
                },
                required_action=(
                    "Schedule work only for a created/running run and pending/running stage."
                ),
            )
        if command.source_key is not None:
            source_exists = connection.execute(
                sa.select(source_capacity_states.c.source_key).where(
                    source_capacity_states.c.source_key == command.source_key
                )
            ).scalar_one_or_none()
            if source_exists is None:
                raise _conflict(
                    code="WORK_SOURCE_NOT_CONFIGURED",
                    message="Source-bound work references an unconfigured source.",
                    context={"sourceKey": command.source_key, "workId": str(command.work_id)},
                    required_action="Configure the source before scheduling its work.",
                )
        self._require_input_artifacts_exist(connection, command)
        existing_rows = (
            connection.execute(
                sa.select(work_units)
                .where(
                    sa.or_(
                        work_units.c.work_id == command.work_id,
                        sa.and_(
                            work_units.c.run_id == command.run_id,
                            work_units.c.semantic_key == command.semantic_key,
                        ),
                    )
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        if existing_rows:
            existing_inputs = self._load_work_input_artifacts(
                connection, UUID(str(existing_rows[0]["work_id"]))
            )
            if len(existing_rows) == 1 and _same_work_identity(
                existing_rows[0], command, existing_inputs
            ):
                return
            canonical_work_id = str(existing_rows[0]["work_id"])
            raise _conflict(
                code="WORK_SEMANTIC_IDENTITY_CONFLICT",
                message=(
                    "The work ID or semantic key is already bound to different immutable input."
                ),
                context={
                    "workId": str(command.work_id),
                    "canonicalWorkId": canonical_work_id,
                    "semanticKey": command.semantic_key,
                },
                required_action=(
                    "Reuse the canonical work identity for the exact input or publish a different "
                    "semantic key for changed input."
                ),
            )
        connection.execute(
            sa.insert(work_units).values(
                work_id=command.work_id,
                run_id=command.run_id,
                stage_run_id=command.stage_run_id,
                stage=command.stage.value,
                capability=command.capability.value,
                source_key=command.source_key,
                semantic_key=command.semantic_key,
                input_digest=command.input_digest,
                expected_output_contract=command.expected_output_contract,
                priority=command.priority,
                state=WorkUnitState.PENDING.value,
                attempt_count=0,
                failure_count=0,
                max_attempts=command.retry_policy.max_attempts,
                retry_initial_delay_seconds=command.retry_policy.initial_delay_seconds,
                retry_multiplier=command.retry_policy.multiplier,
                retry_max_delay_seconds=command.retry_policy.max_delay_seconds,
                available_at_utc=command.available_at_utc,
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
                revision=0,
                created_at_utc=now_utc,
                updated_at_utc=now_utc,
                correlation_id=command.correlation_id,
            )
        )
        if command.input_artifacts:
            connection.execute(
                sa.insert(work_input_artifacts),
                [
                    {
                        "work_id": command.work_id,
                        "position": position,
                        "artifact_id": binding.artifact_id,
                        "role": binding.role,
                    }
                    for position, binding in enumerate(command.input_artifacts)
                ],
            )

    def _require_input_artifacts_exist(
        self,
        connection: Connection,
        command: WorkUnitSpec,
    ) -> None:
        if not command.input_artifacts:
            return
        requested = {binding.artifact_id for binding in command.input_artifacts}
        existing = {
            UUID(str(value))
            for value in connection.execute(
                sa.select(artifact_records.c.artifact_id).where(
                    artifact_records.c.artifact_id.in_(requested)
                )
            ).scalars()
        }
        missing = sorted(str(value) for value in requested.difference(existing))
        if missing:
            raise _conflict(
                code="WORK_INPUT_ARTIFACT_NOT_FOUND",
                message="The work unit references artifact inputs that do not exist.",
                context={"workId": str(command.work_id), "artifactIds": missing},
                required_action=(
                    "Schedule the work only after every exact input artifact has been committed."
                ),
            )

    def _load_work_input_artifacts(
        self,
        connection: Connection,
        work_id: UUID,
    ) -> tuple[WorkInputArtifact, ...]:
        rows = (
            connection.execute(
                sa.select(
                    work_input_artifacts.c.artifact_id,
                    work_input_artifacts.c.role,
                )
                .where(work_input_artifacts.c.work_id == work_id)
                .order_by(work_input_artifacts.c.position)
            )
            .mappings()
            .all()
        )
        return tuple(
            WorkInputArtifact(artifact_id=UUID(str(row["artifact_id"])), role=str(row["role"]))
            for row in rows
        )

    def _acquire_lease(
        self,
        connection: Connection,
        now_utc: datetime,
        command: LeaseRequest,
    ) -> WorkLease | None:
        worker = (
            connection.execute(
                sa.text(
                    """
                SELECT
                    registration.worker_id,
                    registration.build_identity,
                    registration.max_concurrency,
                    heartbeat.active_lease_count
                FROM work.worker_registrations AS registration
                JOIN work.worker_heartbeats AS heartbeat
                  ON heartbeat.worker_id = registration.worker_id
                WHERE registration.worker_id = :worker_id
                FOR UPDATE OF registration, heartbeat
                """
                ),
                {"worker_id": command.worker_id},
            )
            .mappings()
            .one_or_none()
        )
        if worker is None:
            raise _conflict(
                code="WORKER_NOT_REGISTERED",
                message="The worker is not registered with the Work Engine.",
                context={"workerId": command.worker_id},
                required_action="Register the worker before requesting a lease.",
            )
        capability_registered = connection.execute(
            sa.select(worker_capabilities.c.worker_id).where(
                worker_capabilities.c.worker_id == command.worker_id,
                worker_capabilities.c.capability == command.capability.value,
            )
        ).scalar_one_or_none()
        if capability_registered is None:
            raise _conflict(
                code="WORKER_CAPABILITY_UNSUPPORTED",
                message="The worker did not register the requested capability.",
                context={
                    "workerId": command.worker_id,
                    "capability": command.capability.value,
                },
                required_action="Register a worker build that declares this capability.",
            )
        if worker["active_lease_count"] >= worker["max_concurrency"]:
            return None

        work = (
            connection.execute(
                sa.text(
                    """
                SELECT unit.*
                FROM work.work_units AS unit
                JOIN runs.collection_runs AS run
                  ON run.run_id = unit.run_id
                JOIN runs.stage_runs AS stage
                  ON stage.stage_run_id = unit.stage_run_id
                 AND stage.run_id = unit.run_id
                 AND stage.stage = unit.stage
                WHERE unit.capability = :capability
                  AND unit.state IN ('pending', 'retry_wait')
                  AND unit.available_at_utc <= :now_utc
                  AND unit.failure_count < unit.max_attempts
                  AND run.state = 'running'
                  AND stage.state = 'running'
                  AND EXISTS (
                      SELECT 1
                      FROM work.worker_output_contracts AS contract
                      WHERE contract.worker_id = :worker_id
                        AND contract.output_contract = unit.expected_output_contract
                  )
                  AND (
                      unit.source_key IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM sources.source_capacity_states AS capacity
                          WHERE capacity.source_key = unit.source_key
                            AND capacity.operational_state = 'active'
                            AND capacity.active_requests < capacity.max_active_requests
                            AND capacity.next_allowed_request_at_utc <= :now_utc
                            AND (
                                capacity.retry_after_utc IS NULL
                                OR capacity.retry_after_utc <= :now_utc
                            )
                      )
                  )
                ORDER BY
                    unit.priority DESC,
                    unit.available_at_utc,
                    unit.created_at_utc,
                    unit.work_id
                FOR UPDATE OF unit SKIP LOCKED
                LIMIT 1
                """
                ),
                {
                    "worker_id": command.worker_id,
                    "capability": command.capability.value,
                    "now_utc": now_utc,
                },
            )
            .mappings()
            .one_or_none()
        )
        if work is None:
            return None

        permit: SourcePermit | None = None
        source_policy_digest: str | None = None
        permit_not_before_utc: datetime | None = None
        if work["source_key"] is not None:
            source = (
                connection.execute(
                    sa.select(source_capacity_states)
                    .where(source_capacity_states.c.source_key == work["source_key"])
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if source is None:
                raise _state_conflict(
                    code="SOURCE_CAPACITY_MISSING",
                    message="Source-bound work has no source capacity state.",
                    context={"sourceKey": work["source_key"], "workId": str(work["work_id"])},
                )
            if not _source_can_reserve(source, now_utc):
                return None
            permit_not_before_utc = now_utc
            source_policy_digest = str(source["policy_digest"])
            connection.execute(
                sa.update(source_capacity_states)
                .where(source_capacity_states.c.source_key == work["source_key"])
                .values(
                    active_requests=source_capacity_states.c.active_requests + 1,
                    next_allowed_request_at_utc=now_utc
                    + timedelta(milliseconds=source["minimum_interval_milliseconds"]),
                    revision=source_capacity_states.c.revision + 1,
                    updated_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
            )
            permit = SourcePermit(
                source_key=str(work["source_key"]),
                policy_digest=source_policy_digest,
                permit_not_before_utc=permit_not_before_utc,
            )

        attempt_id = self._uuid_factory()
        lease_id = self._uuid_factory()
        lease_token = self._uuid_factory()
        expires_at_utc = now_utc + timedelta(seconds=command.lease_duration_seconds)
        heartbeat_deadline_utc = now_utc + timedelta(seconds=command.heartbeat_interval_seconds)
        attempt_number = int(work["attempt_count"]) + 1
        updated_work = (
            connection.execute(
                sa.update(work_units)
                .where(work_units.c.work_id == work["work_id"])
                .values(
                    state=WorkUnitState.LEASED.value,
                    attempt_count=attempt_number,
                    active_lease_id=lease_id,
                    active_lease_token=lease_token,
                    active_worker_id=command.worker_id,
                    lease_issued_at_utc=now_utc,
                    lease_expires_at_utc=expires_at_utc,
                    heartbeat_deadline_utc=heartbeat_deadline_utc,
                    source_policy_digest=source_policy_digest,
                    source_permit_not_before_utc=permit_not_before_utc,
                    revision=work_units.c.revision + 1,
                    updated_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
                .returning(*work_units.c)
            )
            .mappings()
            .one()
        )
        connection.execute(
            sa.insert(work_attempts).values(
                attempt_id=attempt_id,
                work_id=work["work_id"],
                attempt_number=attempt_number,
                lease_id=lease_id,
                lease_token=lease_token,
                worker_id=command.worker_id,
                worker_build_identity=worker["build_identity"],
                capability=work["capability"],
                input_digest=work["input_digest"],
                source_key=work["source_key"],
                source_policy_digest=source_policy_digest,
                source_permit_not_before_utc=permit_not_before_utc,
                issued_at_utc=now_utc,
                expires_at_utc=expires_at_utc,
                heartbeat_deadline_utc=heartbeat_deadline_utc,
                finished_at_utc=None,
                outcome=WorkAttemptOutcome.LEASED.value,
                failure_kind=None,
                result_code=None,
                failure_owner=None,
                failure_message=None,
                required_action=None,
                output_contract=None,
                output_digest=None,
                correlation_id=command.correlation_id,
            )
        )
        connection.execute(
            sa.update(worker_heartbeats)
            .where(worker_heartbeats.c.worker_id == command.worker_id)
            .values(
                last_seen_at_utc=now_utc,
                active_lease_count=worker_heartbeats.c.active_lease_count + 1,
                correlation_id=command.correlation_id,
            )
        )
        input_artifacts = self._load_work_input_artifacts(
            connection, UUID(str(updated_work["work_id"]))
        )
        return _lease_from_work(updated_work, permit, command.correlation_id, input_artifacts)

    def _heartbeat(
        self,
        connection: Connection,
        now_utc: datetime,
        command: LeaseHeartbeat,
    ) -> WorkLease | _CommittedConflict:
        active = self._lock_active_lease(
            connection,
            now_utc,
            work_id=command.work_id,
            lease_id=command.lease_id,
            lease_token=command.lease_token,
            worker_id=command.worker_id,
            input_digest=command.input_digest,
            correlation_id=command.correlation_id,
        )
        if isinstance(active, _CommittedConflict):
            return active
        _work, attempt = active
        expires_at_utc = now_utc + timedelta(seconds=command.lease_duration_seconds)
        heartbeat_deadline_utc = now_utc + timedelta(seconds=command.heartbeat_interval_seconds)
        updated_work = (
            connection.execute(
                sa.update(work_units)
                .where(work_units.c.work_id == command.work_id)
                .values(
                    lease_expires_at_utc=expires_at_utc,
                    heartbeat_deadline_utc=heartbeat_deadline_utc,
                    revision=work_units.c.revision + 1,
                    updated_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
                .returning(*work_units.c)
            )
            .mappings()
            .one()
        )
        connection.execute(
            sa.update(work_attempts)
            .where(work_attempts.c.attempt_id == attempt["attempt_id"])
            .values(
                expires_at_utc=expires_at_utc,
                heartbeat_deadline_utc=heartbeat_deadline_utc,
                correlation_id=command.correlation_id,
            )
        )
        connection.execute(
            sa.update(worker_heartbeats)
            .where(worker_heartbeats.c.worker_id == command.worker_id)
            .values(last_seen_at_utc=now_utc, correlation_id=command.correlation_id)
        )
        permit = _permit_from_work(updated_work)
        input_artifacts = self._load_work_input_artifacts(
            connection, UUID(str(updated_work["work_id"]))
        )
        return _lease_from_work(updated_work, permit, command.correlation_id, input_artifacts)

    def _complete(
        self,
        connection: Connection,
        now_utc: datetime,
        command: WorkCompletion,
    ) -> WorkCompletionResult | _CommittedConflict:
        work, attempt = self._lock_work_and_attempt(
            connection,
            work_id=command.work_id,
            lease_id=command.lease_id,
        )
        self._require_attempt_identity(
            attempt,
            work_id=command.work_id,
            lease_id=command.lease_id,
            lease_token=command.lease_token,
            worker_id=command.worker_id,
            input_digest=command.input_digest,
        )
        self._require_worker_build(attempt, command.worker_build_identity)
        if work["state"] == WorkUnitState.SUCCEEDED.value:
            persisted_artifacts = self._load_work_output_identity(connection, command.work_id)
            requested_artifacts = tuple(
                (binding.upload_id, binding.role) for binding in command.output_artifacts
            )
            if (
                attempt["outcome"] == WorkAttemptOutcome.SUCCEEDED.value
                and attempt["output_contract"] == command.output_contract
                and attempt["output_digest"] == command.output_digest
                and work["output_contract"] == command.output_contract
                and work["output_digest"] == command.output_digest
                and persisted_artifacts == requested_artifacts
            ):
                return WorkCompletionResult(
                    work_id=command.work_id,
                    status=WorkCompletionStatus.ALREADY_APPLIED,
                    output_digest=command.output_digest,
                    revision=int(work["revision"]),
                )
            raise _conflict(
                code="WORK_COMPLETION_CONFLICT",
                message="The completed work already has a different immutable result.",
                context={"workId": str(command.work_id)},
                required_action="Use the previously applied completion result.",
            )
        active = self._require_active_rows(
            connection,
            now_utc,
            work,
            attempt,
            correlation_id=command.correlation_id,
        )
        if isinstance(active, _CommittedConflict):
            return active
        if command.output_contract != work["expected_output_contract"]:
            raise _conflict(
                code="WORK_OUTPUT_CONTRACT_MISMATCH",
                message="The completion output contract does not match the leased work contract.",
                context={
                    "workId": str(command.work_id),
                    "expectedOutputContract": work["expected_output_contract"],
                    "actualOutputContract": command.output_contract,
                },
                required_action="Produce the exact output contract declared by the lease.",
            )
        self._consume_output_artifacts(connection, work, attempt, command, now_utc)
        connection.execute(
            sa.update(work_attempts)
            .where(work_attempts.c.attempt_id == attempt["attempt_id"])
            .values(
                finished_at_utc=now_utc,
                outcome=WorkAttemptOutcome.SUCCEEDED.value,
                output_contract=command.output_contract,
                output_digest=command.output_digest,
                correlation_id=command.correlation_id,
            )
        )
        updated_work = connection.execute(
            sa.update(work_units)
            .where(work_units.c.work_id == command.work_id)
            .values(
                state=WorkUnitState.SUCCEEDED.value,
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
                revision=work_units.c.revision + 1,
                updated_at_utc=now_utc,
                correlation_id=command.correlation_id,
            )
            .returning(work_units.c.revision)
        ).one()
        self._release_reservations(
            connection,
            work,
            now_utc,
            command.correlation_id,
        )
        return WorkCompletionResult(
            work_id=command.work_id,
            status=WorkCompletionStatus.APPLIED,
            output_digest=command.output_digest,
            revision=int(updated_work.revision),
        )

    def _load_work_output_identity(
        self,
        connection: Connection,
        work_id: UUID,
    ) -> tuple[tuple[UUID, str], ...]:
        rows = (
            connection.execute(
                sa.select(artifact_records.c.upload_id, work_output_artifacts.c.role)
                .select_from(
                    work_output_artifacts.join(
                        artifact_records,
                        artifact_records.c.artifact_id == work_output_artifacts.c.artifact_id,
                    )
                )
                .where(work_output_artifacts.c.work_id == work_id)
                .order_by(work_output_artifacts.c.position)
            )
            .mappings()
            .all()
        )
        return tuple((UUID(str(row["upload_id"])), str(row["role"])) for row in rows)

    def _consume_output_artifacts(
        self,
        connection: Connection,
        work: RowMapping,
        attempt: RowMapping,
        command: WorkCompletion,
        now_utc: datetime,
    ) -> None:
        if not command.output_artifacts:
            return
        requested_ids = tuple(binding.upload_id for binding in command.output_artifacts)
        rows = (
            connection.execute(
                sa.select(artifact_uploads)
                .where(artifact_uploads.c.upload_id.in_(requested_ids))
                .with_for_update()
            )
            .mappings()
            .all()
        )
        uploads = {UUID(str(row["upload_id"])): row for row in rows}
        missing = [str(value) for value in requested_ids if value not in uploads]
        if missing:
            raise _conflict(
                code="WORK_OUTPUT_ARTIFACT_NOT_FOUND",
                message="The completion references uploads that were not prepared.",
                context={"workId": str(command.work_id), "uploadIds": missing},
                required_action=(
                    "Prepare and verify every output upload under the active lease "
                    "before completion."
                ),
            )

        for position, binding in enumerate(command.output_artifacts):
            upload = uploads[binding.upload_id]
            self._require_completion_upload_identity(upload, command)
            if upload["state"] != "verified":
                raise _conflict(
                    code="WORK_OUTPUT_ARTIFACT_NOT_VERIFIED",
                    message="The completion references an upload that is not verified.",
                    context={
                        "workId": str(command.work_id),
                        "uploadId": str(binding.upload_id),
                        "state": upload["state"],
                    },
                    required_action=(
                        "Verify the exact upload through Worker Gateway before completing work."
                    ),
                )
            final_reference = upload["final_reference"]
            verified_at_utc = upload["verified_at_utc"]
            if not isinstance(final_reference, str) or not isinstance(verified_at_utc, datetime):
                raise _state_conflict(
                    code="ARTIFACT_VERIFICATION_STATE_INVALID",
                    message="A verified upload has incomplete persisted object identity.",
                    context={"uploadId": str(binding.upload_id)},
                )
            object_id = self._get_or_create_artifact_object(
                connection,
                upload,
                final_reference=final_reference,
                verified_at_utc=verified_at_utc,
                now_utc=now_utc,
                correlation_id=command.correlation_id,
            )
            artifact_id = self._uuid_factory()
            connection.execute(
                sa.insert(artifact_records).values(
                    artifact_id=artifact_id,
                    object_id=object_id,
                    upload_id=binding.upload_id,
                    work_id=command.work_id,
                    attempt_id=attempt["attempt_id"],
                    worker_id=command.worker_id,
                    producer_kind="worker",
                    producer_identity=command.worker_id,
                    owner_operation_id=None,
                    content_type=upload["content_type"],
                    source_policy_digest=work["source_policy_digest"],
                    recorded_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
            )
            connection.execute(
                sa.insert(work_output_artifacts).values(
                    work_id=command.work_id,
                    position=position,
                    artifact_id=artifact_id,
                    role=binding.role,
                )
            )
            connection.execute(
                sa.update(artifact_uploads)
                .where(artifact_uploads.c.upload_id == binding.upload_id)
                .values(
                    state="consumed",
                    consumed_at_utc=now_utc,
                    revision=artifact_uploads.c.revision + 1,
                    correlation_id=command.correlation_id,
                )
            )

    def _require_completion_upload_identity(
        self,
        upload: RowMapping,
        command: WorkCompletion,
    ) -> None:
        checks = (
            (upload["work_id"] == command.work_id, "work_id_mismatch"),
            (upload["lease_id"] == command.lease_id, "lease_id_mismatch"),
            (upload["lease_token"] == command.lease_token, "lease_token_mismatch"),
            (upload["worker_id"] == command.worker_id, "worker_id_mismatch"),
            (upload["input_digest"] == command.input_digest, "input_digest_mismatch"),
        )
        for valid, reason in checks:
            if not valid:
                raise _conflict(
                    code="WORK_OUTPUT_ARTIFACT_STALE",
                    message="The output upload is not owned by this active work lease.",
                    context={
                        "workId": str(command.work_id),
                        "uploadId": str(upload["upload_id"]),
                        "reason": reason,
                    },
                    required_action=(
                        "Discard the upload and prepare a new one under the active lease."
                    ),
                )

    def _get_or_create_artifact_object(
        self,
        connection: Connection,
        upload: RowMapping,
        *,
        final_reference: str,
        verified_at_utc: datetime,
        now_utc: datetime,
        correlation_id: str,
    ) -> UUID:
        _advisory_lock(
            connection,
            f"artifact-object:{upload['artifact_kind']}:{upload['expected_digest']}",
        )
        existing = (
            connection.execute(
                sa.select(artifact_objects)
                .where(
                    artifact_objects.c.artifact_kind == upload["artifact_kind"],
                    artifact_objects.c.content_digest == upload["expected_digest"],
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if (
                existing["size_bytes"] != upload["expected_size_bytes"]
                or existing["storage_reference"] != final_reference
            ):
                raise _state_conflict(
                    code="ARTIFACT_OBJECT_IDENTITY_CONFLICT",
                    message="A content-addressed object has conflicting persisted identity.",
                    context={
                        "artifactKind": upload["artifact_kind"],
                        "contentDigest": upload["expected_digest"],
                    },
                )
            return UUID(str(existing["object_id"]))
        object_id = self._uuid_factory()
        connection.execute(
            sa.insert(artifact_objects).values(
                object_id=object_id,
                artifact_kind=upload["artifact_kind"],
                content_digest=upload["expected_digest"],
                size_bytes=upload["expected_size_bytes"],
                storage_reference=final_reference,
                verified_at_utc=verified_at_utc,
                recorded_at_utc=now_utc,
                correlation_id=correlation_id,
            )
        )
        return object_id

    def _fail(
        self,
        connection: Connection,
        now_utc: datetime,
        command: WorkFailure,
    ) -> WorkMutationResult | _CommittedConflict:
        active = self._lock_active_lease(
            connection,
            now_utc,
            work_id=command.work_id,
            lease_id=command.lease_id,
            lease_token=command.lease_token,
            worker_id=command.worker_id,
            input_digest=command.input_digest,
            correlation_id=command.correlation_id,
        )
        if isinstance(active, _CommittedConflict):
            return active
        work, attempt = active
        self._require_worker_build(attempt, command.worker_build_identity)
        failure_number = int(work["failure_count"]) + 1
        decision = _retry_policy(work).decide(command.failure_kind, failure_number)
        available_at_utc = (
            now_utc + timedelta(seconds=decision.retry_delay_seconds)
            if decision.retry_delay_seconds is not None
            else now_utc
        )
        connection.execute(
            sa.update(work_attempts)
            .where(work_attempts.c.attempt_id == attempt["attempt_id"])
            .values(
                finished_at_utc=now_utc,
                outcome=decision.attempt_outcome.value,
                failure_kind=command.failure_kind.value,
                result_code=command.code,
                failure_owner=command.owner,
                failure_message=command.message,
                required_action=command.required_action,
                correlation_id=command.correlation_id,
            )
        )
        updated_work = connection.execute(
            sa.update(work_units)
            .where(work_units.c.work_id == command.work_id)
            .values(
                state=decision.target_state.value,
                failure_count=failure_number,
                available_at_utc=available_at_utc,
                active_lease_id=None,
                active_lease_token=None,
                active_worker_id=None,
                lease_issued_at_utc=None,
                lease_expires_at_utc=None,
                heartbeat_deadline_utc=None,
                source_policy_digest=None,
                source_permit_not_before_utc=None,
                revision=work_units.c.revision + 1,
                updated_at_utc=now_utc,
                correlation_id=command.correlation_id,
            )
            .returning(work_units.c.revision)
        ).one()
        self._release_reservations(
            connection,
            work,
            now_utc,
            command.correlation_id,
        )
        if decision.target_state is WorkUnitState.DEAD_LETTER:
            self._insert_dead_letter(
                connection,
                work_id=command.work_id,
                attempt_id=attempt["attempt_id"],
                failure_kind=command.failure_kind,
                code=command.code,
                owner=command.owner,
                message=command.message,
                required_action=command.required_action,
                now_utc=now_utc,
                correlation_id=command.correlation_id,
            )
        return WorkMutationResult(
            work_id=command.work_id,
            state=decision.target_state,
            revision=int(updated_work.revision),
            available_at_utc=(
                available_at_utc if decision.target_state is WorkUnitState.RETRY_WAIT else None
            ),
        )

    def _release(
        self,
        connection: Connection,
        now_utc: datetime,
        command: WorkRelease,
    ) -> WorkMutationResult | _CommittedConflict:
        active = self._lock_active_lease(
            connection,
            now_utc,
            work_id=command.work_id,
            lease_id=command.lease_id,
            lease_token=command.lease_token,
            worker_id=command.worker_id,
            input_digest=command.input_digest,
            correlation_id=command.correlation_id,
        )
        if isinstance(active, _CommittedConflict):
            return active
        work, attempt = active
        self._require_worker_build(attempt, command.worker_build_identity)
        connection.execute(
            sa.update(work_attempts)
            .where(work_attempts.c.attempt_id == attempt["attempt_id"])
            .values(
                finished_at_utc=now_utc,
                outcome=WorkAttemptOutcome.RELEASED.value,
                result_code=command.reason_code,
                correlation_id=command.correlation_id,
            )
        )
        updated_work = connection.execute(
            sa.update(work_units)
            .where(work_units.c.work_id == command.work_id)
            .values(
                state=WorkUnitState.PENDING.value,
                available_at_utc=now_utc,
                active_lease_id=None,
                active_lease_token=None,
                active_worker_id=None,
                lease_issued_at_utc=None,
                lease_expires_at_utc=None,
                heartbeat_deadline_utc=None,
                source_policy_digest=None,
                source_permit_not_before_utc=None,
                revision=work_units.c.revision + 1,
                updated_at_utc=now_utc,
                correlation_id=command.correlation_id,
            )
            .returning(work_units.c.revision)
        ).one()
        self._release_reservations(
            connection,
            work,
            now_utc,
            command.correlation_id,
        )
        return WorkMutationResult(
            work_id=command.work_id,
            state=WorkUnitState.PENDING,
            revision=int(updated_work.revision),
            available_at_utc=now_utc,
        )

    def _expire_leases(
        self,
        connection: Connection,
        now_utc: datetime,
        command: LeaseExpirySweep,
    ) -> LeaseExpirySweepResult:
        due = (
            connection.execute(
                sa.text(
                    """
                SELECT unit.*
                FROM work.work_units AS unit
                WHERE unit.state = 'leased'
                  AND (
                      unit.lease_expires_at_utc <= :now_utc
                      OR unit.heartbeat_deadline_utc <= :now_utc
                  )
                ORDER BY
                    LEAST(unit.lease_expires_at_utc, unit.heartbeat_deadline_utc),
                    unit.work_id
                FOR UPDATE OF unit SKIP LOCKED
                LIMIT :limit
                """
                ),
                {"now_utc": now_utc, "limit": command.limit},
            )
            .mappings()
            .all()
        )
        retry_wait_count = 0
        dead_letter_count = 0
        for work in due:
            attempt = (
                connection.execute(
                    sa.select(work_attempts)
                    .where(work_attempts.c.lease_id == work["active_lease_id"])
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if attempt is None:
                raise _state_conflict(
                    code="WORK_ATTEMPT_MISSING",
                    message="A leased work unit has no active attempt.",
                    context={"workId": str(work["work_id"])},
                )
            target_state = self._expire_locked(
                connection,
                work,
                attempt,
                now_utc,
                command.correlation_id,
            )
            if target_state is WorkUnitState.RETRY_WAIT:
                retry_wait_count += 1
            else:
                dead_letter_count += 1
        return LeaseExpirySweepResult(
            expired_count=len(due),
            retry_wait_count=retry_wait_count,
            dead_letter_count=dead_letter_count,
        )

    def _lock_active_lease(
        self,
        connection: Connection,
        now_utc: datetime,
        *,
        work_id: UUID,
        lease_id: UUID,
        lease_token: UUID,
        worker_id: str,
        input_digest: str,
        correlation_id: str,
    ) -> tuple[RowMapping, RowMapping] | _CommittedConflict:
        work, attempt = self._lock_work_and_attempt(
            connection,
            work_id=work_id,
            lease_id=lease_id,
        )
        self._require_attempt_identity(
            attempt,
            work_id=work_id,
            lease_id=lease_id,
            lease_token=lease_token,
            worker_id=worker_id,
            input_digest=input_digest,
        )
        return self._require_active_rows(
            connection,
            now_utc,
            work,
            attempt,
            correlation_id=correlation_id,
        )

    def _lock_work_and_attempt(
        self,
        connection: Connection,
        *,
        work_id: UUID,
        lease_id: UUID,
    ) -> tuple[RowMapping, RowMapping]:
        work = (
            connection.execute(
                sa.select(work_units).where(work_units.c.work_id == work_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if work is None:
            raise _conflict(
                code="WORK_NOT_FOUND",
                message="The requested work unit does not exist.",
                context={"workId": str(work_id)},
                required_action="Discard the result and acquire an existing work unit.",
            )
        attempt = (
            connection.execute(
                sa.select(work_attempts)
                .where(work_attempts.c.lease_id == lease_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if attempt is None:
            raise _stale_conflict(work_id, "lease_id_unknown")
        return work, attempt

    def _require_attempt_identity(
        self,
        attempt: RowMapping,
        *,
        work_id: UUID,
        lease_id: UUID,
        lease_token: UUID,
        worker_id: str,
        input_digest: str,
    ) -> None:
        checks = (
            (attempt["work_id"] == work_id, "work_id_mismatch"),
            (attempt["lease_id"] == lease_id, "lease_id_mismatch"),
            (attempt["lease_token"] == lease_token, "lease_token_mismatch"),
            (attempt["worker_id"] == worker_id, "worker_id_mismatch"),
            (attempt["input_digest"] == input_digest, "input_digest_mismatch"),
        )
        for valid, reason in checks:
            if not valid:
                raise _stale_conflict(work_id, reason)

    def _require_worker_build(self, attempt: RowMapping, worker_build_identity: str) -> None:
        if attempt["worker_build_identity"] != worker_build_identity:
            raise _stale_conflict(UUID(str(attempt["work_id"])), "worker_build_mismatch")

    def _require_active_rows(
        self,
        connection: Connection,
        now_utc: datetime,
        work: RowMapping,
        attempt: RowMapping,
        *,
        correlation_id: str,
    ) -> tuple[RowMapping, RowMapping] | _CommittedConflict:
        if (
            work["state"] != WorkUnitState.LEASED.value
            or work["active_lease_id"] != attempt["lease_id"]
            or work["active_lease_token"] != attempt["lease_token"]
            or work["active_worker_id"] != attempt["worker_id"]
            or attempt["outcome"] != WorkAttemptOutcome.LEASED.value
        ):
            raise _stale_conflict(UUID(str(work["work_id"])), "lease_not_active")
        if now_utc >= work["lease_expires_at_utc"] or now_utc >= work["heartbeat_deadline_utc"]:
            self._expire_locked(connection, work, attempt, now_utc, correlation_id)
            return _CommittedConflict(_stale_conflict(UUID(str(work["work_id"])), "lease_expired"))
        return work, attempt

    def _expire_locked(
        self,
        connection: Connection,
        work: RowMapping,
        attempt: RowMapping,
        now_utc: datetime,
        correlation_id: str,
    ) -> WorkUnitState:
        failure_number = int(work["failure_count"]) + 1
        decision = _retry_policy(work).decide(WorkFailureKind.TRANSIENT, failure_number)
        available_at_utc = (
            now_utc + timedelta(seconds=decision.retry_delay_seconds)
            if decision.retry_delay_seconds is not None
            else now_utc
        )
        connection.execute(
            sa.update(work_attempts)
            .where(work_attempts.c.attempt_id == attempt["attempt_id"])
            .values(
                finished_at_utc=now_utc,
                outcome=WorkAttemptOutcome.EXPIRED.value,
                result_code="WORK_LEASE_EXPIRED",
                correlation_id=correlation_id,
            )
        )
        connection.execute(
            sa.update(work_units)
            .where(work_units.c.work_id == work["work_id"])
            .values(
                state=decision.target_state.value,
                failure_count=failure_number,
                available_at_utc=available_at_utc,
                active_lease_id=None,
                active_lease_token=None,
                active_worker_id=None,
                lease_issued_at_utc=None,
                lease_expires_at_utc=None,
                heartbeat_deadline_utc=None,
                source_policy_digest=None,
                source_permit_not_before_utc=None,
                revision=work_units.c.revision + 1,
                updated_at_utc=now_utc,
                correlation_id=correlation_id,
            )
        )
        self._release_reservations(connection, work, now_utc, correlation_id)
        if decision.target_state is WorkUnitState.DEAD_LETTER:
            self._insert_dead_letter(
                connection,
                work_id=UUID(str(work["work_id"])),
                attempt_id=attempt["attempt_id"],
                failure_kind=WorkFailureKind.TRANSIENT,
                code="WORK_LEASE_EXPIRED",
                owner="WorkEngine",
                message="The worker lease expired before a valid completion.",
                required_action=(
                    "Inspect worker health and either correct the worker or explicitly schedule "
                    "replacement work from the retained input."
                ),
                now_utc=now_utc,
                correlation_id=correlation_id,
            )
        return decision.target_state

    def _release_reservations(
        self,
        connection: Connection,
        work: RowMapping,
        now_utc: datetime,
        correlation_id: str,
    ) -> None:
        worker_result = connection.execute(
            sa.update(worker_heartbeats)
            .where(
                worker_heartbeats.c.worker_id == work["active_worker_id"],
                worker_heartbeats.c.active_lease_count > 0,
            )
            .values(
                active_lease_count=worker_heartbeats.c.active_lease_count - 1,
                correlation_id=correlation_id,
            )
        )
        if worker_result.rowcount != 1:
            raise _state_conflict(
                code="WORKER_CAPACITY_CORRUPT",
                message="The active lease is not represented in worker capacity state.",
                context={
                    "workId": str(work["work_id"]),
                    "workerId": work["active_worker_id"],
                },
            )
        if work["source_key"] is None:
            return
        source_result = connection.execute(
            sa.update(source_capacity_states)
            .where(
                source_capacity_states.c.source_key == work["source_key"],
                source_capacity_states.c.active_requests > 0,
            )
            .values(
                active_requests=source_capacity_states.c.active_requests - 1,
                revision=source_capacity_states.c.revision + 1,
                updated_at_utc=now_utc,
                correlation_id=correlation_id,
            )
        )
        if source_result.rowcount != 1:
            raise _state_conflict(
                code="SOURCE_CAPACITY_CORRUPT",
                message="The active lease is not represented in source capacity state.",
                context={
                    "workId": str(work["work_id"]),
                    "sourceKey": work["source_key"],
                },
            )

    def _insert_dead_letter(
        self,
        connection: Connection,
        *,
        work_id: UUID,
        attempt_id: UUID,
        failure_kind: WorkFailureKind,
        code: str,
        owner: str,
        message: str,
        required_action: str,
        now_utc: datetime,
        correlation_id: str,
    ) -> None:
        connection.execute(
            sa.insert(dead_letters).values(
                work_id=work_id,
                attempt_id=attempt_id,
                failure_kind=failure_kind.value,
                code=code,
                owner=owner,
                message=message,
                required_action=required_action,
                created_at_utc=now_utc,
                correlation_id=correlation_id,
            )
        )


def _registration_digest(command: WorkerRegistration) -> str:
    payload = {
        "buildIdentity": command.build_identity,
        "capabilities": sorted(capability.value for capability in command.capabilities),
        "maxConcurrency": command.max_concurrency,
        "resourceProfile": command.resource_profile,
        "supportedOutputContracts": sorted(command.supported_output_contracts),
        "workerId": command.worker_id,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(canonical).hexdigest()}"


def _advisory_lock(connection: Connection, identity: str) -> None:
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": identity},
    )


def _same_work_identity(
    existing: RowMapping,
    command: WorkUnitSpec,
    existing_inputs: tuple[WorkInputArtifact, ...],
) -> bool:
    return bool(
        existing["work_id"] == command.work_id
        and existing["run_id"] == command.run_id
        and existing["stage_run_id"] == command.stage_run_id
        and existing["stage"] == command.stage.value
        and existing["capability"] == command.capability.value
        and existing["source_key"] == command.source_key
        and existing["semantic_key"] == command.semantic_key
        and existing["input_digest"] == command.input_digest
        and existing["expected_output_contract"] == command.expected_output_contract
        and existing["priority"] == command.priority
        and existing["max_attempts"] == command.retry_policy.max_attempts
        and existing["retry_initial_delay_seconds"] == command.retry_policy.initial_delay_seconds
        and existing["retry_multiplier"] == command.retry_policy.multiplier
        and existing["retry_max_delay_seconds"] == command.retry_policy.max_delay_seconds
        and existing["available_at_utc"] == command.available_at_utc
        and existing_inputs == command.input_artifacts
    )


def _source_can_reserve(source: RowMapping, now_utc: datetime) -> bool:
    retry_after_utc = source["retry_after_utc"]
    return (
        source["operational_state"] == SourceOperationalState.ACTIVE.value
        and int(source["active_requests"]) < int(source["max_active_requests"])
        and source["next_allowed_request_at_utc"] <= now_utc
        and (retry_after_utc is None or retry_after_utc <= now_utc)
    )


def _retry_policy(work: RowMapping) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=int(work["max_attempts"]),
        initial_delay_seconds=int(work["retry_initial_delay_seconds"]),
        multiplier=int(work["retry_multiplier"]),
        max_delay_seconds=int(work["retry_max_delay_seconds"]),
    )


def _permit_from_work(work: RowMapping) -> SourcePermit | None:
    if work["source_key"] is None:
        return None
    policy_digest = work["source_policy_digest"]
    permit_not_before_utc = work["source_permit_not_before_utc"]
    if policy_digest is None or permit_not_before_utc is None:
        raise ValueError("source-bound lease has incomplete permit state")
    return SourcePermit(
        source_key=str(work["source_key"]),
        policy_digest=str(policy_digest),
        permit_not_before_utc=permit_not_before_utc,
    )


def _lease_from_work(
    work: RowMapping,
    permit: SourcePermit | None,
    correlation_id: str,
    input_artifacts: tuple[WorkInputArtifact, ...],
) -> WorkLease:
    return WorkLease(
        lease_id=UUID(str(work["active_lease_id"])),
        work_id=UUID(str(work["work_id"])),
        lease_token=UUID(str(work["active_lease_token"])),
        worker_id=str(work["active_worker_id"]),
        stage=WorkStage(str(work["stage"])),
        capability=WorkCapability(str(work["capability"])),
        input_digest=str(work["input_digest"]),
        expected_output_contract=str(work["expected_output_contract"]),
        issued_at_utc=work["lease_issued_at_utc"],
        expires_at_utc=work["lease_expires_at_utc"],
        heartbeat_deadline_utc=work["heartbeat_deadline_utc"],
        source_permit=permit,
        correlation_id=correlation_id,
        input_artifacts=input_artifacts,
    )


def _stale_conflict(work_id: UUID, reason: str) -> WorkEngineConflict:
    return _conflict(
        code="WORK_LEASE_STALE",
        message="The worker no longer owns this work lease.",
        context={"workId": str(work_id), "reason": reason},
        required_action="Discard the result and acquire a new lease.",
    )


def _state_conflict(
    *,
    code: str,
    message: str,
    context: Mapping[str, object],
) -> WorkEngineConflict:
    return _conflict(
        code=code,
        message=message,
        context=context,
        required_action=(
            "Inspect the affected Work Engine rows and correct them only through an owner "
            "migration or recovery command."
        ),
    )


def _conflict(
    *,
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
