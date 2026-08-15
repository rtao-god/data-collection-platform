"""Add append-only collection run transition history.

Revision ID: 20260815_0012
Revises: 20260815_0011
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0012"
down_revision: str | None = "20260815_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_run_transitions",
        sa.Column("transition_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=False),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("from_revision", sa.BigInteger(), nullable=False),
        sa.Column("to_revision", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("changed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "from_state IN ('created', 'running', 'paused', 'cancelled', 'completed', 'blocked')",
            name="ck_run_transitions_from",
        ),
        sa.CheckConstraint(
            "to_state IN ('created', 'running', 'paused', 'cancelled', 'completed', 'blocked')",
            name="ck_run_transitions_to",
        ),
        sa.CheckConstraint(
            "from_revision >= 0 AND to_revision = from_revision + 1",
            name="ck_run_transitions_revision_order",
        ),
        sa.CheckConstraint(
            "char_length(actor_id) BETWEEN 1 AND 200",
            name="ck_run_transitions_actor",
        ),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 1000",
            name="ck_run_transitions_reason",
        ),
        sa.ForeignKeyConstraint(
            ("run_id",),
            ("runs.collection_runs.run_id",),
            name="fk_collection_run_transitions_run_id_collection_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("transition_id", name="pk_collection_run_transitions"),
        sa.UniqueConstraint(
            "run_id",
            "to_revision",
            name="uq_run_transitions_revision",
        ),
        schema="runs",
    )
    op.execute(
        """
        CREATE FUNCTION runs.reject_collection_run_transition_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'collection run transition history is immutable'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_collection_run_transitions_immutable
        BEFORE UPDATE OR DELETE ON runs.collection_run_transitions
        FOR EACH ROW EXECUTE FUNCTION runs.reject_collection_run_transition_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_collection_run_transitions_immutable ON runs.collection_run_transitions"
    )
    op.drop_table("collection_run_transitions", schema="runs")
    op.execute("DROP FUNCTION runs.reject_collection_run_transition_mutation()")
