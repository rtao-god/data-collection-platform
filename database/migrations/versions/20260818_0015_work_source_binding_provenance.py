"""Persist source-binding provenance independently of source permits.

Revision ID: 20260818_0015
Revises: 20260817_0014
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0015"
down_revision: str | None = "20260817_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "work_units",
        sa.Column("source_binding_key", sa.Text(), nullable=True),
        schema="work",
    )
    op.create_check_constraint(
        "ck_work_units_source_binding_key_format",
        "work_units",
        "source_binding_key IS NULL OR source_binding_key ~ '^[a-z][a-z0-9_]{0,79}$'",
        schema="work",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_work_units_source_binding_key_format",
        "work_units",
        schema="work",
        type_="check",
    )
    op.drop_column("work_units", "source_binding_key", schema="work")
