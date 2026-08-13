from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from collection_infrastructure.postgres.artifact_metadata import artifact_records
from collection_infrastructure.postgres.metadata import collector_metadata
from collection_infrastructure.postgres.work_metadata import work_units

MANUAL_IMPORT_SCHEMA = "manual_import"
manual_import_metadata = collector_metadata

plan_admissions = sa.Table(
    "plan_admissions",
    manual_import_metadata,
    sa.Column("admission_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "parent_work_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(work_units.c.work_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column(
        "plan_artifact_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(artifact_records.c.artifact_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "source_artifact_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(artifact_records.c.artifact_id, ondelete="RESTRICT"),
        nullable=False,
    ),
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
    sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
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
    sa.UniqueConstraint(
        "parent_work_id",
        "plan_artifact_id",
        name="uq_plan_admissions_parent_plan",
    ),
    schema=MANUAL_IMPORT_SCHEMA,
)

plan_admission_items = sa.Table(
    "plan_admission_items",
    manual_import_metadata,
    sa.Column(
        "admission_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(plan_admissions.c.admission_id, ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Column("position", sa.Integer(), primary_key=True),
    sa.Column(
        "child_work_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(work_units.c.work_id, ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("locator_kind", sa.Text(), nullable=False),
    sa.Column("locator_value", sa.Text(), nullable=False),
    sa.Column("record_digest", sa.Text(), nullable=False),
    sa.CheckConstraint("position >= 0", name="ck_plan_admission_items_position"),
    sa.CheckConstraint(
        "record_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_plan_admission_items_record_digest_format",
    ),
    schema=MANUAL_IMPORT_SCHEMA,
)

MANUAL_IMPORT_TABLES = (plan_admissions, plan_admission_items)
