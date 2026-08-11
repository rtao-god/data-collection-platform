"""Persist worker output compatibility and classified failure budget.

Revision ID: 20260811_0004
Revises: 20260811_0003
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260811_0004"
down_revision: str | None = "20260811_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE work.worker_output_contracts (
            worker_id TEXT NOT NULL,
            output_contract TEXT NOT NULL,
            PRIMARY KEY (worker_id, output_contract),
            CONSTRAINT ck_worker_output_contracts_identity
                CHECK (output_contract ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'),
            FOREIGN KEY(worker_id) REFERENCES work.worker_registrations (worker_id)
        )
        """
    )
    op.execute("ALTER TABLE work.work_units ADD COLUMN failure_count INTEGER")
    op.execute(
        """
        UPDATE work.work_units AS unit
        SET failure_count = counted.failure_count
        FROM (
            SELECT
                attempt.work_id,
                count(*)::integer AS failure_count
            FROM work.work_attempts AS attempt
            WHERE attempt.outcome IN (
                'retry_scheduled',
                'dead_lettered',
                'blocked_by_policy',
                'expired'
            )
            GROUP BY attempt.work_id
        ) AS counted
        WHERE counted.work_id = unit.work_id
        """
    )
    op.execute("UPDATE work.work_units SET failure_count = 0 WHERE failure_count IS NULL")
    op.execute("ALTER TABLE work.work_units ALTER COLUMN failure_count SET NOT NULL")
    op.execute("ALTER TABLE work.work_units DROP CONSTRAINT ck_work_units_attempt_budget")
    op.execute(
        """
        ALTER TABLE work.work_units
        ADD CONSTRAINT ck_work_units_attempt_budget CHECK (
            attempt_count >= 0
            AND failure_count BETWEEN 0 AND max_attempts
            AND failure_count <= attempt_count
            AND max_attempts BETWEEN 1 AND 100
        )
        """
    )
    op.execute(
        """
        ALTER TABLE work.work_units
        ADD CONSTRAINT ck_work_units_expected_output_contract_format CHECK (
            expected_output_contract ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE work.work_units DROP CONSTRAINT "
        "ck_work_units_expected_output_contract_format"
    )
    op.execute("ALTER TABLE work.work_units DROP CONSTRAINT ck_work_units_attempt_budget")
    op.execute(
        """
        ALTER TABLE work.work_units
        ADD CONSTRAINT ck_work_units_attempt_budget CHECK (
            attempt_count BETWEEN 0 AND max_attempts
            AND max_attempts BETWEEN 1 AND 100
        )
        """
    )
    op.execute("ALTER TABLE work.work_units DROP COLUMN failure_count")
    op.execute("DROP TABLE work.worker_output_contracts")
