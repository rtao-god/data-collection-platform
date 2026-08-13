"""Add manual-import admission and orphan-cleanup ownership.

Revision ID: 20260813_0008
Revises: 20260812_0007
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0008"
down_revision: str | None = "20260812_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS manual_import")
    op.create_table(
        "artifact_cleanup_tombstones",
        sa.Column("tombstone_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("upload_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_reference", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("eligible_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_expires_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("retry_not_before_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_digest", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "reason IN ('orphan_staging', 'orphan_verified')",
            name="ck_artifact_cleanup_tombstones_reason",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'retry_wait', 'deleted', 'failed')",
            name="ck_artifact_cleanup_tombstones_state",
        ),
        sa.CheckConstraint(
            "char_length(storage_reference) BETWEEN 1 AND 512 AND "
            "storage_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]*$'",
            name="ck_artifact_cleanup_tombstones_storage_reference",
        ),
        sa.CheckConstraint(
            "attempt_count >= 1 AND revision >= 0",
            name="ck_artifact_cleanup_tombstones_counters",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{0,99}$'",
            name="ck_artifact_cleanup_tombstones_error_code",
        ),
        sa.CheckConstraint(
            "error_digest IS NULL OR error_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_artifact_cleanup_tombstones_error_digest",
        ),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["sources.artifact_uploads.upload_id"],
            name="fk_artifact_cleanup_tombstones_upload_id_artifact_uploads",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tombstone_id",
            name="pk_artifact_cleanup_tombstones",
        ),
        sa.UniqueConstraint(
            "upload_id",
            name="uq_artifact_cleanup_tombstones_upload_id",
        ),
        schema="sources",
    )
    op.create_index(
        "ix_artifact_cleanup_tombstones_claim",
        "artifact_cleanup_tombstones",
        ["state", "retry_not_before_utc", "claim_expires_at_utc"],
        schema="sources",
    )

    op.create_table(
        "plan_admissions",
        sa.Column("admission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_digest", sa.Text(), nullable=False),
        sa.Column("source_digest", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("plan_status", sa.Text(), nullable=False),
        sa.Column("target_stage", sa.Text(), nullable=False),
        sa.Column("target_capability", sa.Text(), nullable=False),
        sa.Column("target_output_contract", sa.Text(), nullable=False),
        sa.Column("total_record_count", sa.Integer(), nullable=False),
        sa.Column("accepted_record_count", sa.Integer(), nullable=False),
        sa.Column("rejected_record_count", sa.Integer(), nullable=False),
        sa.Column("child_work_count", sa.Integer(), nullable=False),
        sa.Column("result_digest", sa.Text(), nullable=False),
        sa.Column("admitted_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("plan_status = 'ready'", name="ck_plan_admissions_ready"),
        sa.CheckConstraint(
            "accepted_record_count + rejected_record_count = total_record_count",
            name="ck_plan_admissions_counts",
        ),
        sa.CheckConstraint(
            "accepted_record_count = child_work_count",
            name="ck_plan_admissions_child_count",
        ),
        sa.CheckConstraint(
            "plan_digest ~ '^sha256:[0-9a-f]{64}$' AND "
            "source_digest ~ '^sha256:[0-9a-f]{64}$' AND "
            "result_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_plan_admissions_digest_format",
        ),
        sa.ForeignKeyConstraint(
            ["parent_work_id"],
            ["work.work_units.work_id"],
            name="fk_plan_admissions_parent_work_id_work_units",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_artifact_id"],
            ["sources.artifact_records.artifact_id"],
            name="fk_plan_admissions_plan_artifact_id_artifact_records",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"],
            ["sources.artifact_records.artifact_id"],
            name="fk_plan_admissions_source_artifact_id_artifact_records",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("admission_id", name="pk_plan_admissions"),
        sa.UniqueConstraint(
            "parent_work_id",
            "plan_artifact_id",
            name="uq_plan_admissions_parent_plan",
        ),
        schema="manual_import",
    )
    op.create_table(
        "plan_admission_items",
        sa.Column("admission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("child_work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("locator_kind", sa.Text(), nullable=False),
        sa.Column("locator_value", sa.Text(), nullable=False),
        sa.Column("record_digest", sa.Text(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_plan_admission_items_position"),
        sa.CheckConstraint(
            "record_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_plan_admission_items_record_digest_format",
        ),
        sa.ForeignKeyConstraint(
            ["admission_id"],
            ["manual_import.plan_admissions.admission_id"],
            name="fk_plan_admission_items_admission_id_plan_admissions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["child_work_id"],
            ["work.work_units.work_id"],
            name="fk_plan_admission_items_child_work_id_work_units",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "admission_id",
            "position",
            name="pk_plan_admission_items",
        ),
        sa.UniqueConstraint(
            "child_work_id",
            name="uq_plan_admission_items_child_work_id",
        ),
        schema="manual_import",
    )


def downgrade() -> None:
    op.drop_table("plan_admission_items", schema="manual_import")
    op.drop_table("plan_admissions", schema="manual_import")
    op.drop_index(
        "ix_artifact_cleanup_tombstones_claim",
        table_name="artifact_cleanup_tombstones",
        schema="sources",
    )
    op.drop_table("artifact_cleanup_tombstones", schema="sources")
    op.execute("DROP SCHEMA IF EXISTS manual_import")
