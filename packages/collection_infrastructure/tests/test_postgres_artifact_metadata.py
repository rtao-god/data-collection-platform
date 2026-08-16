from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from collection_infrastructure.postgres import (
    ARTIFACT_TABLES,
    artifact_objects,
    artifact_records,
    artifact_uploads,
    work_input_artifacts,
    work_output_artifacts,
)


def test_artifact_metadata_has_exact_owner_tables_and_no_cascade_delete() -> None:
    assert tuple(table.fullname for table in ARTIFACT_TABLES) == (
        "sources.artifact_uploads",
        "sources.artifact_cleanup_tombstones",
        "sources.artifact_objects",
        "sources.artifact_records",
        "work.work_input_artifacts",
        "work.work_output_artifacts",
    )
    for table in ARTIFACT_TABLES:
        assert all(foreign_key.ondelete in {None, "RESTRICT"} for foreign_key in table.foreign_keys)
        assert all(column.server_default is None for column in table.columns)


def test_artifact_metadata_compiles_content_and_binding_invariants() -> None:
    dialect = postgresql.dialect()
    upload_sql = str(CreateTable(artifact_uploads).compile(dialect=dialect))
    object_sql = str(CreateTable(artifact_objects).compile(dialect=dialect))
    record_sql = str(CreateTable(artifact_records).compile(dialect=dialect))
    input_sql = str(CreateTable(work_input_artifacts).compile(dialect=dialect))
    output_sql = str(CreateTable(work_output_artifacts).compile(dialect=dialect))
    orphan_index = next(
        index
        for index in artifact_uploads.indexes
        if index.name == "ix_artifact_uploads_orphan_candidates"
    )
    orphan_index_sql = str(CreateIndex(orphan_index).compile(dialect=dialect))

    assert (
        "artifact_kind IN ('raw_artifact', 'diagnostic_artifact', 'derived_artifact', "
        "'config_bundle', 'export_artifact')" in object_sql
    )
    assert (
        "artifact_kind IN ('raw_artifact', 'diagnostic_artifact', 'derived_artifact')" in upload_sql
    )
    assert "UNIQUE (artifact_kind, content_digest)" in object_sql
    assert "uq_artifact_records_upload_id" in record_sql
    assert "uq_artifact_records_owner_operation" in record_sql
    assert "producer_kind IN ('worker', 'control_plane')" in record_sql
    assert "UNIQUE (work_id, artifact_id)" in input_sql
    assert "UNIQUE (work_id, role)" in input_sql
    assert "UNIQUE (work_id, role)" in output_sql
    assert "state IN ('prepared', 'verified', 'consumed')" in upload_sql
    assert "WHERE state IN ('prepared', 'verified', 'consumed')" in orphan_index_sql
