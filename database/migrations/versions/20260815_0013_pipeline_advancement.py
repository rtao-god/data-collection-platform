"""Add durable pipeline advancement checkpoint and append-only attempt events.

Revision ID: 20260815_0013
Revises: 20260815_0012
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260815_0013"
down_revision: str | None = "20260815_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_advancements",
        sa.Column("advancement_id", sa.Uuid(), nullable=False),
        sa.Column("source_work_unit_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("stage_run_id", sa.Uuid(), nullable=False),
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
        sa.Column(
            "blocker_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
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
        sa.CheckConstraint(
            "(state = 'leased') = (active_lease_id IS NOT NULL)",
            name="ck_pipeline_advancements_lease_state",
        ),
        sa.CheckConstraint(
            "(active_lease_id IS NULL AND active_lease_token_digest IS NULL "
            "AND leased_by_worker_id IS NULL AND dagster_execution_id IS NULL "
            "AND dagster_build_id IS NULL AND lease_issued_at_utc IS NULL "
            "AND lease_expires_at_utc IS NULL) OR "
            "(active_lease_id IS NOT NULL AND active_lease_token_digest IS NOT NULL "
            "AND leased_by_worker_id IS NOT NULL AND dagster_execution_id IS NOT NULL "
            "AND dagster_build_id IS NOT NULL AND lease_issued_at_utc IS NOT NULL "
            "AND lease_expires_at_utc IS NOT NULL "
            "AND lease_expires_at_utc > lease_issued_at_utc)",
            name="ck_pipeline_advancements_lease_payload",
        ),
        sa.CheckConstraint(
            "(blocker_owner IS NULL AND blocker_code IS NULL AND blocker_message IS NULL "
            "AND blocker_required_action IS NULL AND blocker_context IS NULL) OR "
            "(blocker_owner IS NOT NULL AND blocker_code IS NOT NULL "
            "AND blocker_message IS NOT NULL AND blocker_required_action IS NOT NULL "
            "AND blocker_context IS NOT NULL)",
            name="ck_pipeline_advancements_blocker_payload",
        ),
        sa.CheckConstraint(
            "(state = 'applied' AND result_digest IS NOT NULL AND blocker_code IS NULL) OR "
            "(state = 'blocked' AND result_digest IS NULL AND blocker_code IS NOT NULL) OR "
            "(state IN ('pending', 'leased') AND result_digest IS NULL "
            "AND blocker_code IS NULL)",
            name="ck_pipeline_advancements_terminal_payload",
        ),
        sa.ForeignKeyConstraint(
            ("source_work_unit_id",),
            ("work.work_units.work_id",),
            name="fk_pipeline_advancements_source_work_unit",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("run_id",),
            ("runs.collection_runs.run_id",),
            name="fk_pipeline_advancements_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("stage_run_id",),
            ("runs.stage_runs.stage_run_id",),
            name="fk_pipeline_advancements_stage_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("advancement_id", name="pk_pipeline_advancements"),
        sa.UniqueConstraint(
            "source_work_unit_id",
            name="uq_pipeline_advancements_source_work_unit",
        ),
        schema="work",
    )
    op.create_index(
        "ix_pipeline_advancements_claim",
        "pipeline_advancements",
        ("state", "created_at_utc", "advancement_id"),
        schema="work",
    )
    op.create_index(
        "ix_pipeline_advancements_expiry",
        "pipeline_advancements",
        ("state", "lease_expires_at_utc"),
        schema="work",
        postgresql_where=sa.text("state = 'leased'"),
    )
    op.create_index(
        "ix_pipeline_advancements_run_state",
        "pipeline_advancements",
        ("run_id", "state"),
        schema="work",
    )

    op.create_table(
        "pipeline_advancement_attempts",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("advancement_id", sa.Uuid(), nullable=False),
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
        sa.Column(
            "blocker_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("occurred_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "attempt_number >= 0",
            name="ck_pipeline_advancement_attempts_number",
        ),
        sa.CheckConstraint(
            "event_kind IN ('registered_block', 'leased', 'expired', 'applied', 'blocked')",
            name="ck_pipeline_advancement_attempts_kind",
        ),
        sa.CheckConstraint(
            "transition_plan_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_pipeline_advancement_attempts_plan_digest",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR lease_token_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_pipeline_advancement_attempts_lease_digest",
        ),
        sa.CheckConstraint(
            "result_digest IS NULL OR result_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_pipeline_advancement_attempts_result_digest",
        ),
        sa.CheckConstraint(
            "(blocker_owner IS NULL AND blocker_code IS NULL AND blocker_message IS NULL "
            "AND blocker_required_action IS NULL AND blocker_context IS NULL) OR "
            "(blocker_owner IS NOT NULL AND blocker_code IS NOT NULL "
            "AND blocker_message IS NOT NULL AND blocker_required_action IS NOT NULL "
            "AND blocker_context IS NOT NULL)",
            name="ck_pipeline_advancement_attempts_blocker_payload",
        ),
        sa.CheckConstraint(
            "(event_kind = 'registered_block' AND attempt_number = 0 "
            "AND lease_id IS NULL AND blocker_code IS NOT NULL) OR "
            "(event_kind = 'leased' AND attempt_number > 0 AND lease_id IS NOT NULL "
            "AND lease_token_digest IS NOT NULL AND worker_id IS NOT NULL "
            "AND dagster_execution_id IS NOT NULL AND dagster_build_id IS NOT NULL "
            "AND lease_expires_at_utc IS NOT NULL AND result_digest IS NULL "
            "AND blocker_code IS NULL) OR "
            "(event_kind = 'expired' AND attempt_number > 0 AND lease_id IS NOT NULL "
            "AND result_digest IS NULL AND blocker_code IS NULL) OR "
            "(event_kind = 'applied' AND attempt_number > 0 AND lease_id IS NOT NULL "
            "AND result_digest IS NOT NULL AND blocker_code IS NULL) OR "
            "(event_kind = 'blocked' AND attempt_number > 0 AND lease_id IS NOT NULL "
            "AND result_digest IS NULL AND blocker_code IS NOT NULL)",
            name="ck_pipeline_advancement_attempts_event_payload",
        ),
        sa.ForeignKeyConstraint(
            ("advancement_id",),
            ("work.pipeline_advancements.advancement_id",),
            name="fk_pipeline_advancement_attempts_advancement",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_pipeline_advancement_attempts"),
        sa.UniqueConstraint(
            "advancement_id",
            "attempt_number",
            "event_kind",
            name="uq_pipeline_advancement_attempts_event",
        ),
        schema="work",
    )
    op.create_index(
        "ix_pipeline_advancement_attempts_advancement",
        "pipeline_advancement_attempts",
        ("advancement_id", "occurred_at_utc"),
        schema="work",
    )
    op.execute(
        """
        CREATE FUNCTION work.reject_pipeline_advancement_attempt_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'pipeline advancement attempt history is immutable'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pipeline_advancement_attempts_immutable
        BEFORE UPDATE OR DELETE ON work.pipeline_advancement_attempts
        FOR EACH ROW EXECUTE FUNCTION work.reject_pipeline_advancement_attempt_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_pipeline_advancement_attempts_immutable "
        "ON work.pipeline_advancement_attempts"
    )
    op.execute("DROP FUNCTION work.reject_pipeline_advancement_attempt_mutation()")
    op.drop_index(
        "ix_pipeline_advancement_attempts_advancement",
        table_name="pipeline_advancement_attempts",
        schema="work",
    )
    op.drop_table("pipeline_advancement_attempts", schema="work")
    op.drop_index(
        "ix_pipeline_advancements_run_state",
        table_name="pipeline_advancements",
        schema="work",
    )
    op.drop_index(
        "ix_pipeline_advancements_expiry",
        table_name="pipeline_advancements",
        schema="work",
    )
    op.drop_index(
        "ix_pipeline_advancements_claim",
        table_name="pipeline_advancements",
        schema="work",
    )
    op.drop_table("pipeline_advancements", schema="work")
