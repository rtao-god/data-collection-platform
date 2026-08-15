from __future__ import annotations

from uuid import UUID

import pytest
import sqlalchemy as sa
from collection_application.pipeline_advancement import PipelineAdvancementConflict
from collection_infrastructure.postgres.succeeded_work_catalog import (
    _binding_direction,
    _binding_table,
    _choose_column,
    _resolve_artifact_table,
    _resolve_binding_tables,
    _with_fixed_direction,
)

_WORK_ID = UUID("00000000-0000-0000-0000-000000000601")


def _artifact_table(metadata: sa.MetaData, name: str = "artifacts") -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column("artifact_id", sa.Uuid(), primary_key=True),
        sa.Column("content_digest", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        schema="artifacts",
    )


def _binding(
    metadata: sa.MetaData,
    name: str,
    *,
    direction: bool = False,
) -> sa.Table:
    columns = [
        sa.Column("work_unit_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
    ]
    if direction:
        columns.append(sa.Column("direction", sa.Text(), nullable=False))
    return sa.Table(name, metadata, *columns, schema="work")


def test_artifact_schema_prefers_the_canonical_named_owner() -> None:
    metadata = sa.MetaData()
    preferred = _artifact_table(metadata)
    fallback = _artifact_table(metadata, "artifact_snapshot")

    table, columns = _resolve_artifact_table((fallback, preferred))

    assert table is preferred
    assert columns.digest.name == "content_digest"
    assert columns.size_bytes.name == "size_bytes"


def test_artifact_schema_rejects_ambiguous_unnamed_owners() -> None:
    metadata = sa.MetaData()
    first = _artifact_table(metadata, "artifact_snapshot_a")
    second = _artifact_table(metadata, "artifact_snapshot_b")

    with pytest.raises(PipelineAdvancementConflict) as captured:
        _resolve_artifact_table((first, second))

    assert captured.value.code == "PIPELINE_ARTIFACT_SCHEMA_AMBIGUOUS"


def test_binding_schema_accepts_separate_input_and_output_tables() -> None:
    metadata = sa.MetaData()
    artifacts = _artifact_table(metadata)
    inputs = _binding(metadata, "work_input_artifacts")
    outputs = _binding(metadata, "work_output_artifacts")

    input_owner, output_owner = _resolve_binding_tables(
        (artifacts, inputs, outputs),
        artifacts,
    )

    assert input_owner.table is inputs
    assert input_owner.fixed_direction == "input"
    assert output_owner.table is outputs
    assert output_owner.fixed_direction == "output"


def test_binding_schema_accepts_one_explicit_direction_table() -> None:
    metadata = sa.MetaData()
    artifacts = _artifact_table(metadata)
    bindings = _binding(metadata, "work_artifact_bindings", direction=True)

    input_owner, output_owner = _resolve_binding_tables((artifacts, bindings), artifacts)

    assert input_owner is output_owner
    assert input_owner.direction is not None


def test_unknown_binding_direction_is_fail_closed() -> None:
    metadata = sa.MetaData()
    binding = _binding_table(_binding(metadata, "work_artifact_bindings", direction=True))
    assert binding is not None

    with pytest.raises(PipelineAdvancementConflict) as captured:
        _binding_direction(binding, {"binding_direction": "side_channel"}, _WORK_ID)  # type: ignore[arg-type]

    assert captured.value.code == "PIPELINE_ARTIFACT_DIRECTION_UNSUPPORTED"


def test_column_resolution_rejects_schema_alias_collision() -> None:
    metadata = sa.MetaData()
    table = sa.Table(
        "work_units",
        metadata,
        sa.Column("capability", sa.Text()),
        sa.Column("required_capability", sa.Text()),
        schema="work",
    )

    with pytest.raises(PipelineAdvancementConflict) as captured:
        _choose_column(
            table,
            ("capability", "required_capability"),
            meaning="work capability",
        )

    assert captured.value.code == "PIPELINE_SCHEMA_COLUMN_AMBIGUOUS"


def test_fixed_direction_copy_removes_dynamic_direction_contract() -> None:
    metadata = sa.MetaData()
    binding = _binding_table(_binding(metadata, "work_input_artifacts", direction=True))
    assert binding is not None

    fixed = _with_fixed_direction(binding, "input")

    assert fixed.fixed_direction == "input"
    assert fixed.direction is None
    assert fixed.is_input is None
