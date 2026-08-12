"""Use PostgreSQL-compatible artifact storage-reference constraints.

Revision ID: 20260812_0007
Revises: 20260812_0006
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260812_0007"
down_revision: str | None = "20260812_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_artifact_uploads_storage_reference",
        "artifact_uploads",
        schema="sources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_artifact_uploads_storage_reference",
        "artifact_uploads",
        """
        char_length(staging_reference) BETWEEN 1 AND 512
        AND staging_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]*$'
        AND (
  final_reference IS NULL
  OR (
      char_length(final_reference) BETWEEN 1 AND 512
      AND final_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]*$'
  )
        )
        """,
        schema="sources",
    )
    op.drop_constraint(
        "ck_artifact_objects_storage_reference",
        "artifact_objects",
        schema="sources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_artifact_objects_storage_reference",
        "artifact_objects",
        """
        char_length(storage_reference) BETWEEN 1 AND 512
        AND storage_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]*$'
        """,
        schema="sources",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_artifact_objects_storage_reference",
        "artifact_objects",
        schema="sources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_artifact_objects_storage_reference",
        "artifact_objects",
        "storage_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}$'",
        schema="sources",
    )
    op.drop_constraint(
        "ck_artifact_uploads_storage_reference",
        "artifact_uploads",
        schema="sources",
        type_="check",
    )
    op.create_check_constraint(
        "ck_artifact_uploads_storage_reference",
        "artifact_uploads",
        """
        staging_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}$'
        AND (
  final_reference IS NULL
  OR final_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}$'
        )
        """,
        schema="sources",
    )
