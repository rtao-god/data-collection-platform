from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import Select

from collection_application import WorkStage
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineAdvancementConflict,
    SucceededWorkOutput,
)
from collection_infrastructure.postgres.artifact_metadata import (
    artifact_objects,
    artifact_records,
    work_input_artifacts,
    work_output_artifacts,
)
from collection_infrastructure.postgres.pipeline_advancement_metadata import (
    pipeline_advancements,
)
from collection_infrastructure.postgres.work_metadata import stage_runs, work_units


@dataclass(frozen=True, slots=True)
class _BoundArtifact:
    position: int
    artifact: ArtifactIdentity


class PostgresSucceededWorkCatalog:
    """Read canonical succeeded-work output and discover unregistered work."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

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
        work = connection.execute(_work_statement(source_work_unit_id)).mappings().one_or_none()
        if work is None:
            raise _catalog_conflict(
                "PIPELINE_SOURCE_WORK_NOT_FOUND",
                "The source work unit does not exist.",
                source_work_unit_id,
                "Refresh canonical work state and select an existing work unit.",
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
        stage = (
            connection.execute(
                sa.select(stage_runs).where(stage_runs.c.stage_run_id == stage_run_id)
            )
            .mappings()
            .one_or_none()
        )
        if stage is None:
            raise _catalog_conflict(
                "PIPELINE_STAGE_OWNER_NOT_FOUND",
                "The successful work has no canonical stage owner.",
                source_work_unit_id,
                "Repair stage ownership before advancing the successful output.",
            )
        if _required_uuid(stage, "run_id") != run_id:
            raise _catalog_conflict(
                "PIPELINE_STAGE_RUN_CONFLICT",
                "The work and stage owners belong to different collection runs.",
                source_work_unit_id,
                "Repair the corrupted run/stage ownership before advancement.",
            )

        inputs = _load_artifacts(connection, work_input_artifacts, source_work_unit_id)
        outputs = _load_artifacts(connection, work_output_artifacts, source_work_unit_id)
        if len(outputs) != 1:
            raise PipelineAdvancementConflict(
                code="PIPELINE_OUTPUT_ARTIFACT_CARDINALITY",
                message="Successful work must bind exactly one canonical output artifact.",
                context={
                    "sourceWorkUnitId": str(source_work_unit_id),
                    "outputArtifactCount": len(outputs),
                },
                required_action=(
                    "Repair work completion so the exact output contract has one "
                    "canonical artifact."
                ),
            )

        input_ids = {item.artifact.artifact_id for item in inputs}
        output = outputs[0].artifact
        if output.artifact_id in input_ids:
            raise _catalog_conflict(
                "PIPELINE_ARTIFACT_DIRECTION_CONFLICT",
                "One artifact is bound as both pipeline input and output.",
                source_work_unit_id,
                "Repair immutable artifact bindings before advancement.",
            )
        output_digest = _required_text(work, "output_digest")
        if output_digest != output.content_digest:
            raise PipelineAdvancementConflict(
                code="PIPELINE_OUTPUT_DIGEST_CONFLICT",
                message="Work completion and output artifact digests differ.",
                context={
                    "sourceWorkUnitId": str(source_work_unit_id),
                    "workOutputDigest": output_digest,
                    "artifactDigest": output.content_digest,
                },
                required_action="Repair the immutable completion record before advancement.",
            )

        return SucceededWorkOutput(
            source_work_unit_id=source_work_unit_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage=WorkStage(_required_text(stage, "stage")),
            capability=_required_text(work, "capability"),
            output_contract=_required_text(work, "output_contract"),
            output_digest=output_digest,
            output_artifact=output,
            input_artifacts=tuple(item.artifact for item in inputs),
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
                work_ids = tuple(
                    _uuid_value(value)
                    for value in connection.execute(
                        _unregistered_succeeded_statement(limit)
                    ).scalars()
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


def _work_statement(source_work_unit_id: UUID) -> Select[tuple[object, ...]]:
    return (
        sa.select(work_units)
        .where(work_units.c.work_id == source_work_unit_id)
        .with_for_update(read=True)
    )


def _artifact_statement(
    binding: sa.Table,
    source_work_unit_id: UUID,
) -> Select[tuple[object, ...]]:
    return (
        sa.select(
            binding.c.position.label("binding_position"),
            binding.c.role.label("binding_role"),
            artifact_records.c.artifact_id.label("artifact_id"),
            artifact_objects.c.content_digest.label("content_digest"),
            artifact_objects.c.size_bytes.label("size_bytes"),
            artifact_records.c.content_type.label("content_type"),
        )
        .select_from(
            binding.join(
                artifact_records,
                binding.c.artifact_id == artifact_records.c.artifact_id,
            ).join(
                artifact_objects,
                artifact_records.c.object_id == artifact_objects.c.object_id,
            )
        )
        .where(binding.c.work_id == source_work_unit_id)
        .order_by(binding.c.position, artifact_records.c.artifact_id)
    )


def _unregistered_succeeded_statement(limit: int) -> Select[tuple[UUID]]:
    return (
        sa.select(work_units.c.work_id)
        .where(
            work_units.c.state == "succeeded",
            ~sa.exists(
                sa.select(sa.literal(1)).where(
                    pipeline_advancements.c.source_work_unit_id == work_units.c.work_id
                )
            ),
        )
        .order_by(work_units.c.updated_at_utc, work_units.c.work_id)
        .limit(limit)
    )


def _load_artifacts(
    connection: Connection,
    binding: sa.Table,
    source_work_unit_id: UUID,
) -> tuple[_BoundArtifact, ...]:
    rows = connection.execute(_artifact_statement(binding, source_work_unit_id)).mappings()
    result = tuple(_bound_artifact(row) for row in rows)
    artifact_ids = tuple(item.artifact.artifact_id for item in result)
    if len(artifact_ids) != len(set(artifact_ids)):
        raise _catalog_conflict(
            "PIPELINE_ARTIFACT_BINDING_DUPLICATE",
            "Successful work contains duplicate artifact bindings.",
            source_work_unit_id,
            "Repair duplicate immutable bindings before advancement.",
        )
    roles = tuple(item.artifact.role for item in result)
    if len(roles) != len(set(roles)):
        raise _catalog_conflict(
            "PIPELINE_ARTIFACT_ROLE_DUPLICATE",
            "Successful work contains duplicate artifact roles.",
            source_work_unit_id,
            "Repair duplicate immutable roles before advancement.",
        )
    return result


def _bound_artifact(row: RowMapping) -> _BoundArtifact:
    position = row["binding_position"]
    if not isinstance(position, int) or position < 0:
        raise TypeError("persisted artifact binding position is invalid")
    return _BoundArtifact(
        position=position,
        artifact=ArtifactIdentity(
            artifact_id=_uuid_value(row["artifact_id"]),
            role=_text_value(row["binding_role"], "artifact binding role"),
            content_digest=_text_value(row["content_digest"], "artifact digest"),
            size_bytes=_non_negative_int(row["size_bytes"], "artifact size"),
            content_type=_text_value(row["content_type"], "artifact content type"),
        ),
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


def _required_text(row: RowMapping, key: str) -> str:
    return _text_value(row[key], key)


def _uuid_value(value: object) -> UUID:
    if value is None:
        raise TypeError("persisted UUID value is null")
    return value if isinstance(value, UUID) else UUID(str(value))


def _text_value(value: object, meaning: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"persisted {meaning} is not non-empty text")
    return value


def _non_negative_int(value: object, meaning: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise TypeError(f"persisted {meaning} is not a non-negative integer")
    return value
