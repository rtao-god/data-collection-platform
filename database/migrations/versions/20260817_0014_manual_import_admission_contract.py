"""Replace legacy manual-import admission ownership with canonical record routing.

Revision ID: 20260817_0014
Revises: 20260815_0013
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0014"
down_revision: str | None = "20260815_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CAPABILITIES = (
    "manual_import",
    "manual_record",
    "osm_query",
    "http_fetch",
    "browser_fetch",
    "extraction",
    "normalization",
    "geography",
    "entity_resolution",
    "quality",
    "export",
)
_LEGACY_CAPABILITIES = tuple(value for value in _CAPABILITIES if value != "manual_record")


def upgrade() -> None:
    _reject_legacy_admissions()
    _replace_capability_constraints(_CAPABILITIES)

    op.drop_constraint(
        "ck_plan_admissions_ready",
        "plan_admissions",
        schema="manual_import",
        type_="check",
    )
    op.drop_constraint(
        "ck_plan_admissions_counts",
        "plan_admissions",
        schema="manual_import",
        type_="check",
    )
    op.drop_constraint(
        "ck_plan_admissions_child_count",
        "plan_admissions",
        schema="manual_import",
        type_="check",
    )
    op.drop_constraint(
        "ck_plan_admissions_digest_format",
        "plan_admissions",
        schema="manual_import",
        type_="check",
    )

    op.add_column(
        "plan_admissions",
        sa.Column("source_artifact_role", sa.Text(), nullable=False),
        schema="manual_import",
    )
    op.alter_column(
        "plan_admissions",
        "plan_status",
        new_column_name="plan_disposition",
        schema="manual_import",
    )
    op.alter_column(
        "plan_admissions",
        "accepted_record_count",
        new_column_name="valid_record_count",
        schema="manual_import",
    )
    op.alter_column(
        "plan_admissions",
        "rejected_record_count",
        new_column_name="issue_count",
        schema="manual_import",
    )
    op.drop_column(
        "plan_admissions",
        "total_record_count",
        schema="manual_import",
    )

    op.create_check_constraint(
        "ck_plan_admissions_source_role",
        "plan_admissions",
        "source_artifact_role ~ "
        "'^(manual_source|manual_import_source):(csv|json|jsonl):(atomic|partial)$'",
        schema="manual_import",
    )
    op.create_check_constraint(
        "ck_plan_admissions_mode",
        "plan_admissions",
        "mode IN ('atomic', 'partial')",
        schema="manual_import",
    )
    op.create_check_constraint(
        "ck_plan_admissions_disposition",
        "plan_admissions",
        "plan_disposition IN ('accepted', 'partial')",
        schema="manual_import",
    )
    op.create_check_constraint(
        "ck_plan_admissions_disposition_shape",
        "plan_admissions",
        "(plan_disposition = 'accepted' AND valid_record_count > 0 "
        "AND issue_count = 0) OR (plan_disposition = 'partial' "
        "AND valid_record_count > 0 AND issue_count > 0)",
        schema="manual_import",
    )
    op.create_check_constraint(
        "ck_plan_admissions_child_count",
        "plan_admissions",
        "valid_record_count = child_work_count",
        schema="manual_import",
    )
    op.create_check_constraint(
        "ck_plan_admissions_target_owner",
        "plan_admissions",
        "target_stage = 'discovery' AND target_capability = 'manual_record' "
        "AND target_output_contract = 'manual-import-record@1'",
        schema="manual_import",
    )
    op.create_check_constraint(
        "ck_plan_admissions_digest_format",
        "plan_admissions",
        "plan_digest ~ '^sha256:[0-9a-f]{64}$' AND "
        "source_digest ~ '^sha256:[0-9a-f]{64}$' AND "
        "result_digest ~ '^sha256:[0-9a-f]{64}$'",
        schema="manual_import",
    )


def downgrade() -> None:
    _reject_current_owner_rows()
    _replace_capability_constraints(_LEGACY_CAPABILITIES)

    for name in (
        "ck_plan_admissions_source_role",
        "ck_plan_admissions_mode",
        "ck_plan_admissions_disposition",
        "ck_plan_admissions_disposition_shape",
        "ck_plan_admissions_child_count",
        "ck_plan_admissions_target_owner",
        "ck_plan_admissions_digest_format",
    ):
        op.drop_constraint(
            name,
            "plan_admissions",
            schema="manual_import",
            type_="check",
        )

    op.add_column(
        "plan_admissions",
        sa.Column("total_record_count", sa.Integer(), nullable=False),
        schema="manual_import",
    )
    op.alter_column(
        "plan_admissions",
        "issue_count",
        new_column_name="rejected_record_count",
        schema="manual_import",
    )
    op.alter_column(
        "plan_admissions",
        "valid_record_count",
        new_column_name="accepted_record_count",
        schema="manual_import",
    )
    op.alter_column(
        "plan_admissions",
        "plan_disposition",
        new_column_name="plan_status",
        schema="manual_import",
    )
    op.drop_column(
        "plan_admissions",
        "source_artifact_role",
        schema="manual_import",
    )

    op.create_check_constraint(
        "ck_plan_admissions_ready",
        "plan_admissions",
        "plan_status = 'ready'",
        schema="manual_import",
    )
    op.create_check_constraint(
        "ck_plan_admissions_counts",
        "plan_admissions",
        "accepted_record_count + rejected_record_count = total_record_count",
        schema="manual_import",
    )
    op.create_check_constraint(
        "ck_plan_admissions_child_count",
        "plan_admissions",
        "accepted_record_count = child_work_count",
        schema="manual_import",
    )
    op.create_check_constraint(
        "ck_plan_admissions_digest_format",
        "plan_admissions",
        "plan_digest ~ '^sha256:[0-9a-f]{64}$' AND "
        "source_digest ~ '^sha256:[0-9a-f]{64}$' AND "
        "result_digest ~ '^sha256:[0-9a-f]{64}$'",
        schema="manual_import",
    )


def _replace_capability_constraints(capabilities: tuple[str, ...]) -> None:
    capability_values = _in_values("capability", capabilities)
    stage_capability = _stage_capability_check(capabilities)

    op.drop_constraint(
        "ck_worker_capabilities_capability",
        "worker_capabilities",
        schema="work",
        type_="check",
    )
    op.create_check_constraint(
        "ck_worker_capabilities_capability",
        "worker_capabilities",
        capability_values,
        schema="work",
    )

    op.drop_constraint(
        "ck_work_units_capability",
        "work_units",
        schema="work",
        type_="check",
    )
    op.drop_constraint(
        "ck_work_units_stage_capability",
        "work_units",
        schema="work",
        type_="check",
    )
    op.create_check_constraint(
        "ck_work_units_capability",
        "work_units",
        capability_values,
        schema="work",
    )
    op.create_check_constraint(
        "ck_work_units_stage_capability",
        "work_units",
        stage_capability,
        schema="work",
    )

    op.drop_constraint(
        "ck_work_attempts_capability",
        "work_attempts",
        schema="work",
        type_="check",
    )
    op.create_check_constraint(
        "ck_work_attempts_capability",
        "work_attempts",
        capability_values,
        schema="work",
    )


def _stage_capability_check(capabilities: tuple[str, ...]) -> str:
    manual = ("manual_import", "osm_query")
    if "manual_record" in capabilities:
        manual = ("manual_import", "manual_record", "osm_query")
    stage_capabilities = {
        "discovery": manual,
        "acquisition": ("http_fetch", "browser_fetch"),
        "extraction": ("extraction",),
        "normalization": ("normalization",),
        "geography": ("geography",),
        "entity_resolution": ("entity_resolution",),
        "quality": ("quality",),
        "export": ("export",),
    }
    return " OR ".join(
        f"(stage = '{stage}' AND {_in_values('capability', values)})"
        for stage, values in stage_capabilities.items()
    )


def _in_values(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def _reject_legacy_admissions() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM manual_import.plan_admissions) THEN
                RAISE EXCEPTION
                    'legacy manual-import admissions cannot be migrated safely'
                    USING ERRCODE = '55000',
                          HINT = 'Rematerialize legacy admission work through the '
                                 'canonical manual-record owner before retrying.';
            END IF;
        END;
        $$;
        """
    )


def _reject_current_owner_rows() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM manual_import.plan_admissions)
               OR EXISTS (
                    SELECT 1 FROM work.work_units
                    WHERE capability = 'manual_record'
               )
               OR EXISTS (
                    SELECT 1 FROM work.worker_capabilities
                    WHERE capability = 'manual_record'
               )
               OR EXISTS (
                    SELECT 1 FROM work.work_attempts
                    WHERE capability = 'manual_record'
               ) THEN
                RAISE EXCEPTION
                    'canonical manual-record state cannot be downgraded safely'
                    USING ERRCODE = '55000',
                          HINT = 'Preserve the schema or remove current-owner state '
                                 'through an approved migration.';
            END IF;
        END;
        $$;
        """
    )
