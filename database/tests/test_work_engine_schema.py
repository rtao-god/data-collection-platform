from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _database_url() -> str:
    value = os.environ.get("COLLECTOR_DATABASE_URL", "").strip()
    if not value:
        pytest.fail("COLLECTOR_DATABASE_URL is required for work-engine integration tests.")
    return value


def _digest(value: str) -> str:
    return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"


def _id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"data-collection-platform:{value}")


def _source_key(value: str) -> str:
    return f"source_{sha256(value.encode('utf-8')).hexdigest()[:12]}"


def _worker_id(value: str) -> str:
    return f"worker-{sha256(value.encode('utf-8')).hexdigest()[:12]}"


def _insert_config(connection: sa.Connection, label: str) -> str:
    bundle_digest = _digest(f"{label}:config")
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
            "component_digest": _digest(f"{label}:component"),
        },
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
                'ready',
                :now_utc
            )
            """
        ),
        {
            "bundle_digest": bundle_digest,
            "campaign_key": f"campaign_{sha256(label.encode('utf-8')).hexdigest()[:12]}",
            "now_utc": _NOW,
        },
    )
    return bundle_digest


def _insert_run_stage(
    connection: sa.Connection,
    label: str,
    *,
    stage: str,
) -> tuple[UUID, UUID]:
    run_id = _id(f"{label}:run")
    stage_run_id = _id(f"{label}:stage")
    connection.execute(
        sa.text(
            """
            INSERT INTO runs.collection_runs (
                run_id,
                campaign_key,
                config_bundle_digest,
                state,
                revision,
                created_at_utc,
                updated_at_utc,
                correlation_id
            ) VALUES (
                :run_id,
                :campaign_key,
                :config_bundle_digest,
                'running',
                0,
                :now_utc,
                :now_utc,
                :correlation_id
            )
            """
        ),
        {
            "run_id": run_id,
            "campaign_key": f"campaign_{sha256(label.encode('utf-8')).hexdigest()[:12]}",
            "config_bundle_digest": _insert_config(connection, label),
            "now_utc": _NOW,
            "correlation_id": f"correlation-{label}",
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO runs.stage_runs (
                stage_run_id,
                run_id,
                stage,
                state,
                revision,
                created_at_utc,
                updated_at_utc,
                correlation_id
            ) VALUES (
                :stage_run_id,
                :run_id,
                :stage,
                'running',
                0,
                :now_utc,
                :now_utc,
                :correlation_id
            )
            """
        ),
        {
            "stage_run_id": stage_run_id,
            "run_id": run_id,
            "stage": stage,
            "now_utc": _NOW,
            "correlation_id": f"correlation-{label}",
        },
    )
    return run_id, stage_run_id


def _insert_source(connection: sa.Connection, label: str) -> tuple[str, str]:
    source_key = _source_key(label)
    policy_digest = _digest(f"{label}:policy")
    connection.execute(
        sa.text(
            """
            INSERT INTO sources.source_capacity_states (
                source_key,
                policy_digest,
                operational_state,
                max_active_requests,
                active_requests,
                minimum_interval_milliseconds,
                next_allowed_request_at_utc,
                retry_after_utc,
                revision,
                updated_at_utc,
                correlation_id
            ) VALUES (
                :source_key,
                :policy_digest,
                'active',
                2,
                0,
                100,
                :now_utc,
                NULL,
                0,
                :now_utc,
                :correlation_id
            )
            """
        ),
        {
            "source_key": source_key,
            "policy_digest": policy_digest,
            "now_utc": _NOW,
            "correlation_id": f"correlation-{label}",
        },
    )
    return source_key, policy_digest


def _insert_worker(
    connection: sa.Connection,
    label: str,
    capability: str,
    output_contract: str = "integration-output",
) -> str:
    worker_id = _worker_id(label)
    connection.execute(
        sa.text(
            """
            INSERT INTO work.worker_registrations (
                worker_id,
                registration_digest,
                build_identity,
                max_concurrency,
                resource_profile,
                registered_at_utc,
                correlation_id
            ) VALUES (
                :worker_id,
                :registration_digest,
                :build_identity,
                2,
                'integration-test',
                :now_utc,
                :correlation_id
            )
            """
        ),
        {
            "worker_id": worker_id,
            "registration_digest": _digest(f"{label}:worker-registration"),
            "build_identity": f"build-{label}",
            "now_utc": _NOW,
            "correlation_id": f"correlation-{label}",
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO work.worker_capabilities (worker_id, capability)
            VALUES (:worker_id, :capability)
            """
        ),
        {"worker_id": worker_id, "capability": capability},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO work.worker_output_contracts (worker_id, output_contract)
            VALUES (:worker_id, :output_contract)
            """
        ),
        {"worker_id": worker_id, "output_contract": output_contract},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO work.worker_heartbeats (
                worker_id,
                last_seen_at_utc,
                active_lease_count,
                correlation_id
            ) VALUES (
                :worker_id,
                :now_utc,
                0,
                :correlation_id
            )
            """
        ),
        {
            "worker_id": worker_id,
            "now_utc": _NOW,
            "correlation_id": f"correlation-{label}",
        },
    )
    return worker_id


def _pending_work_values(
    label: str,
    *,
    run_id: UUID,
    stage_run_id: UUID,
    stage: str,
    capability: str,
    source_key: str | None,
) -> dict[str, object]:
    return {
        "work_id": _id(f"{label}:work"),
        "run_id": run_id,
        "stage_run_id": stage_run_id,
        "stage": stage,
        "capability": capability,
        "source_key": source_key,
        "semantic_key": _digest(f"{label}:semantic"),
        "input_digest": _digest(f"{label}:input"),
        "expected_output_contract": "integration-output",
        "priority": 0,
        "state": "pending",
        "attempt_count": 0,
        "failure_count": 0,
        "max_attempts": 3,
        "retry_initial_delay_seconds": 10,
        "retry_multiplier": 2,
        "retry_max_delay_seconds": 60,
        "available_at_utc": _NOW,
        "revision": 0,
        "created_at_utc": _NOW,
        "updated_at_utc": _NOW,
        "correlation_id": f"correlation-{label}",
    }


def _insert_pending_work(connection: sa.Connection, values: dict[str, object]) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO work.work_units (
                work_id,
                run_id,
                stage_run_id,
                stage,
                capability,
                source_key,
                semantic_key,
                input_digest,
                expected_output_contract,
                priority,
                state,
                attempt_count,
                failure_count,
                max_attempts,
                retry_initial_delay_seconds,
                retry_multiplier,
                retry_max_delay_seconds,
                available_at_utc,
                active_lease_id,
                active_lease_token,
                active_worker_id,
                lease_issued_at_utc,
                lease_expires_at_utc,
                heartbeat_deadline_utc,
                source_policy_digest,
                source_permit_not_before_utc,
                output_contract,
                output_digest,
                completed_at_utc,
                revision,
                created_at_utc,
                updated_at_utc,
                correlation_id
            ) VALUES (
                :work_id,
                :run_id,
                :stage_run_id,
                :stage,
                :capability,
                :source_key,
                :semantic_key,
                :input_digest,
                :expected_output_contract,
                :priority,
                :state,
                :attempt_count,
                :failure_count,
                :max_attempts,
                :retry_initial_delay_seconds,
                :retry_multiplier,
                :retry_max_delay_seconds,
                :available_at_utc,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                :revision,
                :created_at_utc,
                :updated_at_utc,
                :correlation_id
            )
            """
        ),
        values,
    )


def test_fresh_migration_creates_exact_work_engine_contract() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)
    inspector = sa.inspect(engine)

    assert set(inspector.get_table_names(schema="runs")) == {
        "collection_runs",
        "stage_runs",
    }
    assert set(inspector.get_table_names(schema="sources")) == {
        "artifact_objects",
        "artifact_uploads",
        "raw_artifacts",
        "source_capacity_states",
    }
    assert set(inspector.get_table_names(schema="work")) == {
        "dead_letters",
        "work_attempts",
        "work_units",
        "work_output_artifacts",
        "worker_capabilities",
        "worker_heartbeats",
        "worker_output_contracts",
        "worker_registrations",
    }
    assert {column["name"] for column in inspector.get_columns("work_units", schema="work")} >= {
        "attempt_count",
        "failure_count",
        "max_attempts",
    }
    assert {index["name"] for index in inspector.get_indexes("work_units", schema="work")} == {
        "ix_work_units_claim",
        "ix_work_units_lease_expiry",
        "uq_work_units_active_lease_id",
        "uq_work_units_active_lease_token",
        "uq_work_units_run_semantic_key",
    }


def test_worker_output_contract_identity_is_fail_closed() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)

    with engine.begin() as connection:
        worker_id = _insert_worker(connection, "worker-contract", "extraction")

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO work.worker_output_contracts (worker_id, output_contract)
                VALUES (:worker_id, 'invalid contract')
                """
            ),
            {"worker_id": worker_id},
        )


def test_failure_budget_is_independent_from_safe_attempts() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)

    with engine.begin() as connection:
        run_id, stage_run_id = _insert_run_stage(
            connection,
            "safe-attempt-budget",
            stage="extraction",
        )
        values = _pending_work_values(
            "safe-attempt-budget",
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage="extraction",
            capability="extraction",
            source_key=None,
        )
        values["attempt_count"] = 5
        values["failure_count"] = 1
        _insert_pending_work(connection, values)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        invalid = dict(values)
        invalid["work_id"] = _id("safe-attempt-budget:invalid")
        invalid["semantic_key"] = _digest("safe-attempt-budget:invalid")
        invalid["failure_count"] = 6
        _insert_pending_work(connection, invalid)


def test_source_capability_contract_rejects_missing_or_extraneous_source() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)

    with engine.begin() as connection:
        run_id, stage_run_id = _insert_run_stage(
            connection,
            "missing-source",
            stage="acquisition",
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert_pending_work(
            connection,
            _pending_work_values(
                "missing-source",
                run_id=run_id,
                stage_run_id=stage_run_id,
                stage="acquisition",
                capability="http_fetch",
                source_key=None,
            ),
        )

    with engine.begin() as connection:
        run_id, stage_run_id = _insert_run_stage(
            connection,
            "extraneous-source",
            stage="extraction",
        )
        source_key, _ = _insert_source(connection, "extraneous-source")

    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert_pending_work(
            connection,
            _pending_work_values(
                "extraneous-source",
                run_id=run_id,
                stage_run_id=stage_run_id,
                stage="extraction",
                capability="extraction",
                source_key=source_key,
            ),
        )


def test_work_unit_rejects_stage_mismatch_and_duplicate_semantic_identity() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)

    with engine.begin() as connection:
        run_id, stage_run_id = _insert_run_stage(
            connection,
            "stage-mismatch",
            stage="extraction",
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert_pending_work(
            connection,
            _pending_work_values(
                "stage-mismatch",
                run_id=run_id,
                stage_run_id=stage_run_id,
                stage="extraction",
                capability="normalization",
                source_key=None,
            ),
        )

    with engine.begin() as connection:
        run_id, stage_run_id = _insert_run_stage(
            connection,
            "semantic-identity",
            stage="extraction",
        )
        values = _pending_work_values(
            "semantic-identity",
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage="extraction",
            capability="extraction",
            source_key=None,
        )
        _insert_pending_work(connection, values)

    duplicate = dict(values)
    duplicate["work_id"] = _id("semantic-identity:duplicate-work")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert_pending_work(connection, duplicate)


def test_leased_work_and_attempt_result_shapes_fail_closed() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)
    label = "lease-shape"

    with engine.begin() as connection:
        run_id, stage_run_id = _insert_run_stage(connection, label, stage="acquisition")
        source_key, policy_digest = _insert_source(connection, label)
        worker_id = _insert_worker(connection, label, "http_fetch")
        values = _pending_work_values(
            label,
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage="acquisition",
            capability="http_fetch",
            source_key=source_key,
        )
        values.update(
            {
                "state": "leased",
                "attempt_count": 1,
                "active_lease_id": _id(f"{label}:lease"),
                "active_lease_token": _id(f"{label}:token"),
                "active_worker_id": worker_id,
                "lease_issued_at_utc": _NOW,
                "lease_expires_at_utc": _NOW + timedelta(minutes=5),
                "heartbeat_deadline_utc": _NOW + timedelta(minutes=1),
                "source_policy_digest": policy_digest,
                "source_permit_not_before_utc": _NOW,
            }
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO work.work_units (
                    work_id,
                    run_id,
                    stage_run_id,
                    stage,
                    capability,
                    source_key,
                    semantic_key,
                    input_digest,
                    expected_output_contract,
                    priority,
                    state,
                    attempt_count,
                    failure_count,
                    max_attempts,
                    retry_initial_delay_seconds,
                    retry_multiplier,
                    retry_max_delay_seconds,
                    available_at_utc,
                    active_lease_id,
                    active_lease_token,
                    active_worker_id,
                    lease_issued_at_utc,
                    lease_expires_at_utc,
                    heartbeat_deadline_utc,
                    source_policy_digest,
                    source_permit_not_before_utc,
                    output_contract,
                    output_digest,
                    completed_at_utc,
                    revision,
                    created_at_utc,
                    updated_at_utc,
                    correlation_id
                ) VALUES (
                    :work_id,
                    :run_id,
                    :stage_run_id,
                    :stage,
                    :capability,
                    :source_key,
                    :semantic_key,
                    :input_digest,
                    :expected_output_contract,
                    :priority,
                    :state,
                    :attempt_count,
                    :failure_count,
                    :max_attempts,
                    :retry_initial_delay_seconds,
                    :retry_multiplier,
                    :retry_max_delay_seconds,
                    :available_at_utc,
                    :active_lease_id,
                    :active_lease_token,
                    :active_worker_id,
                    :lease_issued_at_utc,
                    :lease_expires_at_utc,
                    :heartbeat_deadline_utc,
                    :source_policy_digest,
                    :source_permit_not_before_utc,
                    NULL,
                    NULL,
                    NULL,
                    :revision,
                    :created_at_utc,
                    :updated_at_utc,
                    :correlation_id
                )
                """
            ),
            values,
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO work.work_attempts (
                    attempt_id,
                    work_id,
                    attempt_number,
                    lease_id,
                    lease_token,
                    worker_id,
                    worker_build_identity,
                    capability,
                    input_digest,
                    source_key,
                    source_policy_digest,
                    source_permit_not_before_utc,
                    issued_at_utc,
                    expires_at_utc,
                    heartbeat_deadline_utc,
                    finished_at_utc,
                    outcome,
                    failure_kind,
                    result_code,
                    failure_owner,
                    failure_message,
                    required_action,
                    output_contract,
                    output_digest,
                    correlation_id
                ) VALUES (
                    :attempt_id,
                    :work_id,
                    1,
                    :lease_id,
                    :lease_token,
                    :worker_id,
                    :worker_build_identity,
                    'http_fetch',
                    :input_digest,
                    :source_key,
                    :source_policy_digest,
                    :source_permit_not_before_utc,
                    :issued_at_utc,
                    :expires_at_utc,
                    :heartbeat_deadline_utc,
                    :finished_at_utc,
                    'succeeded',
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    :correlation_id
                )
                """
            ),
            {
                "attempt_id": _id(f"{label}:attempt"),
                "work_id": values["work_id"],
                "lease_id": values["active_lease_id"],
                "lease_token": values["active_lease_token"],
                "worker_id": worker_id,
                "worker_build_identity": f"build-{label}",
                "input_digest": values["input_digest"],
                "source_key": source_key,
                "source_policy_digest": policy_digest,
                "source_permit_not_before_utc": _NOW,
                "issued_at_utc": _NOW,
                "expires_at_utc": _NOW + timedelta(minutes=5),
                "heartbeat_deadline_utc": _NOW + timedelta(minutes=1),
                "finished_at_utc": _NOW + timedelta(seconds=30),
                "correlation_id": f"correlation-{label}",
            },
        )
