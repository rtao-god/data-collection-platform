from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from collection_application import CollectionRunState, WorkUnitState
from collection_application.run_control import TransitionCollectionRun
from collection_infrastructure.postgres import PostgresRunControlRepository

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


def _table(connection: sa.Connection, schema: str, name: str) -> sa.Table:
    return sa.Table(name, sa.MetaData(), schema=schema, autoload_with=connection)


def _insert(
    connection: sa.Connection,
    schema: str,
    name: str,
    values: dict[str, object],
) -> None:
    connection.execute(sa.insert(_table(connection, schema, name)).values(**values))


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


def _insert_config(connection: sa.Connection, label: str) -> str:
    bundle_digest = _digest(f"{label}:config")
    _insert_config_artifact(connection, bundle_digest, recorded_at_utc=_NOW)
    _insert(
        connection,
        "config",
        "config_bundle_components",
        {
            "bundle_digest": bundle_digest,
            "position": 0,
            "path": "campaign.yaml",
            "component_digest": _digest(f"{label}:component"),
        },
    )
    _insert(
        connection,
        "config",
        "config_bundles",
        {
            "bundle_digest": bundle_digest,
            "campaign_key": f"campaign_{sha256(label.encode('utf-8')).hexdigest()[:12]}",
            "contract": "collector-campaign-snapshot",
            "contract_revision": "campaign-snapshot-v1",
            "readiness": "ready",
            "recorded_at_utc": _NOW,
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
    correlation_id = f"correlation-{label}"
    _insert(
        connection,
        "runs",
        "collection_runs",
        {
            "run_id": run_id,
            "campaign_key": f"campaign_{sha256(label.encode('utf-8')).hexdigest()[:12]}",
            "config_bundle_digest": _insert_config(connection, label),
            "state": "running",
            "revision": 0,
            "created_at_utc": _NOW,
            "updated_at_utc": _NOW,
            "correlation_id": correlation_id,
        },
    )
    _insert(
        connection,
        "runs",
        "stage_runs",
        {
            "stage_run_id": stage_run_id,
            "run_id": run_id,
            "stage": stage,
            "state": "running",
            "revision": 0,
            "created_at_utc": _NOW,
            "updated_at_utc": _NOW,
            "correlation_id": correlation_id,
        },
    )
    return run_id, stage_run_id


def _insert_source(connection: sa.Connection, label: str) -> tuple[str, str]:
    source_key = _source_key(label)
    policy_digest = _digest(f"{label}:policy")
    _insert(
        connection,
        "sources",
        "source_capacity_states",
        {
            "source_key": source_key,
            "policy_digest": policy_digest,
            "operational_state": "active",
            "max_active_requests": 2,
            "active_requests": 0,
            "minimum_interval_milliseconds": 100,
            "next_allowed_request_at_utc": _NOW,
            "retry_after_utc": None,
            "revision": 0,
            "updated_at_utc": _NOW,
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
    correlation_id = f"correlation-{label}"
    _insert(
        connection,
        "work",
        "worker_registrations",
        {
            "worker_id": worker_id,
            "registration_digest": _digest(f"{label}:worker-registration"),
            "build_identity": f"build-{label}",
            "max_concurrency": 2,
            "resource_profile": "integration-test",
            "registered_at_utc": _NOW,
            "correlation_id": correlation_id,
        },
    )
    _insert(
        connection,
        "work",
        "worker_capabilities",
        {"worker_id": worker_id, "capability": capability},
    )
    _insert(
        connection,
        "work",
        "worker_output_contracts",
        {"worker_id": worker_id, "output_contract": output_contract},
    )
    _insert(
        connection,
        "work",
        "worker_heartbeats",
        {
            "worker_id": worker_id,
            "last_seen_at_utc": _NOW,
            "active_lease_count": 0,
            "correlation_id": correlation_id,
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
        "active_lease_id": None,
        "active_lease_token": None,
        "active_worker_id": None,
        "lease_issued_at_utc": None,
        "lease_expires_at_utc": None,
        "heartbeat_deadline_utc": None,
        "source_policy_digest": None,
        "source_permit_not_before_utc": None,
        "output_contract": None,
        "output_digest": None,
        "completed_at_utc": None,
        "revision": 0,
        "created_at_utc": _NOW,
        "updated_at_utc": _NOW,
        "correlation_id": f"correlation-{label}",
    }


def _insert_work(connection: sa.Connection, values: dict[str, object]) -> None:
    _insert(connection, "work", "work_units", values)


def test_fresh_migration_creates_exact_work_engine_contract() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)
    inspector = sa.inspect(engine)

    assert set(inspector.get_table_names(schema="runs")) == {
        "collection_run_transitions",
        "collection_runs",
        "stage_runs",
    }
    assert set(inspector.get_table_names(schema="sources")) == {
        "artifact_cleanup_tombstones",
        "artifact_objects",
        "artifact_records",
        "artifact_uploads",
        "source_capacity_states",
    }
    assert set(inspector.get_table_names(schema="manual_import")) == {
        "plan_admission_items",
        "plan_admissions",
    }
    assert set(inspector.get_table_names(schema="work")) == {
        "dead_letters",
        "pipeline_advancement_attempts",
        "pipeline_advancements",
        "work_attempts",
        "work_input_artifacts",
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
    assert {
        column["name"] for column in inspector.get_columns("artifact_objects", schema="sources")
    } == {
        "object_id",
        "artifact_kind",
        "content_digest",
        "size_bytes",
        "storage_reference",
        "verified_at_utc",
        "recorded_at_utc",
        "correlation_id",
    }
    assert {
        column["name"] for column in inspector.get_columns("artifact_records", schema="sources")
    } == {
        "artifact_id",
        "object_id",
        "upload_id",
        "work_id",
        "attempt_id",
        "worker_id",
        "producer_kind",
        "producer_identity",
        "owner_operation_id",
        "content_type",
        "source_policy_digest",
        "recorded_at_utc",
        "correlation_id",
    }
    assert {
        column["name"] for column in inspector.get_columns("work_input_artifacts", schema="work")
    } == {"work_id", "position", "artifact_id", "role"}
    assert {
        column["name"] for column in inspector.get_columns("work_output_artifacts", schema="work")
    } == {"work_id", "position", "artifact_id", "role"}
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("artifact_objects", schema="sources")
    } == {
        "uq_artifact_objects_kind_content_digest",
        "uq_artifact_objects_storage_reference",
    }
    assert {
        index["name"] for index in inspector.get_indexes("artifact_uploads", schema="sources")
    } >= {"ix_artifact_uploads_orphan_candidates"}
    assert {
        index["name"]
        for index in inspector.get_indexes("artifact_cleanup_tombstones", schema="sources")
    } == {
        "ix_artifact_cleanup_tombstones_claim",
        "uq_artifact_cleanup_tombstones_upload_id",
    }


def test_run_control_persists_revisioned_transitions_and_cancels_pending_work() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)
    label = "run-control-transition"
    changed_at_utc = _NOW + timedelta(minutes=1)

    with engine.begin() as connection:
        run_id, stage_run_id = _insert_run_stage(connection, label, stage="extraction")
        _insert_work(
            connection,
            _pending_work_values(
                label,
                run_id=run_id,
                stage_run_id=stage_run_id,
                stage="extraction",
                capability="extraction",
                source_key=None,
            ),
        )

    repository = PostgresRunControlRepository(
        engine,
        clock=lambda: changed_at_utc,
        uuid_factory=uuid4,
    )
    paused = repository.transition(
        TransitionCollectionRun(
            run_id=run_id,
            expected_revision=0,
            requested_state=CollectionRunState.PAUSED,
            actor_id="operator-1",
            reason="Controlled integration pause.",
            correlation_id="run-control-pause",
        )
    )

    assert paused.state is CollectionRunState.PAUSED
    assert paused.revision == 1
    assert repository.coverage(run_id).stages[0].pending == 1

    cancelled = repository.transition(
        TransitionCollectionRun(
            run_id=run_id,
            expected_revision=1,
            requested_state=CollectionRunState.CANCELLED,
            actor_id="operator-1",
            reason="Cancel remaining pending work.",
            correlation_id="run-control-cancel",
        )
    )

    assert cancelled.state is CollectionRunState.CANCELLED
    assert cancelled.revision == 2
    assert cancelled.stages[0].work_counts[0].state is WorkUnitState.CANCELLED
    assert cancelled.stages[0].work_counts[0].count == 1

    with engine.begin() as connection:
        transitions = _table(connection, "runs", "collection_run_transitions")
        rows = (
            connection.execute(
                sa.select(transitions)
                .where(transitions.c.run_id == run_id)
                .order_by(transitions.c.to_revision)
            )
            .mappings()
            .all()
        )
        assert [(row["from_state"], row["to_state"]) for row in rows] == [
            ("running", "paused"),
            ("paused", "cancelled"),
        ]

    with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
        transitions = _table(connection, "runs", "collection_run_transitions")
        connection.execute(
            sa.update(transitions)
            .where(transitions.c.run_id == run_id)
            .values(reason="Mutation must fail.")
        )


def test_worker_output_contract_identity_is_fail_closed() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)

    with engine.begin() as connection:
        worker_id = _insert_worker(connection, "worker-contract", "extraction")

    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert(
            connection,
            "work",
            "worker_output_contracts",
            {"worker_id": worker_id, "output_contract": "invalid contract"},
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
        _insert_work(connection, values)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        invalid = dict(values)
        invalid["work_id"] = _id("safe-attempt-budget:invalid")
        invalid["semantic_key"] = _digest("safe-attempt-budget:invalid")
        invalid["failure_count"] = 6
        _insert_work(connection, invalid)


def test_source_capability_contract_rejects_missing_or_extraneous_source() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)

    with engine.begin() as connection:
        run_id, stage_run_id = _insert_run_stage(
            connection,
            "missing-source",
            stage="acquisition",
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert_work(
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
        _insert_work(
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
        _insert_work(
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
        _insert_work(connection, values)

    duplicate = dict(values)
    duplicate["work_id"] = _id("semantic-identity:duplicate-work")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert_work(connection, duplicate)


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
        _insert_work(connection, values)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert(
            connection,
            "work",
            "work_attempts",
            {
                "attempt_id": _id(f"{label}:attempt"),
                "work_id": values["work_id"],
                "attempt_number": 1,
                "lease_id": values["active_lease_id"],
                "lease_token": values["active_lease_token"],
                "worker_id": worker_id,
                "worker_build_identity": f"build-{label}",
                "capability": "http_fetch",
                "input_digest": values["input_digest"],
                "source_key": source_key,
                "source_policy_digest": policy_digest,
                "source_permit_not_before_utc": _NOW,
                "issued_at_utc": _NOW,
                "expires_at_utc": _NOW + timedelta(minutes=5),
                "heartbeat_deadline_utc": _NOW + timedelta(minutes=1),
                "finished_at_utc": _NOW + timedelta(seconds=30),
                "outcome": "succeeded",
                "failure_kind": None,
                "result_code": None,
                "failure_owner": None,
                "failure_message": None,
                "required_action": None,
                "output_contract": None,
                "output_digest": None,
                "correlation_id": f"correlation-{label}",
            },
        )
