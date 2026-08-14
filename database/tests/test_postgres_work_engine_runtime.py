from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Barrier
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.pool import NullPool

from collection_application import (
    CollectionRunSpec,
    CollectionRunState,
    LeaseExpirySweep,
    LeaseHeartbeat,
    LeaseRequest,
    RetryPolicy,
    SourceCapacitySpec,
    SourceOperationalState,
    StageRunSpec,
    StageRunState,
    WorkCapability,
    WorkCompletion,
    WorkCompletionStatus,
    WorkEngineConflict,
    WorkerRegistration,
    WorkerRegistrationStatus,
    WorkFailure,
    WorkFailureKind,
    WorkRelease,
    WorkStage,
    WorkUnitSpec,
    WorkUnitState,
)
from collection_infrastructure import PostgresWorkEngine

pytestmark = pytest.mark.integration

_START = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **duration: int) -> None:
        self.value += timedelta(**duration)


@dataclass(frozen=True, slots=True)
class Worker:
    worker_id: str
    build_identity: str


@dataclass(frozen=True, slots=True)
class Harness:
    engine: Engine
    adapter: PostgresWorkEngine
    clock: MutableClock
    run_id: UUID
    stage_run_id: UUID
    stage: WorkStage
    capability: WorkCapability
    source_key: str | None
    output_contract: str
    label: str


def _database_url() -> str:
    value = os.environ.get("COLLECTOR_DATABASE_URL", "").strip()
    if not value:
        pytest.fail("COLLECTOR_DATABASE_URL is required for Work Engine runtime tests.")
    return value


@pytest.fixture
def engine() -> Iterator[Engine]:
    value = sa.create_engine(_database_url(), poolclass=NullPool)
    try:
        yield value
    finally:
        value.dispose()


def _label(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _digest(*parts: str) -> str:
    payload = ":".join(parts).encode("utf-8")
    return f"sha256:{sha256(payload).hexdigest()}"


def _insert_config_artifact(
    connection: sa.Connection,
    bundle_digest: str,
    *,
    recorded_at_utc: datetime | str,
) -> None:
    object_id = uuid4()
    artifact_id = uuid4()
    operation_id = uuid4()
    digest_value = bundle_digest.removeprefix("sha256:")
    connection.execute(
        sa.text(
            """
            INSERT INTO sources.artifact_objects (
                object_id, artifact_kind, content_digest, size_bytes, storage_reference,
                verified_at_utc, recorded_at_utc, correlation_id
            ) VALUES (
                :object_id, 'config_bundle', :bundle_digest, 1, :storage_reference,
                :recorded_at_utc, :recorded_at_utc, 'integration-config-artifact'
            )
            """
        ),
        {
            "object_id": object_id,
            "bundle_digest": bundle_digest,
            "storage_reference": (
                f"config-bundles/sha256/{digest_value[:2]}/{digest_value[2:4]}/{digest_value}"
            ),
            "recorded_at_utc": recorded_at_utc,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO sources.artifact_records (
                artifact_id, object_id, upload_id, work_id, attempt_id, worker_id,
                producer_kind, producer_identity, owner_operation_id, content_type,
                source_policy_digest, recorded_at_utc, correlation_id
            ) VALUES (
                :artifact_id, :object_id, NULL, NULL, NULL, NULL,
                'control_plane', 'integration-test', :operation_id, 'application/json',
                NULL, :recorded_at_utc, 'integration-config-artifact'
            )
            """
        ),
        {
            "artifact_id": artifact_id,
            "object_id": object_id,
            "operation_id": operation_id,
            "recorded_at_utc": recorded_at_utc,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO config.config_bundle_artifacts (
                bundle_digest, artifact_id, recorded_at_utc, correlation_id
            ) VALUES (
                :bundle_digest, :artifact_id, :recorded_at_utc,
                'integration-config-artifact'
            )
            """
        ),
        {
            "bundle_digest": bundle_digest,
            "artifact_id": artifact_id,
            "recorded_at_utc": recorded_at_utc,
        },
    )


def _insert_snapshot(
    engine: Engine,
    label: str,
    *,
    readiness: str = "ready",
    blocker_code: str | None = None,
) -> tuple[str, str]:
    campaign_key = f"campaign_{label}"
    bundle_digest = _digest(label, "bundle")
    with engine.begin() as connection:
        _insert_config_artifact(
            connection,
            bundle_digest,
            recorded_at_utc=_START,
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO config.config_bundle_components (
                    bundle_digest,
                    position,
                    path,
                    component_digest
                ) VALUES (
                    :bundle_digest,
                    0,
                    'campaign.yaml',
                    :component_digest
                )
                """
            ),
            {
                "bundle_digest": bundle_digest,
                "component_digest": _digest(label, "component"),
            },
        )
        if blocker_code is not None:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO config.config_bundle_blockers (
                        bundle_digest,
                        position,
                        code,
                        owner,
                        message,
                        required_action
                    ) VALUES (
                        :bundle_digest,
                        0,
                        :code,
                        'CampaignConfiguration',
                        'The integration snapshot is intentionally blocked.',
                        'Publish a ready snapshot for runtime admission.'
                    )
                    """
                ),
                {"bundle_digest": bundle_digest, "code": blocker_code},
            )
        connection.execute(
            sa.text(
                """
                INSERT INTO config.config_bundles (
                    bundle_digest,
                    campaign_key,
                    contract,
                    contract_revision,
                    readiness,
                    recorded_at_utc
                ) VALUES (
                    :bundle_digest,
                    :campaign_key,
                    'collector-campaign-snapshot',
                    'campaign-snapshot-v1',
                    :readiness,
                    :recorded_at_utc
                )
                """
            ),
            {
                "bundle_digest": bundle_digest,
                "campaign_key": campaign_key,
                "readiness": readiness,
                "recorded_at_utc": _START,
            },
        )
    return campaign_key, bundle_digest


def _create_harness(
    engine: Engine,
    label: str,
    *,
    stage: WorkStage = WorkStage.EXTRACTION,
    capability: WorkCapability = WorkCapability.EXTRACTION,
    source_max_active: int | None = None,
) -> Harness:
    campaign_key, bundle_digest = _insert_snapshot(engine, label)
    clock = MutableClock(_START)
    adapter = PostgresWorkEngine(engine, clock=clock)
    run_id = uuid4()
    stage_run_id = uuid4()
    adapter.create_run(
        CollectionRunSpec(
            run_id=run_id,
            campaign_key=campaign_key,
            config_bundle_digest=bundle_digest,
            initial_state=CollectionRunState.RUNNING,
            correlation_id=f"correlation-{label}",
        )
    )
    adapter.create_stage(
        StageRunSpec(
            stage_run_id=stage_run_id,
            run_id=run_id,
            stage=stage,
            initial_state=StageRunState.RUNNING,
            correlation_id=f"correlation-{label}",
        )
    )
    source_key: str | None = None
    if source_max_active is not None:
        source_key = f"source_{label}"
        adapter.configure_source(
            SourceCapacitySpec(
                source_key=source_key,
                policy_digest=_digest(label, "policy"),
                state=SourceOperationalState.ACTIVE,
                max_active_requests=source_max_active,
                minimum_interval_milliseconds=0,
                correlation_id=f"correlation-{label}",
            )
        )
    return Harness(
        engine=engine,
        adapter=adapter,
        clock=clock,
        run_id=run_id,
        stage_run_id=stage_run_id,
        stage=stage,
        capability=capability,
        source_key=source_key,
        output_contract=f"contract-{label[-16:]}",
        label=label,
    )


def _register_worker(
    harness: Harness,
    suffix: str,
    *,
    output_contracts: frozenset[str] | None = None,
    max_concurrency: int = 1,
) -> tuple[Worker, WorkerRegistrationStatus]:
    worker = Worker(
        worker_id=f"worker-{harness.label[-12:]}-{suffix}",
        build_identity=f"build-{harness.label[-12:]}-{suffix}",
    )
    result = harness.adapter.register_worker(
        WorkerRegistration(
            worker_id=worker.worker_id,
            build_identity=worker.build_identity,
            capabilities=frozenset({harness.capability}),
            supported_output_contracts=(
                output_contracts
                if output_contracts is not None
                else frozenset({harness.output_contract})
            ),
            max_concurrency=max_concurrency,
            resource_profile="integration-test",
            correlation_id=f"correlation-{harness.label}",
        )
    )
    return worker, result.status


def _worker_registration(
    harness: Harness,
    worker: Worker,
    *,
    output_contracts: frozenset[str],
    max_concurrency: int = 1,
) -> WorkerRegistration:
    return WorkerRegistration(
        worker_id=worker.worker_id,
        build_identity=worker.build_identity,
        capabilities=frozenset({harness.capability}),
        supported_output_contracts=output_contracts,
        max_concurrency=max_concurrency,
        resource_profile="integration-test",
        correlation_id=f"correlation-{harness.label}",
    )


def _enqueue_work(
    harness: Harness,
    suffix: str,
    *,
    max_attempts: int = 3,
    priority: int = 0,
) -> WorkUnitSpec:
    command = WorkUnitSpec(
        work_id=uuid4(),
        run_id=harness.run_id,
        stage_run_id=harness.stage_run_id,
        stage=harness.stage,
        capability=harness.capability,
        source_key=harness.source_key,
        semantic_key=_digest(harness.label, suffix, "semantic"),
        input_digest=_digest(harness.label, suffix, "input"),
        expected_output_contract=harness.output_contract,
        priority=priority,
        retry_policy=RetryPolicy(
            max_attempts=max_attempts,
            initial_delay_seconds=10,
            multiplier=2,
            max_delay_seconds=60,
        ),
        available_at_utc=harness.clock.value,
        correlation_id=f"correlation-{harness.label}",
    )
    harness.adapter.enqueue_work(command)
    return command


def _lease_request(
    harness: Harness,
    worker: Worker,
    *,
    lease_duration_seconds: int = 300,
    heartbeat_interval_seconds: int = 60,
) -> LeaseRequest:
    return LeaseRequest(
        worker_id=worker.worker_id,
        capability=harness.capability,
        lease_duration_seconds=lease_duration_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        correlation_id=f"correlation-{harness.label}",
    )


def _work_row(engine: Engine, work_id: UUID) -> RowMapping:
    with engine.connect() as connection:
        return (
            connection.execute(
                sa.text("SELECT * FROM work.work_units WHERE work_id = :work_id"),
                {"work_id": work_id},
            )
            .mappings()
            .one()
        )


def _worker_active_leases(engine: Engine, worker_id: str) -> int:
    with engine.connect() as connection:
        value = connection.execute(
            sa.text(
                """
                SELECT active_lease_count
                FROM work.worker_heartbeats
                WHERE worker_id = :worker_id
                """
            ),
            {"worker_id": worker_id},
        ).scalar_one()
    return int(value)


def _source_active_requests(engine: Engine, source_key: str) -> int:
    with engine.connect() as connection:
        value = connection.execute(
            sa.text(
                """
                SELECT active_requests
                FROM sources.source_capacity_states
                WHERE source_key = :source_key
                """
            ),
            {"source_key": source_key},
        ).scalar_one()
    return int(value)


def test_run_admission_requires_an_exact_ready_snapshot(engine: Engine) -> None:
    ready_label = _label("ready")
    campaign_key, bundle_digest = _insert_snapshot(engine, ready_label)
    adapter = PostgresWorkEngine(engine, clock=MutableClock(_START))
    run_id = uuid4()
    command = CollectionRunSpec(
        run_id=run_id,
        campaign_key=campaign_key,
        config_bundle_digest=bundle_digest,
        initial_state=CollectionRunState.RUNNING,
        correlation_id=f"correlation-{ready_label}",
    )

    adapter.create_run(command)
    adapter.create_run(command)

    with engine.connect() as connection:
        count = connection.execute(
            sa.text("SELECT count(*) FROM runs.collection_runs WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar_one()
    assert count == 1

    blocked_label = _label("blocked")
    blocked_campaign, blocked_digest = _insert_snapshot(
        engine,
        blocked_label,
        readiness="blocked",
        blocker_code="GEOGRAPHY_REVISION_MISSING",
    )
    with pytest.raises(WorkEngineConflict) as blocked:
        adapter.create_run(
            CollectionRunSpec(
                run_id=uuid4(),
                campaign_key=blocked_campaign,
                config_bundle_digest=blocked_digest,
                initial_state=CollectionRunState.RUNNING,
                correlation_id=f"correlation-{blocked_label}",
            )
        )

    assert blocked.value.code == "RUN_CONFIG_BLOCKED"
    assert blocked.value.context["blockerCodes"] == ["GEOGRAPHY_REVISION_MISSING"]

    with pytest.raises(WorkEngineConflict) as missing:
        adapter.create_run(
            CollectionRunSpec(
                run_id=uuid4(),
                campaign_key=f"campaign_{_label('missing')}",
                config_bundle_digest=_digest("missing", uuid4().hex),
                initial_state=CollectionRunState.RUNNING,
                correlation_id="correlation-missing-snapshot",
            )
        )

    assert missing.value.code == "RUN_CONFIG_NOT_FOUND"


def test_worker_registration_is_idempotent_and_contract_exact(engine: Engine) -> None:
    harness = _create_harness(engine, _label("registration"))
    worker, first_status = _register_worker(harness, "one")

    repeated = harness.adapter.register_worker(
        _worker_registration(
            harness,
            worker,
            output_contracts=frozenset({harness.output_contract}),
        )
    )

    assert first_status is WorkerRegistrationStatus.REGISTERED
    assert repeated.status is WorkerRegistrationStatus.ALREADY_REGISTERED

    with pytest.raises(WorkEngineConflict) as changed:
        harness.adapter.register_worker(
            _worker_registration(
                harness,
                worker,
                output_contracts=frozenset({"different-output-contract"}),
            )
        )

    assert changed.value.code == "WORKER_REGISTRATION_CONFLICT"


def test_worker_output_contract_gates_leasing(engine: Engine) -> None:
    harness = _create_harness(engine, _label("compatibility"))
    incompatible, _ = _register_worker(
        harness,
        "incompatible",
        output_contracts=frozenset({"other-output-contract"}),
    )
    work = _enqueue_work(harness, "only")

    assert harness.adapter.acquire_lease(_lease_request(harness, incompatible)) is None

    compatible, _ = _register_worker(harness, "compatible")
    lease = harness.adapter.acquire_lease(_lease_request(harness, compatible))

    assert lease is not None
    assert lease.work_id == work.work_id
    assert lease.expected_output_contract == harness.output_contract


def test_concurrent_workers_cannot_lease_the_same_work(engine: Engine) -> None:
    harness = _create_harness(engine, _label("concurrent"))
    first_worker, _ = _register_worker(harness, "first")
    second_worker, _ = _register_worker(harness, "second")
    work = _enqueue_work(harness, "single")
    barrier = Barrier(2)

    def acquire(worker: Worker) -> UUID | None:
        barrier.wait()
        adapter = PostgresWorkEngine(engine, clock=harness.clock)
        lease = adapter.acquire_lease(_lease_request(harness, worker))
        return lease.lease_id if lease is not None else None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acquire, (first_worker, second_worker)))

    assert sum(result is not None for result in results) == 1
    with engine.connect() as connection:
        attempts = connection.execute(
            sa.text("SELECT count(*) FROM work.work_attempts WHERE work_id = :work_id"),
            {"work_id": work.work_id},
        ).scalar_one()
    assert attempts == 1
    assert _work_row(engine, work.work_id)["state"] == WorkUnitState.LEASED.value


def test_worker_concurrency_is_released_after_completion(engine: Engine) -> None:
    harness = _create_harness(engine, _label("worker_capacity"))
    worker, _ = _register_worker(harness, "one", max_concurrency=1)
    first_work = _enqueue_work(harness, "first", priority=10)
    _enqueue_work(harness, "second")
    first_lease = harness.adapter.acquire_lease(_lease_request(harness, worker))

    assert first_lease is not None
    assert first_lease.work_id == first_work.work_id
    assert harness.adapter.acquire_lease(_lease_request(harness, worker)) is None

    harness.adapter.complete(
        WorkCompletion(
            work_id=first_lease.work_id,
            lease_id=first_lease.lease_id,
            lease_token=first_lease.lease_token,
            worker_id=worker.worker_id,
            input_digest=first_lease.input_digest,
            output_contract=harness.output_contract,
            output_digest=_digest(harness.label, "first", "output"),
            worker_build_identity=worker.build_identity,
            correlation_id=f"correlation-{harness.label}",
        )
    )

    assert _worker_active_leases(engine, worker.worker_id) == 0
    assert harness.adapter.acquire_lease(_lease_request(harness, worker)) is not None


def test_source_capacity_is_released_after_completion(engine: Engine) -> None:
    harness = _create_harness(
        engine,
        _label("source_capacity"),
        stage=WorkStage.ACQUISITION,
        capability=WorkCapability.HTTP_FETCH,
        source_max_active=1,
    )
    first_worker, _ = _register_worker(harness, "first")
    second_worker, _ = _register_worker(harness, "second")
    first_work = _enqueue_work(harness, "first", priority=10)
    _enqueue_work(harness, "second")
    first_lease = harness.adapter.acquire_lease(_lease_request(harness, first_worker))

    assert first_lease is not None
    assert first_lease.work_id == first_work.work_id
    assert harness.source_key is not None
    assert _source_active_requests(engine, harness.source_key) == 1
    assert harness.adapter.acquire_lease(_lease_request(harness, second_worker)) is None

    harness.adapter.complete(
        WorkCompletion(
            work_id=first_lease.work_id,
            lease_id=first_lease.lease_id,
            lease_token=first_lease.lease_token,
            worker_id=first_worker.worker_id,
            input_digest=first_lease.input_digest,
            output_contract=harness.output_contract,
            output_digest=_digest(harness.label, "source", "output"),
            worker_build_identity=first_worker.build_identity,
            correlation_id=f"correlation-{harness.label}",
        )
    )

    assert _source_active_requests(engine, harness.source_key) == 0
    assert harness.adapter.acquire_lease(_lease_request(harness, second_worker)) is not None


def test_heartbeat_and_completion_are_owner_checked_and_idempotent(engine: Engine) -> None:
    harness = _create_harness(engine, _label("completion"))
    worker, _ = _register_worker(harness, "one")
    work = _enqueue_work(harness, "one")
    lease = harness.adapter.acquire_lease(_lease_request(harness, worker))
    assert lease is not None

    harness.clock.advance(seconds=30)
    renewed = harness.adapter.heartbeat(
        LeaseHeartbeat(
            work_id=lease.work_id,
            lease_id=lease.lease_id,
            lease_token=lease.lease_token,
            worker_id=worker.worker_id,
            input_digest=lease.input_digest,
            lease_duration_seconds=300,
            heartbeat_interval_seconds=60,
            correlation_id=f"correlation-{harness.label}",
        )
    )
    assert renewed.expires_at_utc == harness.clock.value + timedelta(seconds=300)
    assert renewed.heartbeat_deadline_utc == harness.clock.value + timedelta(seconds=60)

    with pytest.raises(WorkEngineConflict) as wrong_contract:
        harness.adapter.complete(
            WorkCompletion(
                work_id=lease.work_id,
                lease_id=lease.lease_id,
                lease_token=lease.lease_token,
                worker_id=worker.worker_id,
                input_digest=lease.input_digest,
                output_contract="wrong-output-contract",
                output_digest=_digest(harness.label, "wrong-output"),
                worker_build_identity=worker.build_identity,
                correlation_id=f"correlation-{harness.label}",
            )
        )
    assert wrong_contract.value.code == "WORK_OUTPUT_CONTRACT_MISMATCH"

    completion = WorkCompletion(
        work_id=lease.work_id,
        lease_id=lease.lease_id,
        lease_token=lease.lease_token,
        worker_id=worker.worker_id,
        input_digest=lease.input_digest,
        output_contract=harness.output_contract,
        output_digest=_digest(harness.label, "output"),
        worker_build_identity=worker.build_identity,
        correlation_id=f"correlation-{harness.label}",
    )
    applied = harness.adapter.complete(completion)
    repeated = harness.adapter.complete(completion)

    assert applied.status is WorkCompletionStatus.APPLIED
    assert repeated.status is WorkCompletionStatus.ALREADY_APPLIED
    assert repeated.revision == applied.revision
    assert _work_row(engine, work.work_id)["state"] == WorkUnitState.SUCCEEDED.value


def test_safe_release_does_not_consume_failure_budget(engine: Engine) -> None:
    harness = _create_harness(engine, _label("release"))
    worker, _ = _register_worker(harness, "one")
    work = _enqueue_work(harness, "one", max_attempts=1)
    first_lease = harness.adapter.acquire_lease(_lease_request(harness, worker))
    assert first_lease is not None

    released = harness.adapter.release(
        WorkRelease(
            work_id=first_lease.work_id,
            lease_id=first_lease.lease_id,
            lease_token=first_lease.lease_token,
            worker_id=worker.worker_id,
            input_digest=first_lease.input_digest,
            reason_code="SAFE_RELEASE",
            worker_build_identity=worker.build_identity,
            correlation_id=f"correlation-{harness.label}",
        )
    )
    after_release = _work_row(engine, work.work_id)

    assert released.state is WorkUnitState.PENDING
    assert after_release["attempt_count"] == 1
    assert after_release["failure_count"] == 0

    assert harness.adapter.acquire_lease(_lease_request(harness, worker)) is not None
    after_second_lease = _work_row(engine, work.work_id)
    assert after_second_lease["attempt_count"] == 2
    assert after_second_lease["failure_count"] == 0


def test_transient_failures_respect_retry_budget_and_dead_letter(engine: Engine) -> None:
    harness = _create_harness(engine, _label("retry"))
    worker, _ = _register_worker(harness, "one")
    work = _enqueue_work(harness, "one", max_attempts=2)
    first_lease = harness.adapter.acquire_lease(_lease_request(harness, worker))
    assert first_lease is not None

    first_failure = harness.adapter.fail(
        WorkFailure(
            work_id=first_lease.work_id,
            lease_id=first_lease.lease_id,
            lease_token=first_lease.lease_token,
            worker_id=worker.worker_id,
            input_digest=first_lease.input_digest,
            failure_kind=WorkFailureKind.TRANSIENT,
            code="UPSTREAM_TIMEOUT",
            owner="HttpAcquisition",
            message="The upstream request timed out.",
            required_action="Retry after the bounded Work Engine delay.",
            worker_build_identity=worker.build_identity,
            correlation_id=f"correlation-{harness.label}",
        )
    )

    assert first_failure.state is WorkUnitState.RETRY_WAIT
    assert first_failure.available_at_utc == harness.clock.value + timedelta(seconds=10)
    assert harness.adapter.acquire_lease(_lease_request(harness, worker)) is None

    harness.clock.advance(seconds=10)
    second_lease = harness.adapter.acquire_lease(_lease_request(harness, worker))
    assert second_lease is not None
    second_failure = harness.adapter.fail(
        WorkFailure(
            work_id=second_lease.work_id,
            lease_id=second_lease.lease_id,
            lease_token=second_lease.lease_token,
            worker_id=worker.worker_id,
            input_digest=second_lease.input_digest,
            failure_kind=WorkFailureKind.TRANSIENT,
            code="UPSTREAM_TIMEOUT",
            owner="HttpAcquisition",
            message="The upstream request timed out again.",
            required_action="Inspect the source or worker before explicit replacement work.",
            worker_build_identity=worker.build_identity,
            correlation_id=f"correlation-{harness.label}",
        )
    )

    assert second_failure.state is WorkUnitState.DEAD_LETTER
    row = _work_row(engine, work.work_id)
    assert row["failure_count"] == 2
    with engine.connect() as connection:
        dead_letter_count = connection.execute(
            sa.text("SELECT count(*) FROM work.dead_letters WHERE work_id = :work_id"),
            {"work_id": work.work_id},
        ).scalar_one()
        outcomes = list(
            connection.execute(
                sa.text(
                    """
                    SELECT outcome
                    FROM work.work_attempts
                    WHERE work_id = :work_id
                    ORDER BY attempt_number
                    """
                ),
                {"work_id": work.work_id},
            ).scalars()
        )
    assert dead_letter_count == 1
    assert outcomes == ["retry_scheduled", "dead_lettered"]


def test_expiry_requeues_and_stale_completion_commits_dead_letter(engine: Engine) -> None:
    harness = _create_harness(engine, _label("expiry"))
    worker, _ = _register_worker(harness, "one")
    work = _enqueue_work(harness, "one", max_attempts=2)
    first_lease = harness.adapter.acquire_lease(
        _lease_request(
            harness,
            worker,
            lease_duration_seconds=30,
            heartbeat_interval_seconds=10,
        )
    )
    assert first_lease is not None

    harness.clock.advance(seconds=10)
    sweep = harness.adapter.expire_leases(
        LeaseExpirySweep(
            limit=10,
            correlation_id=f"correlation-{harness.label}",
        )
    )
    assert sweep.expired_count == 1
    assert sweep.retry_wait_count == 1
    assert _work_row(engine, work.work_id)["failure_count"] == 1

    harness.clock.advance(seconds=10)
    second_lease = harness.adapter.acquire_lease(
        _lease_request(
            harness,
            worker,
            lease_duration_seconds=30,
            heartbeat_interval_seconds=10,
        )
    )
    assert second_lease is not None

    harness.clock.advance(seconds=10)
    with pytest.raises(WorkEngineConflict) as stale:
        harness.adapter.complete(
            WorkCompletion(
                work_id=second_lease.work_id,
                lease_id=second_lease.lease_id,
                lease_token=second_lease.lease_token,
                worker_id=worker.worker_id,
                input_digest=second_lease.input_digest,
                output_contract=harness.output_contract,
                output_digest=_digest(harness.label, "late-output"),
                worker_build_identity=worker.build_identity,
                correlation_id=f"correlation-{harness.label}",
            )
        )

    assert stale.value.code == "WORK_LEASE_STALE"
    row = _work_row(engine, work.work_id)
    assert row["state"] == WorkUnitState.DEAD_LETTER.value
    assert row["failure_count"] == 2
    assert _worker_active_leases(engine, worker.worker_id) == 0
    with engine.connect() as connection:
        outcomes = list(
            connection.execute(
                sa.text(
                    """
                    SELECT outcome
                    FROM work.work_attempts
                    WHERE work_id = :work_id
                    ORDER BY attempt_number
                    """
                ),
                {"work_id": work.work_id},
            ).scalars()
        )
        dead_letter_count = connection.execute(
            sa.text("SELECT count(*) FROM work.dead_letters WHERE work_id = :work_id"),
            {"work_id": work.work_id},
        ).scalar_one()
    assert outcomes == ["expired", "expired"]
    assert dead_letter_count == 1
