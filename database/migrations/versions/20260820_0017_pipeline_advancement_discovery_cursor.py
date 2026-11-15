"""Add a rotating scan cursor for succeeded-work discovery.

Revision ID: 20260820_0017
Revises: 20260819_0016
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0017"
down_revision: str | None = "20260819_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_advancement_discovery_cursor",
        sa.Column("cursor_id", sa.SmallInteger(), primary_key=True),
        sa.Column("last_scanned_updated_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scanned_work_id", sa.Uuid(), nullable=True),
        sa.Column("cycle_max_updated_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cycle_max_work_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "cursor_id = 1",
            name="ck_pipeline_advancement_discovery_cursor_singleton",
        ),
        sa.CheckConstraint(
            "(last_scanned_updated_at_utc IS NULL) = (last_scanned_work_id IS NULL)",
            name="ck_pipeline_advancement_discovery_cursor_pair",
        ),
        sa.CheckConstraint(
            "(cycle_max_updated_at_utc IS NULL) = (cycle_max_work_id IS NULL)",
            name="ck_pipeline_advancement_discovery_cursor_cycle_pair",
        ),
        sa.CheckConstraint(
            "last_scanned_updated_at_utc IS NULL OR cycle_max_updated_at_utc IS NOT NULL",
            name="ck_pipeline_advancement_discovery_cursor_active_cycle",
        ),
        schema="work",
    )
    op.bulk_insert(
        sa.table(
            "pipeline_advancement_discovery_cursor",
            sa.column("cursor_id", sa.SmallInteger()),
            sa.column("last_scanned_updated_at_utc", sa.DateTime(timezone=True)),
            sa.column("last_scanned_work_id", sa.Uuid()),
            sa.column("cycle_max_updated_at_utc", sa.DateTime(timezone=True)),
            sa.column("cycle_max_work_id", sa.Uuid()),
            schema="work",
        ),
        [
            {
                "cursor_id": 1,
                "last_scanned_updated_at_utc": None,
                "last_scanned_work_id": None,
                "cycle_max_updated_at_utc": None,
                "cycle_max_work_id": None,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("pipeline_advancement_discovery_cursor", schema="work")
