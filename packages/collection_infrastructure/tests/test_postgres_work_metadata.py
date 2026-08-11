from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from collection_infrastructure.postgres import (
    RUNS_SCHEMA,
    RUN_TABLES,
    SOURCES_SCHEMA,
    SOURCE_CAPABILITY_CONSTRAINTS,
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


def test_work_metadata_has_exact_owner_schemas_and_tables() -> None:
    assert RUNS_SCHEMA == "runs"
    assert SOURCES_SCHEMA == "sources"
    assert WORK_SCHEMA == "work"
    assert tuple(table.fullname for table in RUN_TABLES) == (
        "runs.collection_runs",
        "runs.stage_runs",
    )
    assert tuple(table.fullname for table in SOURCE_TABLES) == (
        "sources.source_capacity_states",
    )
    assert tuple(table.fullname for table in WORK_TABLES) == (
        "work.worker_registrations",
        "work.worker_capabilities",
        "work.worker_heartbeats",
        "work.work_units",
        "work.work_attempts",
        "work.dead_letters",
    )
    assert WORK_ENGINE_TABLES == RUN_TABLES + SOURCE_TABLES + WORK_TABLES
    assert {constraint.name for constraint in SOURCE_CAPABILITY_CONSTRAINTS} == {
        "ck_work_attempts_source_capability",
        "ck_work_units_source_capability",
    }


def test_work_metadata_preserves_owner_identity_without_cascade_delete() -> None:
    for table in WORK_ENGINE_TABLES:
        assert all(foreign_key.ondelete is None for foreign_key in table.foreign_keys)
        assert all(column.server_default is None for column in table.columns)

    assert collection_runs.primary_key.columns.keys() == ["run_id"]
    assert stage_runs.primary_key.columns.keys() == ["stage_run_id"]
    assert source_capacity_states.primary_key.columns.keys() == ["source_key"]
    assert worker_registrations.primary_key.columns.keys() == ["worker_id"]
    assert worker_capabilities.primary_key.columns.keys() == ["worker_id", "capability"]
    assert worker_heartbeats.primary_key.columns.keys() == ["worker_id"]
    assert work_units.primary_key.columns.keys() == ["work_id"]
    assert work_attempts.primary_key.columns.keys() == ["attempt_id"]
    assert dead_letters.primary_key.columns.keys() == ["work_id"]


def test_work_unit_ddl_contains_fail_closed_lease_and_idempotency_contracts() -> None:
    dialect = postgresql.dialect()
    sql = str(CreateTable(work_units).compile(dialect=dialect))
    indexes = {
        index.name: str(CreateIndex(index).compile(dialect=dialect))
        for index in work_units.indexes
    }

    assert "CONSTRAINT fk_work_units_stage_owner" in sql
    assert "CONSTRAINT uq_work_units_run_semantic_key" in sql
    assert "CONSTRAINT ck_work_units_stage_capability" in sql
    assert "CONSTRAINT ck_work_units_source_capability" in sql
    assert "CONSTRAINT ck_work_units_active_lease" in sql
    assert "CONSTRAINT ck_work_units_source_permit" in sql
    assert "CONSTRAINT ck_work_units_output" in sql
    assert "active_lease_token IS NOT NULL" in sql
    assert "state <> 'leased' AND active_lease_id IS NULL" in sql
    assert "source_policy_digest ~ '^sha256:[0-9a-f]{64}$'" in sql
    assert "capability IN ('manual_import', 'osm_query', 'http_fetch', 'browser_fetch')" in sql
    assert set(indexes) == {
        "ix_work_units_claim",
        "ix_work_units_lease_expiry",
        "uq_work_units_active_lease_id",
        "uq_work_units_active_lease_token",
    }
    assert "WHERE state IN ('pending', 'retry_wait')" in indexes["ix_work_units_claim"]
    assert "WHERE state = 'leased'" in indexes["uq_work_units_active_lease_token"]


def test_attempt_ddl_requires_one_typed_result_shape() -> None:
    sql = str(CreateTable(work_attempts).compile(dialect=postgresql.dialect()))

    assert "CONSTRAINT uq_work_attempts_number" in sql
    assert "CONSTRAINT uq_work_attempts_lease_id" in sql
    assert "CONSTRAINT uq_work_attempts_lease_token" in sql
    assert "CONSTRAINT ck_work_attempts_source_capability" in sql
    assert "CONSTRAINT ck_work_attempts_result_shape" in sql
    assert "outcome = 'leased'" in sql
    assert "outcome = 'succeeded'" in sql
    assert "'retry_scheduled', 'dead_lettered', 'blocked_by_policy'" in sql
    assert "outcome IN ('released', 'expired')" in sql


def test_source_capacity_ddl_is_centralized_and_bounded() -> None:
    sql = str(CreateTable(source_capacity_states).compile(dialect=postgresql.dialect()))

    assert "operational_state IN ('active', 'suspended', 'circuit_open')" in sql
    assert "active_requests BETWEEN 0 AND max_active_requests" in sql
    assert "minimum_interval_milliseconds BETWEEN 0 AND 86400000" in sql
    assert "policy_digest ~ '^sha256:[0-9a-f]{64}$'" in sql
