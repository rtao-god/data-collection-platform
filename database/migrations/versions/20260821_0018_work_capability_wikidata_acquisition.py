"""Admit the Wikidata entity query and source-record mapping capabilities.

Revision ID: 20260821_0018
Revises: 20260820_0017
Create Date: 2026-08-21

`work.work_units` and `work.work_attempts` enumerate the capabilities they accept, and the
source-permit constraint decides which of those must carry a `source_key`. Both were written
before Wikidata acquisition and source-record mapping existed, so a bootstrapped run could not
persist its DISCOVERY unit at all. The capability vocabularies are restated here in full; the
columns keep their own values.

`wikidata_entity_query` reads a named external source, so it is source-bound exactly like the
other acquisition capabilities: its work units must name the source they read.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260821_0018"
down_revision: str | None = "20260820_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CAPABILITIES = (
    "manual_import",
    "manual_record",
    "osm_query",
    "wikidata_entity_query",
    "http_fetch",
    "browser_fetch",
    "extraction",
    "normalization",
    "source_record_mapping",
    "geography",
    "entity_resolution",
    "quality",
    "export",
)
_PREVIOUS_CAPABILITIES = (
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
_STAGE_CAPABILITIES = {
    "discovery": ("manual_import", "manual_record", "osm_query", "wikidata_entity_query"),
    "acquisition": ("http_fetch", "browser_fetch"),
    "extraction": ("extraction",),
    "normalization": ("normalization", "source_record_mapping"),
    "geography": ("geography",),
    "entity_resolution": ("entity_resolution",),
    "quality": ("quality",),
    "export": ("export",),
}
_PREVIOUS_STAGE_CAPABILITIES = {
    **_STAGE_CAPABILITIES,
    "discovery": ("manual_import", "manual_record", "osm_query"),
    "normalization": ("normalization",),
}
_SOURCE_BOUND_CAPABILITIES = (
    "manual_import",
    "osm_query",
    "wikidata_entity_query",
    "http_fetch",
    "browser_fetch",
)
_PREVIOUS_SOURCE_BOUND_CAPABILITIES = tuple(
    value for value in _SOURCE_BOUND_CAPABILITIES if value != "wikidata_entity_query"
)


def upgrade() -> None:
    _replace_capability_constraints(_CAPABILITIES, _STAGE_CAPABILITIES)
    _replace_source_capability_constraints(_SOURCE_BOUND_CAPABILITIES)


def downgrade() -> None:
    _replace_capability_constraints(_PREVIOUS_CAPABILITIES, _PREVIOUS_STAGE_CAPABILITIES)
    _replace_source_capability_constraints(_PREVIOUS_SOURCE_BOUND_CAPABILITIES)


def _replace_capability_constraints(
    capabilities: tuple[str, ...],
    stage_capabilities: dict[str, tuple[str, ...]],
) -> None:
    capability_values = _in_values("capability", capabilities)
    stage_capability = " OR ".join(
        f"(stage = '{stage}' AND {_in_values('capability', values)})"
        for stage, values in stage_capabilities.items()
    )

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


def _replace_source_capability_constraints(source_bound: tuple[str, ...]) -> None:
    constraint = (
        f"({_in_values('capability', source_bound)} AND source_key IS NOT NULL) OR "
        f"(NOT ({_in_values('capability', source_bound)}) AND source_key IS NULL)"
    )
    for table in ("work_units", "work_attempts"):
        op.drop_constraint(
            "ck_work_units_source_capability"
            if table == "work_units"
            else "ck_work_attempts_source_capability",
            table,
            schema="work",
            type_="check",
        )
        op.create_check_constraint(
            "ck_work_units_source_capability"
            if table == "work_units"
            else "ck_work_attempts_source_capability",
            table,
            constraint,
            schema="work",
        )


def _in_values(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"
