from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from collection_infrastructure.postgres.metadata import collector_metadata
from collection_infrastructure.postgres.work_metadata import (
    collection_runs,
    stage_runs,
    work_units,
)

pipeline_advancement_metadata = collector_metadata

pipeline_advancements = sa.Table(
    "pipeline_advancements",
    pipeline_advancement_metadata,
    sa.Column("advancement_id", sa.Uuid(), primary_key=True),
    sa.Column(
        "source_work_unit_id",
        sa.Uuid(),
        sa.ForeignKey(work_units.c.work_id, ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "run_id",
        sa.Uuid(),
        sa.ForeignKey(collection_runs.c.run_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "stage_run_id",
        sa.Uuid(),
        sa.ForeignKey(stage_runs.c.stage_run_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("source_stage", sa.Text(), nullable=False),
    sa.Column("source_capability", sa.Text(), nullable=False),
    sa.Column("source_output_contract", sa.Text(), nullable=False),
    sa.Column("source_output_digest", sa.Text(), nullable=False),
    sa.Column("source_output_artifact_id", sa.Uuid(), nullable=False),
    sa.Column("source_output_artifact_role", sa.Text(), nullable=False),
    sa.Column("source_output_artifact_digest", sa.Text(), nullable=False),
    sa.Column("source_output_artifact_size_bytes", sa.BigInteger(), nullable=False),
    sa.Column("source_output_artifact_content_type", sa.Text(), nullable=False),
    sa.Column(
        "source_input_artifacts",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    ),
    sa.Column("transition_key", sa.Text(), nullable=False),
    sa.Column("transition_plan_digest", sa.Text(), nullable=False),
    sa.Column("state", sa.Text(), nullable=False),
    sa.Column("revision", sa.BigInteger(), nullable=False),
    sa.Column("attempt_count", sa.BigInteger(), nullable=False),
    sa.Column("result_digest", sa.Text(), nullable=True),
    sa.Column("blocker_owner", sa.Text(), nullable=True),
    sa.Column("blocker_code", sa.Text(), nullable=True),
    sa.Column("blocker_message", sa.Text(), nullable=True),
    sa.Column("blocker_required_action", sa.Text(), nullable=True),
    sa.Column("blocker_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column("active_lease_id", sa.Uuid(), nullable=True),
    sa.Column("active_lease_token_digest", sa.Text(), nullable=True),
    sa.Column("leased_by_worker_id", sa.Text(), nullable=True),
    sa.Column("dagster_execution_id", sa.Text(), nullable=True),
    sa.Column("dagster_build_id", sa.Text(), nullable=True),
    sa.Column("lease_issued_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("lease_expires_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text(), nullable=False),
    sa.CheckConstraint(
        "source_output_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_pipeline_advancements_output_digest",
    ),
    sa.CheckConstraint(
        "source_output_artifact_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_pipeline_advancements_artifact_digest",
    ),
    sa.CheckConstraint(
        "transition_plan_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_pipeline_advancements_plan_digest",
    ),
    sa.CheckConstraint(
        "result_digest IS NULL OR result_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_pipeline_advancements_result_digest",
    ),
    sa.CheckConstraint(
        "source_output_artifact_size_bytes >= 0",
        name="ck_pipeline_advancements_artifact_size",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(source_input_artifacts) = 'array'",
        name="ck_pipeline_advancements_input_artifacts",
    ),
    sa.CheckConstraint(
        "state IN ('pending', 'leased', 'applied', 'blocked')",
        name="ck_pipeline_advancements_state",
    ),
    sa.CheckConstraint(
        "revision >= 0 AND attempt_count >= 0",
        name="ck_pipeline_advancements_revisions",
    ),
    schema="work",
)

sa.Index(
    "ix_pipeline_advancements_claim",
    pipeline_advancements.c.state,
    pipeline_advancements.c.created_at_utc,
    pipeline_advancements.c.advancement_id,
)
sa.Index(
    "ix_pipeline_advancements_expiry",
    pipeline_advancements.c.state,
    pipeline_advancements.c.lease_expires_at_utc,
    postgresql_where=pipeline_advancements.c.state == "leased",
)
sa.Index(
    "ix_pipeline_advancements_run_state",
    pipeline_advancements.c.run_id,
    pipeline_advancements.c.state,
)

pipeline_advancement_attempts = sa.Table(
    "pipeline_advancement_attempts",
    pipeline_advancement_metadata,
    sa.Column("event_id", sa.Uuid(), primary_key=True),
    sa.Column(
        "advancement_id",
        sa.Uuid(),
        sa.ForeignKey(pipeline_advancements.c.advancement_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("attempt_number", sa.BigInteger(), nullable=False),
    sa.Column("event_kind", sa.Text(), nullable=False),
    sa.Column("lease_id", sa.Uuid(), nullable=True),
    sa.Column("lease_token_digest", sa.Text(), nullable=True),
    sa.Column("worker_id", sa.Text(), nullable=True),
    sa.Column("dagster_execution_id", sa.Text(), nullable=True),
    sa.Column("dagster_build_id", sa.Text(), nullable=True),
    sa.Column("transition_plan_digest", sa.Text(), nullable=False),
    sa.Column("lease_expires_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("result_digest", sa.Text(), nullable=True),
    sa.Column("blocker_owner", sa.Text(), nullable=True),
    sa.Column("blocker_code", sa.Text(), nullable=True),
    sa.Column("blocker_message", sa.Text(), nullable=True),
    sa.Column("blocker_required_action", sa.Text(), nullable=True),
    sa.Column("blocker_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column("occurred_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text(), nullable=False),
    sa.UniqueConstraint(
        "advancement_id",
        "attempt_number",
        "event_kind",
        name="uq_pipeline_advancement_attempts_event",
    ),
    schema="work",
)

sa.Index(
    "ix_pipeline_advancement_attempts_advancement",
    pipeline_advancement_attempts.c.advancement_id,
    pipeline_advancement_attempts.c.occurred_at_utc,
)

PIPELINE_ADVANCEMENT_TABLES = (
    pipeline_advancements,
    pipeline_advancement_attempts,
)
