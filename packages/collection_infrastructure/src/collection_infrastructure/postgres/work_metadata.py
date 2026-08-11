from __future__ import annotations

import sqlalchemy as sa

from collection_application import (
    CollectionRunState,
    SourceOperationalState,
    StageRunState,
    WorkAttemptOutcome,
    WorkCapability,
    WorkFailureKind,
    WorkStage,
    WorkUnitState,
    capability_belongs_to_stage,
)
from collection_infrastructure.postgres.metadata import collector_metadata

RUNS_SCHEMA = "runs"
WORK_SCHEMA = "work"
SOURCES_SCHEMA = "sources"


def _in_values(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def _stage_capability_check() -> str:
    clauses: list[str] = []
    for stage in WorkStage:
        capabilities = tuple(
            capability.value
            for capability in WorkCapability
            if capability_belongs_to_stage(stage, capability)
        )
        clauses.append(f"(stage = '{stage.value}' AND {_in_values('capability', capabilities)})")
    return " OR ".join(clauses)


_RUN_STATES = tuple(value.value for value in CollectionRunState)
_STAGE_RUN_STATES = tuple(value.value for value in StageRunState)
_WORK_STATES = tuple(value.value for value in WorkUnitState)
_WORK_STAGES = tuple(value.value for value in WorkStage)
_WORK_CAPABILITIES = tuple(value.value for value in WorkCapability)
_SOURCE_STATES = tuple(value.value for value in SourceOperationalState)
_ATTEMPT_OUTCOMES = tuple(value.value for value in WorkAttemptOutcome)
_FAILURE_KINDS = tuple(value.value for value in WorkFailureKind)
_STAGE_CAPABILITY_CHECK = _stage_capability_check()

collection_runs = sa.Table(
    "collection_runs",
    collector_metadata,
    sa.Column("run_id", sa.Uuid, primary_key=True),
    sa.Column("campaign_key", sa.Text, nullable=False),
    sa.Column(
        "config_bundle_digest",
        sa.Text,
        sa.ForeignKey("config.config_bundles.bundle_digest"),
        nullable=False,
    ),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("revision", sa.BigInteger, nullable=False),
    sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.CheckConstraint(
        "campaign_key ~ '^[a-z][a-z0-9_]{0,79}$'",
        name="ck_collection_runs_campaign_key_format",
    ),
    sa.CheckConstraint(
        "config_bundle_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_collection_runs_config_digest_format",
    ),
    sa.CheckConstraint(_in_values("state", _RUN_STATES), name="ck_collection_runs_state"),
    sa.CheckConstraint("revision >= 0", name="ck_collection_runs_revision"),
    sa.CheckConstraint(
        "updated_at_utc >= created_at_utc",
        name="ck_collection_runs_time_order",
    ),
    schema=RUNS_SCHEMA,
)

stage_runs = sa.Table(
    "stage_runs",
    collector_metadata,
    sa.Column("stage_run_id", sa.Uuid, primary_key=True),
    sa.Column(
        "run_id",
        sa.Uuid,
        sa.ForeignKey("runs.collection_runs.run_id"),
        nullable=False,
    ),
    sa.Column("stage", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("revision", sa.BigInteger, nullable=False),
    sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.CheckConstraint(_in_values("stage", _WORK_STAGES), name="ck_stage_runs_stage"),
    sa.CheckConstraint(
        _in_values("state", _STAGE_RUN_STATES),
        name="ck_stage_runs_state",
    ),
    sa.CheckConstraint("revision >= 0", name="ck_stage_runs_revision"),
    sa.CheckConstraint(
        "updated_at_utc >= created_at_utc",
        name="ck_stage_runs_time_order",
    ),
    sa.UniqueConstraint("run_id", "stage", name="uq_stage_runs_run_stage"),
    sa.UniqueConstraint(
        "stage_run_id",
        "run_id",
        "stage",
        name="uq_stage_runs_owner_identity",
    ),
    schema=RUNS_SCHEMA,
)

source_capacity_states = sa.Table(
    "source_capacity_states",
    collector_metadata,
    sa.Column("source_key", sa.Text, primary_key=True),
    sa.Column("policy_digest", sa.Text, nullable=False),
    sa.Column("operational_state", sa.Text, nullable=False),
    sa.Column("max_active_requests", sa.Integer, nullable=False),
    sa.Column("active_requests", sa.Integer, nullable=False),
    sa.Column("minimum_interval_milliseconds", sa.Integer, nullable=False),
    sa.Column("next_allowed_request_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("retry_after_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revision", sa.BigInteger, nullable=False),
    sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.CheckConstraint(
        "source_key ~ '^[a-z][a-z0-9_]{0,79}$'",
        name="ck_source_capacity_states_key_format",
    ),
    sa.CheckConstraint(
        "policy_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_source_capacity_states_policy_digest_format",
    ),
    sa.CheckConstraint(
        _in_values("operational_state", _SOURCE_STATES),
        name="ck_source_capacity_states_operational_state",
    ),
    sa.CheckConstraint(
        "max_active_requests BETWEEN 1 AND 10000",
        name="ck_source_capacity_states_max_active",
    ),
    sa.CheckConstraint(
        "active_requests BETWEEN 0 AND max_active_requests",
        name="ck_source_capacity_states_active",
    ),
    sa.CheckConstraint(
        "minimum_interval_milliseconds BETWEEN 0 AND 86400000",
        name="ck_source_capacity_states_minimum_interval",
    ),
    sa.CheckConstraint("revision >= 0", name="ck_source_capacity_states_revision"),
    schema=SOURCES_SCHEMA,
)

worker_registrations = sa.Table(
    "worker_registrations",
    collector_metadata,
    sa.Column("worker_id", sa.Text, primary_key=True),
    sa.Column("registration_digest", sa.Text, nullable=False),
    sa.Column("build_identity", sa.Text, nullable=False),
    sa.Column("max_concurrency", sa.Integer, nullable=False),
    sa.Column("resource_profile", sa.Text, nullable=False),
    sa.Column("registered_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.CheckConstraint(
        "worker_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'",
        name="ck_worker_registrations_id_format",
    ),
    sa.CheckConstraint(
        "registration_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_worker_registrations_digest_format",
    ),
    sa.CheckConstraint(
        "max_concurrency BETWEEN 1 AND 10000",
        name="ck_worker_registrations_max_concurrency",
    ),
    sa.UniqueConstraint(
        "registration_digest",
        name="uq_worker_registrations_digest",
    ),
    schema=WORK_SCHEMA,
)

worker_capabilities = sa.Table(
    "worker_capabilities",
    collector_metadata,
    sa.Column(
        "worker_id",
        sa.Text,
        sa.ForeignKey("work.worker_registrations.worker_id"),
        primary_key=True,
    ),
    sa.Column("capability", sa.Text, primary_key=True),
    sa.CheckConstraint(
        _in_values("capability", _WORK_CAPABILITIES),
        name="ck_worker_capabilities_capability",
    ),
    schema=WORK_SCHEMA,
)

worker_output_contracts = sa.Table(
    "worker_output_contracts",
    collector_metadata,
    sa.Column(
        "worker_id",
        sa.Text,
        sa.ForeignKey("work.worker_registrations.worker_id"),
        primary_key=True,
    ),
    sa.Column("output_contract", sa.Text, primary_key=True),
    sa.CheckConstraint(
        "output_contract ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'",
        name="ck_worker_output_contracts_identity",
    ),
    schema=WORK_SCHEMA,
)

worker_heartbeats = sa.Table(
    "worker_heartbeats",
    collector_metadata,
    sa.Column(
        "worker_id",
        sa.Text,
        sa.ForeignKey("work.worker_registrations.worker_id"),
        primary_key=True,
    ),
    sa.Column("last_seen_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("active_lease_count", sa.Integer, nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.CheckConstraint(
        "active_lease_count BETWEEN 0 AND 10000",
        name="ck_worker_heartbeats_active_lease_count",
    ),
    schema=WORK_SCHEMA,
)

work_units = sa.Table(
    "work_units",
    collector_metadata,
    sa.Column("work_id", sa.Uuid, primary_key=True),
    sa.Column("run_id", sa.Uuid, nullable=False),
    sa.Column("stage_run_id", sa.Uuid, nullable=False),
    sa.Column("stage", sa.Text, nullable=False),
    sa.Column("capability", sa.Text, nullable=False),
    sa.Column(
        "source_key",
        sa.Text,
        sa.ForeignKey("sources.source_capacity_states.source_key"),
        nullable=True,
    ),
    sa.Column("semantic_key", sa.Text, nullable=False),
    sa.Column("input_digest", sa.Text, nullable=False),
    sa.Column("expected_output_contract", sa.Text, nullable=False),
    sa.Column("priority", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("attempt_count", sa.Integer, nullable=False),
    sa.Column("failure_count", sa.Integer, nullable=False),
    sa.Column("max_attempts", sa.Integer, nullable=False),
    sa.Column("retry_initial_delay_seconds", sa.Integer, nullable=False),
    sa.Column("retry_multiplier", sa.Integer, nullable=False),
    sa.Column("retry_max_delay_seconds", sa.Integer, nullable=False),
    sa.Column("available_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("active_lease_id", sa.Uuid, nullable=True),
    sa.Column("active_lease_token", sa.Uuid, nullable=True),
    sa.Column(
        "active_worker_id",
        sa.Text,
        sa.ForeignKey("work.worker_registrations.worker_id"),
        nullable=True,
    ),
    sa.Column("lease_issued_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("lease_expires_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("heartbeat_deadline_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("source_policy_digest", sa.Text, nullable=True),
    sa.Column("source_permit_not_before_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("output_contract", sa.Text, nullable=True),
    sa.Column("output_digest", sa.Text, nullable=True),
    sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revision", sa.BigInteger, nullable=False),
    sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.ForeignKeyConstraint(
        ("stage_run_id", "run_id", "stage"),
        ("runs.stage_runs.stage_run_id", "runs.stage_runs.run_id", "runs.stage_runs.stage"),
        name="fk_work_units_stage_owner",
    ),
    sa.UniqueConstraint("run_id", "semantic_key", name="uq_work_units_run_semantic_key"),
    sa.CheckConstraint(_in_values("state", _WORK_STATES), name="ck_work_units_state"),
    sa.CheckConstraint(_in_values("stage", _WORK_STAGES), name="ck_work_units_stage"),
    sa.CheckConstraint(
        _in_values("capability", _WORK_CAPABILITIES),
        name="ck_work_units_capability",
    ),
    sa.CheckConstraint(_STAGE_CAPABILITY_CHECK, name="ck_work_units_stage_capability"),
    sa.CheckConstraint(
        "semantic_key ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_work_units_semantic_key_format",
    ),
    sa.CheckConstraint(
        "input_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_work_units_input_digest_format",
    ),
    sa.CheckConstraint(
        "expected_output_contract ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'",
        name="ck_work_units_expected_output_contract_format",
    ),
    sa.CheckConstraint(
        "source_policy_digest IS NULL OR source_policy_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_work_units_source_policy_digest_format",
    ),
    sa.CheckConstraint(
        "output_digest IS NULL OR output_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_work_units_output_digest_format",
    ),
    sa.CheckConstraint(
        "priority BETWEEN -1000000 AND 1000000",
        name="ck_work_units_priority",
    ),
    sa.CheckConstraint(
        "attempt_count >= 0 AND failure_count BETWEEN 0 AND max_attempts AND "
        "failure_count <= attempt_count AND max_attempts BETWEEN 1 AND 100",
        name="ck_work_units_attempt_budget",
    ),
    sa.CheckConstraint(
        "retry_initial_delay_seconds >= 1 AND retry_multiplier >= 1 AND "
        "retry_max_delay_seconds >= retry_initial_delay_seconds",
        name="ck_work_units_retry_policy",
    ),
    sa.CheckConstraint(
        "(state = 'leased' AND active_lease_id IS NOT NULL AND "
        "active_lease_token IS NOT NULL AND active_worker_id IS NOT NULL AND "
        "lease_issued_at_utc IS NOT NULL AND lease_expires_at_utc IS NOT NULL AND "
        "heartbeat_deadline_utc IS NOT NULL AND attempt_count >= 1 AND "
        "lease_issued_at_utc < heartbeat_deadline_utc AND "
        "heartbeat_deadline_utc <= lease_expires_at_utc) OR "
        "(state <> 'leased' AND active_lease_id IS NULL AND "
        "active_lease_token IS NULL AND active_worker_id IS NULL AND "
        "lease_issued_at_utc IS NULL AND lease_expires_at_utc IS NULL AND "
        "heartbeat_deadline_utc IS NULL)",
        name="ck_work_units_active_lease",
    ),
    sa.CheckConstraint(
        "(source_key IS NULL AND source_policy_digest IS NULL AND "
        "source_permit_not_before_utc IS NULL) OR "
        "(source_key IS NOT NULL AND ((state = 'leased' AND "
        "source_policy_digest IS NOT NULL AND source_permit_not_before_utc IS NOT NULL) OR "
        "(state <> 'leased' AND source_policy_digest IS NULL AND "
        "source_permit_not_before_utc IS NULL)))",
        name="ck_work_units_source_permit",
    ),
    sa.CheckConstraint(
        "(state = 'succeeded' AND output_contract IS NOT NULL AND "
        "output_digest IS NOT NULL AND completed_at_utc IS NOT NULL) OR "
        "(state <> 'succeeded' AND output_contract IS NULL AND "
        "output_digest IS NULL AND completed_at_utc IS NULL)",
        name="ck_work_units_output",
    ),
    sa.CheckConstraint("revision >= 0", name="ck_work_units_revision"),
    sa.CheckConstraint(
        "updated_at_utc >= created_at_utc",
        name="ck_work_units_time_order",
    ),
    schema=WORK_SCHEMA,
)

sa.Index(
    "ix_work_units_claim",
    work_units.c.capability,
    work_units.c.available_at_utc,
    work_units.c.priority.desc(),
    work_units.c.created_at_utc,
    work_units.c.work_id,
    postgresql_where=work_units.c.state.in_(("pending", "retry_wait")),
)
sa.Index(
    "ix_work_units_lease_expiry",
    work_units.c.lease_expires_at_utc,
    postgresql_where=work_units.c.state == "leased",
)
sa.Index(
    "uq_work_units_active_lease_id",
    work_units.c.active_lease_id,
    unique=True,
    postgresql_where=work_units.c.state == "leased",
)
sa.Index(
    "uq_work_units_active_lease_token",
    work_units.c.active_lease_token,
    unique=True,
    postgresql_where=work_units.c.state == "leased",
)

work_attempts = sa.Table(
    "work_attempts",
    collector_metadata,
    sa.Column("attempt_id", sa.Uuid, primary_key=True),
    sa.Column(
        "work_id",
        sa.Uuid,
        sa.ForeignKey("work.work_units.work_id"),
        nullable=False,
    ),
    sa.Column("attempt_number", sa.Integer, nullable=False),
    sa.Column("lease_id", sa.Uuid, nullable=False),
    sa.Column("lease_token", sa.Uuid, nullable=False),
    sa.Column(
        "worker_id",
        sa.Text,
        sa.ForeignKey("work.worker_registrations.worker_id"),
        nullable=False,
    ),
    sa.Column("worker_build_identity", sa.Text, nullable=False),
    sa.Column("capability", sa.Text, nullable=False),
    sa.Column("input_digest", sa.Text, nullable=False),
    sa.Column(
        "source_key",
        sa.Text,
        sa.ForeignKey("sources.source_capacity_states.source_key"),
        nullable=True,
    ),
    sa.Column("source_policy_digest", sa.Text, nullable=True),
    sa.Column("source_permit_not_before_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("issued_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("heartbeat_deadline_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("failure_kind", sa.Text, nullable=True),
    sa.Column("result_code", sa.Text, nullable=True),
    sa.Column("failure_owner", sa.Text, nullable=True),
    sa.Column("failure_message", sa.Text, nullable=True),
    sa.Column("required_action", sa.Text, nullable=True),
    sa.Column("output_contract", sa.Text, nullable=True),
    sa.Column("output_digest", sa.Text, nullable=True),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.UniqueConstraint("work_id", "attempt_number", name="uq_work_attempts_number"),
    sa.UniqueConstraint("lease_id", name="uq_work_attempts_lease_id"),
    sa.UniqueConstraint("lease_token", name="uq_work_attempts_lease_token"),
    sa.CheckConstraint("attempt_number >= 1", name="ck_work_attempts_number"),
    sa.CheckConstraint(
        _in_values("capability", _WORK_CAPABILITIES),
        name="ck_work_attempts_capability",
    ),
    sa.CheckConstraint(
        "input_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_work_attempts_input_digest_format",
    ),
    sa.CheckConstraint(
        "source_policy_digest IS NULL OR source_policy_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_work_attempts_source_policy_digest_format",
    ),
    sa.CheckConstraint(
        "output_digest IS NULL OR output_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_work_attempts_output_digest_format",
    ),
    sa.CheckConstraint(
        _in_values("outcome", _ATTEMPT_OUTCOMES),
        name="ck_work_attempts_outcome",
    ),
    sa.CheckConstraint(
        "failure_kind IS NULL OR " + _in_values("failure_kind", _FAILURE_KINDS),
        name="ck_work_attempts_failure_kind",
    ),
    sa.CheckConstraint(
        "result_code IS NULL OR result_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
        name="ck_work_attempts_result_code_format",
    ),
    sa.CheckConstraint(
        "issued_at_utc < heartbeat_deadline_utc AND heartbeat_deadline_utc <= expires_at_utc",
        name="ck_work_attempts_lease_time_order",
    ),
    sa.CheckConstraint(
        "(source_key IS NULL AND source_policy_digest IS NULL AND "
        "source_permit_not_before_utc IS NULL) OR "
        "(source_key IS NOT NULL AND source_policy_digest IS NOT NULL AND "
        "source_permit_not_before_utc IS NOT NULL)",
        name="ck_work_attempts_source_permit",
    ),
    sa.CheckConstraint(
        "(outcome = 'leased' AND finished_at_utc IS NULL AND failure_kind IS NULL AND "
        "result_code IS NULL AND failure_owner IS NULL AND failure_message IS NULL AND "
        "required_action IS NULL AND output_contract IS NULL AND output_digest IS NULL) OR "
        "(outcome = 'succeeded' AND finished_at_utc IS NOT NULL AND "
        "failure_kind IS NULL AND result_code IS NULL AND failure_owner IS NULL AND "
        "failure_message IS NULL AND required_action IS NULL AND "
        "output_contract IS NOT NULL AND output_digest IS NOT NULL) OR "
        "(outcome IN ('retry_scheduled', 'dead_lettered', 'blocked_by_policy') AND "
        "finished_at_utc IS NOT NULL AND failure_kind IS NOT NULL AND "
        "result_code IS NOT NULL AND failure_owner IS NOT NULL AND "
        "failure_message IS NOT NULL AND required_action IS NOT NULL AND "
        "output_contract IS NULL AND output_digest IS NULL) OR "
        "(outcome IN ('released', 'expired') AND finished_at_utc IS NOT NULL AND "
        "failure_kind IS NULL AND result_code IS NOT NULL AND failure_owner IS NULL AND "
        "failure_message IS NULL AND required_action IS NULL AND "
        "output_contract IS NULL AND output_digest IS NULL)",
        name="ck_work_attempts_result_shape",
    ),
    schema=WORK_SCHEMA,
)

dead_letters = sa.Table(
    "dead_letters",
    collector_metadata,
    sa.Column(
        "work_id",
        sa.Uuid,
        sa.ForeignKey("work.work_units.work_id"),
        primary_key=True,
    ),
    sa.Column(
        "attempt_id",
        sa.Uuid,
        sa.ForeignKey("work.work_attempts.attempt_id"),
        nullable=False,
    ),
    sa.Column("failure_kind", sa.Text, nullable=False),
    sa.Column("code", sa.Text, nullable=False),
    sa.Column("owner", sa.Text, nullable=False),
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("required_action", sa.Text, nullable=False),
    sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.CheckConstraint(
        "failure_kind IN ('transient', 'permanent', 'contract_invalid')",
        name="ck_dead_letters_failure_kind",
    ),
    sa.UniqueConstraint("attempt_id", name="uq_dead_letters_attempt_id"),
    sa.CheckConstraint(
        "code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
        name="ck_dead_letters_code_format",
    ),
    schema=WORK_SCHEMA,
)

RUN_TABLES = (collection_runs, stage_runs)
SOURCE_TABLES = (source_capacity_states,)
WORK_TABLES = (
    worker_registrations,
    worker_capabilities,
    worker_output_contracts,
    worker_heartbeats,
    work_units,
    work_attempts,
    dead_letters,
)
WORK_ENGINE_TABLES = RUN_TABLES + SOURCE_TABLES + WORK_TABLES
