"""Add control-plane artifact ownership and config snapshot object identity.

Revision ID: 20260815_0011
Revises: 20260814_0010
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0011"
down_revision: str | None = "20260814_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_artifact_objects_kind",
        "artifact_objects",
        schema="sources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_artifact_objects_kind",
        "artifact_objects",
        "artifact_kind IN ('raw_artifact', 'diagnostic_artifact', 'derived_artifact', "
        "'config_bundle', 'export_artifact')",
        schema="sources",
    )
    op.alter_column("artifact_records", "upload_id", schema="sources", nullable=True)
    op.alter_column("artifact_records", "work_id", schema="sources", nullable=True)
    op.alter_column("artifact_records", "attempt_id", schema="sources", nullable=True)
    op.alter_column("artifact_records", "worker_id", schema="sources", nullable=True)
    op.add_column(
        "artifact_records",
        sa.Column("producer_kind", sa.Text(), nullable=True),
        schema="sources",
    )
    op.add_column(
        "artifact_records",
        sa.Column("producer_identity", sa.Text(), nullable=True),
        schema="sources",
    )
    op.add_column(
        "artifact_records",
        sa.Column("owner_operation_id", sa.Uuid(), nullable=True),
        schema="sources",
    )
    op.execute(
        """
        UPDATE sources.artifact_records
        SET producer_kind = 'worker', producer_identity = worker_id
        """
    )
    op.alter_column("artifact_records", "producer_kind", schema="sources", nullable=False)
    op.alter_column("artifact_records", "producer_identity", schema="sources", nullable=False)
    op.create_check_constraint(
        "ck_artifact_records_producer_kind",
        "artifact_records",
        "producer_kind IN ('worker', 'control_plane')",
        schema="sources",
    )
    op.create_check_constraint(
        "ck_artifact_records_producer_identity",
        "artifact_records",
        "producer_identity ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'",
        schema="sources",
    )
    op.create_check_constraint(
        "ck_artifact_records_producer_shape",
        "artifact_records",
        "(producer_kind = 'worker' AND upload_id IS NOT NULL AND work_id IS NOT NULL "
        "AND attempt_id IS NOT NULL AND worker_id IS NOT NULL "
        "AND producer_identity = worker_id AND owner_operation_id IS NULL) OR "
        "(producer_kind = 'control_plane' AND upload_id IS NULL AND work_id IS NULL "
        "AND attempt_id IS NULL AND worker_id IS NULL AND owner_operation_id IS NOT NULL)",
        schema="sources",
    )
    op.create_unique_constraint(
        "uq_artifact_records_owner_operation",
        "artifact_records",
        ("producer_kind", "producer_identity", "owner_operation_id"),
        schema="sources",
    )

    op.create_table(
        "config_bundle_artifacts",
        sa.Column("bundle_digest", sa.String(length=71), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ("artifact_id",),
            ("sources.artifact_records.artifact_id",),
            name="fk_config_bundle_artifacts_artifact_id_artifact_records",
        ),
        sa.ForeignKeyConstraint(
            ("bundle_digest",),
            ("config.config_bundles.bundle_digest",),
            name="fk_config_bundle_artifacts_bundle_digest_config_bundles",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("bundle_digest", name="pk_config_bundle_artifacts"),
        sa.UniqueConstraint("artifact_id", name="uq_config_bundle_artifacts_artifact_id"),
        schema="config",
        comment="Exact immutable object-store artifact for one campaign snapshot.",
    )
    op.execute(
        """
        CREATE TRIGGER trg_config_bundle_artifacts_guard_insert
        BEFORE INSERT ON config.config_bundle_artifacts
        FOR EACH ROW
        EXECUTE FUNCTION config.guard_unsealed_config_child_insert()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_config_bundle_artifacts_immutable
        BEFORE UPDATE OR DELETE ON config.config_bundle_artifacts
        FOR EACH ROW
        EXECUTE FUNCTION config.reject_immutable_config_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_config_bundle_artifacts_immutable ON config.config_bundle_artifacts"
    )
    op.execute(
        "DROP TRIGGER trg_config_bundle_artifacts_guard_insert ON config.config_bundle_artifacts"
    )
    op.drop_table("config_bundle_artifacts", schema="config")
    op.drop_constraint(
        "uq_artifact_records_owner_operation",
        "artifact_records",
        schema="sources",
        type_="unique",
    )
    op.drop_constraint(
        "ck_artifact_records_producer_shape",
        "artifact_records",
        schema="sources",
        type_="check",
    )
    op.drop_constraint(
        "ck_artifact_records_producer_identity",
        "artifact_records",
        schema="sources",
        type_="check",
    )
    op.drop_constraint(
        "ck_artifact_records_producer_kind",
        "artifact_records",
        schema="sources",
        type_="check",
    )
    op.drop_column("artifact_records", "owner_operation_id", schema="sources")
    op.drop_column("artifact_records", "producer_identity", schema="sources")
    op.drop_column("artifact_records", "producer_kind", schema="sources")
    op.drop_constraint(
        "ck_artifact_objects_kind",
        "artifact_objects",
        schema="sources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_artifact_objects_kind",
        "artifact_objects",
        "artifact_kind IN ('raw_artifact', 'diagnostic_artifact', 'derived_artifact')",
        schema="sources",
    )
    op.alter_column("artifact_records", "worker_id", schema="sources", nullable=False)
    op.alter_column("artifact_records", "attempt_id", schema="sources", nullable=False)
    op.alter_column("artifact_records", "work_id", schema="sources", nullable=False)
    op.alter_column("artifact_records", "upload_id", schema="sources", nullable=False)
