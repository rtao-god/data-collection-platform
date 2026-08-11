"""Bind source ownership to source-capable work.

Revision ID: 20260811_0003
Revises: 20260811_0002
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260811_0003"
down_revision: str | None = "20260811_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_CAPABILITY_CHECK = """
(
    capability IN ('manual_import', 'osm_query', 'http_fetch', 'browser_fetch')
    AND source_key IS NOT NULL
)
OR
(
    capability NOT IN ('manual_import', 'osm_query', 'http_fetch', 'browser_fetch')
    AND source_key IS NULL
)
"""


def upgrade() -> None:
    op.create_check_constraint(
        "ck_work_units_source_capability",
        "work_units",
        _SOURCE_CAPABILITY_CHECK,
        schema="work",
    )
    op.create_check_constraint(
        "ck_work_attempts_source_capability",
        "work_attempts",
        _SOURCE_CAPABILITY_CHECK,
        schema="work",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_work_attempts_source_capability",
        "work_attempts",
        type_="check",
        schema="work",
    )
    op.drop_constraint(
        "ck_work_units_source_capability",
        "work_units",
        type_="check",
        schema="work",
    )
