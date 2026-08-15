from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from uuid import UUID

import sqlalchemy as sa
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineAdvancementConflict,
    SucceededWorkOutput,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from collection_application import WorkStage
from collection_infrastructure.postgres.pipeline_advancement_metadata import (
    pipeline_advancements,
)

_INPUT_DIRECTIONS = frozenset({"input", "lease_input", "source"})
_OUTPUT_DIRECTIONS = frozenset({"output", "completion_output", "result"})


@dataclass(frozen=True, slots=True)
class _ArtifactColumns:
    artifact_id: sa.Column[object]
    digest: sa.Column[object]
    size_bytes: sa.Column[object]
    content_type: sa.Column[object]


@dataclass(frozen=True, slots=True)
class _BindingTable:
    table: sa.Table
    work_unit_id: sa.Column[object]
    artifact_id: sa.Column[object]
    role: sa.Column[object]
    ordinal: sa.Column[object] | None
    direction: sa.Column[object] | None
    is_input: sa.Column[object] | None
    fixed_direction: str | None


@dataclass(frozen=True, slots=True)
class _ResolvedSchema:
    work_units: sa.Table
    stage_runs: sa.Table
    artifact_table: sa.Table
    artifact_columns: _ArtifactColumns
    input_binding: _BindingTable
    output_binding: _BindingTable
    work_capability: sa.Column[object]
    work_output_contract: sa.Column[object]
    work_output_digest: sa.Column[object] | None
    work_updated_at: sa.Column[object] | None


@dataclass(frozen=True, slots=True)
class _BoundArtifact:
    direction: str
    ordinal: int
    artifact: ArtifactIdentity


class PostgresSucceededWorkCatalog:
    """Canonical succeeded-work reader and unregistered-output discovery port."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._schema: _ResolvedSchema | None = None
        self._schema_lock = Lock()

    def __call__(
        self,
        connection: Connection,
        source_work_unit_id: UUID,
    ) -> SucceededWorkOutput:
        return self.read(connection, source_work_unit_id)

    def read(
        self,
        connection: Connection,
        source_work_unit_id: UUID,
    ) -> SucceededWorkOutput:
        schema = self._resolved_schema(connection)
        work = (
            connection.execute(
                sa.select(schema.work_units)
                .where(schema.work_units.c.work_unit_id == source_work_unit_id)
                .with_for_update(read=True)
            )
            .mappings()
            .one_or_none()
        )
        if work is None:
            raise _catalog_conflict(
                "PIPELINE_SOURCE_WORK_NOT_FOUND",
                "The source work unit does not exist.",
                source_work_unit_id,
                "Refresh canonical work state and select an existing successful work unit.",
            )
        if _required_text(work, "state") != "succeeded":
            raise _catalog_conflict(
                "PIPELINE_SOURCE_WORK_NOT_SUCCEEDED",
                "Pipeline advancement accepts only successfully completed work.",
                source_work_unit_id,
                "Wait for canonical successful completion before registering advancement.",
            )
        run_id = _required_uuid(work, "run_id")
        stage_run_id = _required_uuid(work, "stage_run_id")
        stage_row = (
            connection.execute(
                sa.select(schema.stage_runs).where(schema.stage_runs.c.stage_run_id == stage_run_id)
            )
            .mappings()
            .one_or_none()
        )
        if stage_row is None:
            raise _catalog_conflict(
                "PIPELINE_STAGE_OWNER_NOT_FOUND",
                "The successful work has no canonical stage owner.",
                source_work_unit_id,
                "Repair stage ownership before advancing the successful output.",
            )
        if "run_id" in stage_row and _required_uuid(stage_row, "run_id") != run_id:
            raise _catalog_conflict(
                "PIPELINE_STAGE_RUN_CONFLICT",
                "The work and stage owners belong to different collection runs.",
                source_work_unit_id,
                "Repair the corrupted run/stage ownership before advancement.",
            )
        bindings = self._load_bindings(connection, schema, source_work_unit_id)
        outputs = tuple(item.artifact for item in bindings if item.direction == "output")
        inputs = tuple(item.artifact for item in bindings if item.direction == "input")
        if len(outputs) != 1:
            raise PipelineAdvancementConflict(
                code="PIPELINE_OUTPUT_ARTIFACT_CARDINALITY",
                message="Successful work must bind exactly one canonical output artifact.",
                context={
                    "sourceWorkUnitId": str(source_work_unit_id),
                    "outputArtifactCount": len(outputs),
                },
                required_action=(
                    "Repair work completion so the exact output contract "
                    "has one canonical artifact."
                ),
            )
        if outputs[0].artifact_id in {item.artifact_id for item in inputs}:
            raise _catalog_conflict(
                "PIPELINE_ARTIFACT_DIRECTION_CONFLICT",
                "One artifact is bound as both pipeline input and output.",
                source_work_unit_id,
                "Repair immutable artifact bindings before advancement.",
            )
        output_digest = (
            outputs[0].content_digest
            if schema.work_output_digest is None
            else _required_column_text(work, schema.work_output_digest)
        )
        return SucceededWorkOutput(
            source_work_unit_id=source_work_unit_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage=WorkStage(_required_text(stage_row, "stage")),
            capability=_required_column_text(work, schema.work_capability),
            output_contract=_required_column_text(work, schema.work_output_contract),
            output_digest=output_digest,
            output_artifact=outputs[0],
            input_artifacts=inputs,
        )

    def list_unregistered_succeeded(
        self,
        *,
        limit: int,
        correlation_id: str,
    ) -> tuple[SucceededWorkOutput, ...]:
        del correlation_id
        if not 1 <= limit <= 1_000:
            raise ValueError("successful work discovery limit must be between 1 and 1000")
        try:
            with self._engine.connect() as connection:
                schema = self._resolved_schema(connection)
                statement = sa.select(schema.work_units.c.work_unit_id).where(
                    schema.work_units.c.state == "succeeded",
                    ~sa.exists(
                        sa.select(sa.literal(1)).where(
                            pipeline_advancements.c.source_work_unit_id
                            == schema.work_units.c.work_unit_id
                        )
                    ),
                )
                if schema.work_updated_at is not None:
                    statement = statement.order_by(
                        schema.work_updated_at,
                        schema.work_units.c.work_unit_id,
                    )
                else:
                    statement = statement.order_by(schema.work_units.c.work_unit_id)
                work_ids = tuple(
                    _uuid_value(value)
                    for value in connection.execute(statement.limit(limit)).scalars()
                )
                return tuple(self.read(connection, work_id) for work_id in work_ids)
        except PipelineAdvancementConflict:
            raise
        except SQLAlchemyError as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_DISCOVERY_STORAGE_FAILED",
                message="Successful work discovery did not complete.",
                context={"causeType": type(exc).__name__},
                required_action="Inspect PostgreSQL owner state and retry exact discovery.",
            ) from exc

    def _resolved_schema(self, connection: Connection) -> _ResolvedSchema:
        if self._schema is not None:
            return self._schema
        with self._schema_lock:
            if self._schema is None:
                self._schema = _resolve_schema(connection)
            return self._schema

    @staticmethod
    def _load_bindings(
        connection: Connection,
        schema: _ResolvedSchema,
        source_work_unit_id: UUID,
    ) -> tuple[_BoundArtifact, ...]:
        if schema.input_binding.table is schema.output_binding.table:
            rows = _binding_rows(
                connection,
                schema.input_binding,
                schema.artifact_table,
                schema.artifact_columns,
                source_work_unit_id,
            )
        else:
            rows = (
                *_binding_rows(
                    connection,
                    schema.input_binding,
                    schema.artifact_table,
                    schema.artifact_columns,
                    source_work_unit_id,
                ),
                *_binding_rows(
                    connection,
                    schema.output_binding,
                    schema.artifact_table,
                    schema.artifact_columns,
                    source_work_unit_id,
                ),
            )
        ordered = tuple(
            sorted(
                rows,
                key=lambda item: (item.direction, item.ordinal, item.artifact.role),
            )
        )
        identities = tuple(item.artifact.artifact_id for item in ordered)
        if len(identities) != len(set(identities)):
            raise _catalog_conflict(
                "PIPELINE_ARTIFACT_BINDING_DUPLICATE",
                "Successful work contains duplicate artifact bindings.",
                source_work_unit_id,
                "Repair duplicate immutable bindings before advancement.",
            )
        return ordered


def _resolve_schema(connection: Connection) -> _ResolvedSchema:
    metadata = sa.MetaData()
    work_units = sa.Table(
        "work_units",
        metadata,
        schema="work",
        autoload_with=connection,
    )
    stage_runs = sa.Table(
        "stage_runs",
        metadata,
        schema="runs",
        autoload_with=connection,
    )
    _require_columns(work_units, ("work_unit_id", "run_id", "stage_run_id", "state"))
    _require_columns(stage_runs, ("stage_run_id", "stage"))
    capability = _choose_column(
        work_units,
        ("capability", "required_capability"),
        meaning="work capability",
    )
    output_contract = _choose_column(
        work_units,
        ("output_contract", "required_output_contract"),
        meaning="work output contract",
    )
    output_digest = _optional_column(
        work_units,
        ("output_digest", "completion_digest", "result_digest"),
        meaning="work output digest",
    )
    updated_at = _optional_column(
        work_units,
        ("updated_at_utc", "completed_at_utc"),
        meaning="work update ordering",
    )
    tables = _reflect_artifact_tables(connection, metadata)
    artifact_table, artifact_columns = _resolve_artifact_table(tables)
    input_binding, output_binding = _resolve_binding_tables(tables, artifact_table)
    return _ResolvedSchema(
        work_units=work_units,
        stage_runs=stage_runs,
        artifact_table=artifact_table,
        artifact_columns=artifact_columns,
        input_binding=input_binding,
        output_binding=output_binding,
        work_capability=capability,
        work_output_contract=output_contract,
        work_output_digest=output_digest,
        work_updated_at=updated_at,
    )


def _reflect_artifact_tables(
    connection: Connection,
    metadata: sa.MetaData,
) -> tuple[sa.Table, ...]:
    inspector = sa.inspect(connection)
    tables: list[sa.Table] = []
    for schema_name in ("artifacts", "work"):
        for table_name in inspector.get_table_names(schema=schema_name):
            if table_name in {
                "work_units",
                "pipeline_advancements",
                "pipeline_advancement_attempts",
            }:
                continue
            tables.append(
                sa.Table(
                    table_name,
                    metadata,
                    schema=schema_name,
                    autoload_with=connection,
                )
            )
    return tuple(tables)


def _resolve_artifact_table(
    tables: tuple[sa.Table, ...],
) -> tuple[sa.Table, _ArtifactColumns]:
    candidates: list[tuple[int, sa.Table, _ArtifactColumns]] = []
    preferred_names = {
        "artifacts": 4,
        "raw_artifacts": 3,
        "artifact_objects": 2,
        "artifact_metadata": 1,
    }
    for table in tables:
        artifact_id = table.c.get("artifact_id")
        digest = _optional_column(
            table,
            ("content_digest", "sha256_digest", "digest"),
            meaning="artifact digest",
            fail_on_multiple=False,
        )
        size_bytes = _optional_column(
            table,
            ("size_bytes", "byte_count", "content_length"),
            meaning="artifact size",
            fail_on_multiple=False,
        )
        content_type = _optional_column(
            table,
            ("content_type", "media_type"),
            meaning="artifact content type",
            fail_on_multiple=False,
        )
        if artifact_id is None or digest is None or size_bytes is None or content_type is None:
            continue
        candidates.append(
            (
                preferred_names.get(table.name, 0),
                table,
                _ArtifactColumns(
                    artifact_id=artifact_id,
                    digest=digest,
                    size_bytes=size_bytes,
                    content_type=content_type,
                ),
            )
        )
    if not candidates:
        raise _schema_conflict(
            "PIPELINE_ARTIFACT_SCHEMA_MISSING",
            "No artifact metadata table exposes the required identity contract.",
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        raise _schema_conflict(
            "PIPELINE_ARTIFACT_SCHEMA_AMBIGUOUS",
            "Multiple artifact metadata tables match the required identity contract.",
        )
    _, table, columns = candidates[0]
    return table, columns


def _resolve_binding_tables(
    tables: tuple[sa.Table, ...],
    artifact_table: sa.Table,
) -> tuple[_BindingTable, _BindingTable]:
    candidates = tuple(
        binding
        for table in tables
        if table is not artifact_table
        if (binding := _binding_table(table)) is not None
    )
    inputs = tuple(item for item in candidates if "input" in item.table.name)
    outputs = tuple(item for item in candidates if "output" in item.table.name)
    if len(inputs) == 1 and len(outputs) == 1:
        return (
            _with_fixed_direction(inputs[0], "input"),
            _with_fixed_direction(outputs[0], "output"),
        )
    shared = tuple(
        item for item in candidates if item.direction is not None or item.is_input is not None
    )
    if len(shared) == 1:
        return shared[0], shared[0]
    raise _schema_conflict(
        "PIPELINE_ARTIFACT_BINDING_SCHEMA_AMBIGUOUS",
        "Artifact input/output bindings do not have one unambiguous physical owner.",
    )


def _binding_table(table: sa.Table) -> _BindingTable | None:
    work_unit_id = table.c.get("work_unit_id")
    artifact_id = table.c.get("artifact_id")
    role = _optional_column(
        table,
        ("role", "artifact_role", "binding_role"),
        meaning="artifact binding role",
        fail_on_multiple=False,
    )
    if work_unit_id is None or artifact_id is None or role is None:
        return None
    ordinal = _optional_column(
        table,
        ("ordinal", "position", "sequence"),
        meaning="artifact binding ordinal",
        fail_on_multiple=False,
    )
    direction = _optional_column(
        table,
        ("direction", "binding_kind", "artifact_direction", "io_kind"),
        meaning="artifact binding direction",
        fail_on_multiple=False,
    )
    is_input = table.c.get("is_input")
    return _BindingTable(
        table=table,
        work_unit_id=work_unit_id,
        artifact_id=artifact_id,
        role=role,
        ordinal=ordinal,
        direction=direction,
        is_input=is_input,
        fixed_direction=None,
    )


def _with_fixed_direction(binding: _BindingTable, direction: str) -> _BindingTable:
    return _BindingTable(
        table=binding.table,
        work_unit_id=binding.work_unit_id,
        artifact_id=binding.artifact_id,
        role=binding.role,
        ordinal=binding.ordinal,
        direction=None,
        is_input=None,
        fixed_direction=direction,
    )


def _binding_rows(
    connection: Connection,
    binding: _BindingTable,
    artifact_table: sa.Table,
    artifact_columns: _ArtifactColumns,
    source_work_unit_id: UUID,
) -> tuple[_BoundArtifact, ...]:
    columns = [
        binding.role.label("binding_role"),
        binding.artifact_id.label("binding_artifact_id"),
        artifact_columns.digest.label("artifact_digest"),
        artifact_columns.size_bytes.label("artifact_size_bytes"),
        artifact_columns.content_type.label("artifact_content_type"),
    ]
    if binding.ordinal is not None:
        columns.append(binding.ordinal.label("binding_ordinal"))
    if binding.direction is not None:
        columns.append(binding.direction.label("binding_direction"))
    if binding.is_input is not None:
        columns.append(binding.is_input.label("binding_is_input"))
    rows = (
        connection.execute(
            sa.select(*columns)
            .select_from(
                binding.table.join(
                    artifact_table,
                    binding.artifact_id == artifact_columns.artifact_id,
                )
            )
            .where(binding.work_unit_id == source_work_unit_id)
        )
        .mappings()
        .all()
    )
    result: list[_BoundArtifact] = []
    for index, row in enumerate(rows):
        direction = _binding_direction(binding, row, source_work_unit_id)
        ordinal_value = row.get("binding_ordinal", index)
        if not isinstance(ordinal_value, int) or ordinal_value < 0:
            raise _catalog_conflict(
                "PIPELINE_ARTIFACT_ORDINAL_INVALID",
                "Artifact binding ordinal is invalid.",
                source_work_unit_id,
                "Repair ordered artifact bindings before advancement.",
            )
        result.append(
            _BoundArtifact(
                direction=direction,
                ordinal=ordinal_value,
                artifact=ArtifactIdentity(
                    artifact_id=_uuid_value(row["binding_artifact_id"]),
                    role=_non_empty_text(row["binding_role"], "artifact binding role"),
                    content_digest=_non_empty_text(row["artifact_digest"], "artifact digest"),
                    size_bytes=_non_negative_int(row["artifact_size_bytes"], "artifact size"),
                    content_type=_non_empty_text(
                        row["artifact_content_type"],
                        "artifact content type",
                    ),
                ),
            )
        )
    return tuple(result)


def _binding_direction(
    binding: _BindingTable,
    row: RowMapping,
    source_work_unit_id: UUID,
) -> str:
    if binding.fixed_direction is not None:
        return binding.fixed_direction
    if binding.is_input is not None:
        value = row["binding_is_input"]
        if not isinstance(value, bool):
            raise _catalog_conflict(
                "PIPELINE_ARTIFACT_DIRECTION_INVALID",
                "Artifact binding direction flag is not boolean.",
                source_work_unit_id,
                "Repair artifact binding direction before advancement.",
            )
        return "input" if value else "output"
    value = _non_empty_text(row["binding_direction"], "artifact binding direction").lower()
    if value in _INPUT_DIRECTIONS:
        return "input"
    if value in _OUTPUT_DIRECTIONS:
        return "output"
    raise PipelineAdvancementConflict(
        code="PIPELINE_ARTIFACT_DIRECTION_UNSUPPORTED",
        message="Artifact binding direction is not part of the canonical pipeline contract.",
        context={
            "sourceWorkUnitId": str(source_work_unit_id),
            "direction": value,
        },
        required_action="Migrate the binding to an explicit input or output direction.",
    )


def _choose_column(
    table: sa.Table,
    names: tuple[str, ...],
    *,
    meaning: str,
) -> sa.Column[object]:
    value = _optional_column(table, names, meaning=meaning)
    if value is None:
        raise _schema_conflict(
            "PIPELINE_SCHEMA_COLUMN_MISSING",
            f"The {meaning} column is missing from {table.fullname}.",
        )
    return value


def _optional_column(
    table: sa.Table,
    names: tuple[str, ...],
    *,
    meaning: str,
    fail_on_multiple: bool = True,
) -> sa.Column[object] | None:
    found = tuple(table.c[name] for name in names if name in table.c)
    if len(found) > 1 and fail_on_multiple:
        raise _schema_conflict(
            "PIPELINE_SCHEMA_COLUMN_AMBIGUOUS",
            f"Multiple {meaning} columns exist in {table.fullname}.",
        )
    return None if not found else found[0]


def _require_columns(table: sa.Table, names: tuple[str, ...]) -> None:
    missing = tuple(name for name in names if name not in table.c)
    if missing:
        raise _schema_conflict(
            "PIPELINE_SCHEMA_COLUMN_MISSING",
            f"Required columns are missing from {table.fullname}: {', '.join(missing)}.",
        )


def _schema_conflict(code: str, message: str) -> PipelineAdvancementConflict:
    return PipelineAdvancementConflict(
        code=code,
        message=message,
        context={},
        required_action="Apply the exact database migration or update the explicit schema adapter.",
    )


def _catalog_conflict(
    code: str,
    message: str,
    source_work_unit_id: UUID,
    required_action: str,
) -> PipelineAdvancementConflict:
    return PipelineAdvancementConflict(
        code=code,
        message=message,
        context={"sourceWorkUnitId": str(source_work_unit_id)},
        required_action=required_action,
    )


def _required_uuid(row: RowMapping, key: str) -> UUID:
    return _uuid_value(row[key])


def _uuid_value(value: object) -> UUID:
    if value is None:
        raise TypeError("persisted UUID value is null")
    return value if isinstance(value, UUID) else UUID(str(value))


def _required_text(row: RowMapping, key: str) -> str:
    return _non_empty_text(row[key], key)


def _required_column_text(row: RowMapping, column: sa.Column[object]) -> str:
    return _non_empty_text(row[column.name], column.name)


def _non_empty_text(value: object, meaning: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"persisted {meaning} is not non-empty text")
    return value


def _non_negative_int(value: object, meaning: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise TypeError(f"persisted {meaning} is not a non-negative integer")
    return value
