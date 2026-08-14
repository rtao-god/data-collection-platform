"""Allow immutable derived processing artifacts.

Revision ID: 20260814_0009
Revises: 20260813_0008
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260814_0009"
down_revision: str | None = "20260813_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CONTRACT = "artifact_kind IN ('raw_artifact', 'diagnostic_artifact', 'derived_artifact')"
_LEGACY_KIND_CONTRACT = "artifact_kind IN ('raw_artifact', 'diagnostic_artifact')"


def upgrade() -> None:
    _replace_kind_constraint(
        table_name="artifact_uploads",
        constraint_name="ck_artifact_uploads_kind",
        condition=_KIND_CONTRACT,
    )
    _replace_kind_constraint(
        table_name="artifact_objects",
        constraint_name="ck_artifact_objects_kind",
        condition=_KIND_CONTRACT,
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM sources.artifact_uploads
                WHERE artifact_kind = 'derived_artifact'
            ) OR EXISTS (
                SELECT 1
                FROM sources.artifact_objects
                WHERE artifact_kind = 'derived_artifact'
            ) THEN
                RAISE EXCEPTION
                    'cannot remove derived artifact contract while derived artifacts exist';
            END IF;
        END
        $$
        """
    )
    _replace_kind_constraint(
        table_name="artifact_uploads",
        constraint_name="ck_artifact_uploads_kind",
        condition=_LEGACY_KIND_CONTRACT,
    )
    _replace_kind_constraint(
        table_name="artifact_objects",
        constraint_name="ck_artifact_objects_kind",
        condition=_LEGACY_KIND_CONTRACT,
    )


def _replace_kind_constraint(
    *,
    table_name: str,
    constraint_name: str,
    condition: str,
) -> None:
    op.drop_constraint(
        constraint_name,
        table_name,
        schema="sources",
        type_="check",
    )
    op.create_check_constraint(
        constraint_name,
        table_name,
        condition,
        schema="sources",
    )
