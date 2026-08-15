from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

pipeline_advancement_metadata = sa.MetaData()

_work_units = sa.Table(
    "work_units",
    pipeline_advancement_metadata,
    sa.Column("work_id", sa.Uuid(), primary_key=True),
    schema="work",
)
_collection_runs = sa.Table(
    "collection_runs",
    pipeline_advancement_metadata,
    sa.Column("run_id", sa.Uuid(), primary_key=True),
    schema="runs",
)
_stage_runs = sa.Table(
    "stage_runs",
    pipeline_advancement_metadata,
    sa.Column("stage_run_id", sa.Uuid(), primary_key=True),
    schema="runs",
)

pipeline_advancements = sa.Table(
    "pipeline_advancements",
    pipeline_advancement_metadata,
    sa.Column("advancement_id", sa.Uuid(), primary_key=True),
    sa.Column(
        "source_work_unit_id",
        sa.Uuid(),
        sa.ForeignKey(_work_units.c.work_id, ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "run_id",
        sa.Uuid(),
        sa.ForeignKey(_collection_runs.c.run_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "stage_run_id",
        sa.Uuid(),
        sa.ForeignKey(_stage_runs.c.stage_run_id, ondelete="RESTRICT"),
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
    schema="work",
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

PIPELINE_ADVANCEMENT_TABLES = (
    pipeline_advancements,
    pipeline_advancement_attempts,
)
