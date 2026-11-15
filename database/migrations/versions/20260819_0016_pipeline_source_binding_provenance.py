"""Persist source-binding provenance on pipeline advancement checkpoints.

Revision ID: 20260819_0016
Revises: 20260818_0015
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0016"
down_revision: str | None = "20260818_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_advancements",
        sa.Column("source_binding_key", sa.Text(), nullable=True),
        schema="work",
    )
    op.add_column(
        "pipeline_advancements",
        sa.Column("source_config_bundle_digest", sa.Text(), nullable=True),
        schema="work",
    )
    op.execute(
        """
        UPDATE work.pipeline_advancements AS advancement
        SET source_binding_key = source.source_binding_key,
            source_config_bundle_digest = run.config_bundle_digest
        FROM work.work_units AS source
        JOIN runs.collection_runs AS run ON run.run_id = source.run_id
        WHERE source.work_id = advancement.source_work_unit_id
        """
    )
    op.create_check_constraint(
        "ck_pipeline_advancements_source_binding_key_format",
        "pipeline_advancements",
        "source_binding_key IS NULL OR source_binding_key ~ '^[a-z][a-z0-9_]{0,79}$'",
        schema="work",
    )
    op.create_check_constraint(
        "ck_pipeline_advancements_config_digest_format",
        "pipeline_advancements",
        "source_config_bundle_digest IS NULL OR "
        "source_config_bundle_digest ~ '^sha256:[0-9a-f]{64}$'",
        schema="work",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_pipeline_advancements_config_digest_format",
        "pipeline_advancements",
        schema="work",
        type_="check",
    )
    op.drop_constraint(
        "ck_pipeline_advancements_source_binding_key_format",
        "pipeline_advancements",
        schema="work",
        type_="check",
    )
    op.drop_column("pipeline_advancements", "source_config_bundle_digest", schema="work")
    op.drop_column("pipeline_advancements", "source_binding_key", schema="work")
