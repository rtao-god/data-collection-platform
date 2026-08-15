from __future__ import annotations

from uuid import UUID

from collection_infrastructure.postgres.artifact_metadata import (
    work_input_artifacts,
    work_output_artifacts,
)
from collection_infrastructure.postgres.pipeline_advancement_metadata import (
    pipeline_advancements,
)
from collection_infrastructure.postgres.succeeded_work_catalog import (
    _artifact_statement,
    _unregistered_succeeded_statement,
    _work_statement,
)
from sqlalchemy.dialects import postgresql

_WORK_ID = UUID("00000000-0000-0000-0000-000000000601")


def _sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


def test_work_lookup_targets_canonical_work_identity() -> None:
    sql = _sql(_work_statement(_WORK_ID))

    assert "work.work_units.work_id" in sql
    assert "work.work_units.work_unit_id" not in sql
    assert "FOR SHARE" in sql


def test_input_artifact_lookup_joins_canonical_record_and_object_owners() -> None:
    sql = _sql(_artifact_statement(work_input_artifacts, _WORK_ID))

    assert "work.work_input_artifacts.work_id" in sql
    assert "sources.artifact_records" in sql
    assert "sources.artifact_objects" in sql
    assert "artifact_records.object_id = sources.artifact_objects.object_id" in sql


def test_output_artifact_lookup_uses_canonical_binding_owner() -> None:
    sql = _sql(_artifact_statement(work_output_artifacts, _WORK_ID))

    assert "work.work_output_artifacts.work_id" in sql
    assert "work.work_output_artifacts.position" in sql
    assert "work.work_output_artifacts.role" in sql


def test_unregistered_discovery_uses_exact_source_work_identity() -> None:
    sql = _sql(_unregistered_succeeded_statement(25))

    assert "work.work_units.work_id" in sql
    assert "work.pipeline_advancements.source_work_unit_id" in sql
    assert "work.work_units.work_unit_id" not in sql
    assert "LIMIT" in sql


def test_advancement_metadata_foreign_key_targets_canonical_work_id() -> None:
    foreign_key = next(iter(pipeline_advancements.c.source_work_unit_id.foreign_keys))

    assert foreign_key.target_fullname == "work.work_units.work_id"
    assert foreign_key.ondelete == "RESTRICT"
