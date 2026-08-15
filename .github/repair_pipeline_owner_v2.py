from __future__ import annotations

from pathlib import Path


METADATA_SOURCE = '''from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from collection_infrastructure.postgres.metadata import collector_metadata
from collection_infrastructure.postgres.work_metadata import (
    collection_runs,
    stage_runs,
    work_units,
)

pipeline_advancement_metadata = collector_metadata

pipeline_advancements = sa.Table(
    "pipeline_advancements",
    pipeline_advancement_metadata,
    sa.Column("advancement_id", sa.Uuid(), primary_key=True),
    sa.Column(
        "source_work_unit_id",
        sa.Uuid(),
        sa.ForeignKey(work_units.c.work_id, ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "run_id",
        sa.Uuid(),
        sa.ForeignKey(collection_runs.c.run_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "stage_run_id",
        sa.Uuid(),
        sa.ForeignKey(stage_runs.c.stage_run_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("source_stage", sa.Text(), nullable=False),
    sa.Column("source_capability", sa.Text(), nullable=False),
    sa.Column("source_output_contract", sa.Text(), nullable=False),
    sa.Column("source_output_digest", sa.Text(), nullable=False),
    sa.Column("source_output_artifact_id", sa.Uuid(), nullable=False),
    sa.Column("source_output_artifact_role", sa.Text(), nullable=False),
    sa.Column("source_output_artifact_digest", sa.Text(), nullable=False),
    sa.Column("source_output_artifact_size_bytes", sa.BigInteger(), nullable=False),
    sa.Column("source_output_artifact_content_type", sa.Text(), nullable=False),
    sa.Column(
        "source_input_artifacts",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    ),
    sa.Column("transition_key", sa.Text(), nullable=False),
    sa.Column("transition_plan_digest", sa.Text(), nullable=False),
    sa.Column("state", sa.Text(), nullable=False),
    sa.Column("revision", sa.BigInteger(), nullable=False),
    sa.Column("attempt_count", sa.BigInteger(), nullable=False),
    sa.Column("result_digest", sa.Text(), nullable=True),
    sa.Column("blocker_owner", sa.Text(), nullable=True),
    sa.Column("blocker_code", sa.Text(), nullable=True),
    sa.Column("blocker_message", sa.Text(), nullable=True),
    sa.Column("blocker_required_action", sa.Text(), nullable=True),
    sa.Column("blocker_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column("active_lease_id", sa.Uuid(), nullable=True),
    sa.Column("active_lease_token_digest", sa.Text(), nullable=True),
    sa.Column("leased_by_worker_id", sa.Text(), nullable=True),
    sa.Column("dagster_execution_id", sa.Text(), nullable=True),
    sa.Column("dagster_build_id", sa.Text(), nullable=True),
    sa.Column("lease_issued_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("lease_expires_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text(), nullable=False),
    sa.CheckConstraint(
        "source_output_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_pipeline_advancements_output_digest",
    ),
    sa.CheckConstraint(
        "source_output_artifact_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_pipeline_advancements_artifact_digest",
    ),
    sa.CheckConstraint(
        "transition_plan_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_pipeline_advancements_plan_digest",
    ),
    sa.CheckConstraint(
        "result_digest IS NULL OR result_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_pipeline_advancements_result_digest",
    ),
    sa.CheckConstraint(
        "source_output_artifact_size_bytes >= 0",
        name="ck_pipeline_advancements_artifact_size",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(source_input_artifacts) = 'array'",
        name="ck_pipeline_advancements_input_artifacts",
    ),
    sa.CheckConstraint(
        "state IN ('pending', 'leased', 'applied', 'blocked')",
        name="ck_pipeline_advancements_state",
    ),
    sa.CheckConstraint(
        "revision >= 0 AND attempt_count >= 0",
        name="ck_pipeline_advancements_revisions",
    ),
    schema="work",
)

sa.Index(
    "ix_pipeline_advancements_claim",
    pipeline_advancements.c.state,
    pipeline_advancements.c.created_at_utc,
    pipeline_advancements.c.advancement_id,
)
sa.Index(
    "ix_pipeline_advancements_expiry",
    pipeline_advancements.c.state,
    pipeline_advancements.c.lease_expires_at_utc,
    postgresql_where=pipeline_advancements.c.state == "leased",
)
sa.Index(
    "ix_pipeline_advancements_run_state",
    pipeline_advancements.c.run_id,
    pipeline_advancements.c.state,
)

pipeline_advancement_attempts = sa.Table(
    "pipeline_advancement_attempts",
    pipeline_advancement_metadata,
    sa.Column("event_id", sa.Uuid(), primary_key=True),
    sa.Column(
        "advancement_id",
        sa.Uuid(),
        sa.ForeignKey(pipeline_advancements.c.advancement_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("attempt_number", sa.BigInteger(), nullable=False),
    sa.Column("event_kind", sa.Text(), nullable=False),
    sa.Column("lease_id", sa.Uuid(), nullable=True),
    sa.Column("lease_token_digest", sa.Text(), nullable=True),
    sa.Column("worker_id", sa.Text(), nullable=True),
    sa.Column("dagster_execution_id", sa.Text(), nullable=True),
    sa.Column("dagster_build_id", sa.Text(), nullable=True),
    sa.Column("transition_plan_digest", sa.Text(), nullable=False),
    sa.Column("lease_expires_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("result_digest", sa.Text(), nullable=True),
    sa.Column("blocker_owner", sa.Text(), nullable=True),
    sa.Column("blocker_code", sa.Text(), nullable=True),
    sa.Column("blocker_message", sa.Text(), nullable=True),
    sa.Column("blocker_required_action", sa.Text(), nullable=True),
    sa.Column("blocker_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column("occurred_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text(), nullable=False),
    sa.UniqueConstraint(
        "advancement_id",
        "attempt_number",
        "event_kind",
        name="uq_pipeline_advancement_attempts_event",
    ),
    schema="work",
)

sa.Index(
    "ix_pipeline_advancement_attempts_advancement",
    pipeline_advancement_attempts.c.advancement_id,
    pipeline_advancement_attempts.c.occurred_at_utc,
)

PIPELINE_ADVANCEMENT_TABLES = (
    pipeline_advancements,
    pipeline_advancement_attempts,
)
'''


CATALOG_SOURCE = '''from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineAdvancementConflict,
    SucceededWorkOutput,
)
from collection_domain import WorkStage
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import Select

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
        work = (
            connection.execute(_work_statement(source_work_unit_id))
            .mappings()
            .one_or_none()
        )
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
    rows = connection.execute(
        _artifact_statement(binding, source_work_unit_id)
    ).mappings()
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
'''


CATALOG_TEST_SOURCE = '''from __future__ import annotations

from uuid import UUID

from sqlalchemy.dialects import postgresql

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
    foreign_key = next(
        iter(pipeline_advancements.c.source_work_unit_id.foreign_keys)
    )

    assert foreign_key.target_fullname == "work.work_units.work_id"
    assert foreign_key.ondelete == "RESTRICT"
'''


def _write(path: str, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected repair anchor is missing")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> int:
    _write(
        "packages/collection_infrastructure/src/collection_infrastructure/postgres/"
        "pipeline_advancement_metadata.py",
        METADATA_SOURCE,
    )
    _write(
        "packages/collection_infrastructure/src/collection_infrastructure/postgres/"
        "succeeded_work_catalog.py",
        CATALOG_SOURCE,
    )
    _write(
        "packages/collection_infrastructure/tests/test_succeeded_work_catalog.py",
        CATALOG_TEST_SOURCE,
    )

    migration = Path("database/migrations/versions/20260815_0013_pipeline_advancement.py")
    migration_text = migration.read_text(encoding="utf-8")
    migration_text = migration_text.replace(
        '"work.work_units.work_unit_id"',
        '"work.work_units.work_id"',
    )
    if '"work.work_units.work_unit_id"' in migration_text:
        raise RuntimeError("obsolete work identity remains in migration")
    if '"work.work_units.work_id"' not in migration_text:
        raise RuntimeError("canonical work identity is absent from migration")
    migration.write_text(migration_text, encoding="utf-8")

    schema_test = Path("database/tests/test_pipeline_advancement_schema.py")
    schema_text = schema_test.read_text(encoding="utf-8")
    schema_text = schema_text.replace(
        '    assert "ON DELETE RESTRICT" in source\n',
        '    assert \'ondelete="RESTRICT"\' in source\n',
    )
    schema_anchor = '    assert \'schema="work"\' in source\n'
    schema_assertions = (
        '    assert \'"work.work_units.work_id"\' in source\n'
        '    assert \'"work.work_units.work_unit_id"\' not in source\n'
    )
    if schema_assertions not in schema_text:
        if schema_anchor not in schema_text:
            raise RuntimeError("pipeline schema test anchor is missing")
        schema_text = schema_text.replace(
            schema_anchor,
            schema_anchor + schema_assertions,
            1,
        )
    schema_test.write_text(schema_text, encoding="utf-8")

    metadata_test = Path(
        "packages/collection_infrastructure/tests/"
        "test_pipeline_advancement_metadata.py"
    )
    metadata_text = metadata_test.read_text(encoding="utf-8")
    metadata_assertion = '''


def test_pipeline_advancement_metadata_targets_canonical_work_identity() -> None:
    from collection_infrastructure.postgres.pipeline_advancement_metadata import (
        pipeline_advancements,
    )

    foreign_key = next(
        iter(pipeline_advancements.c.source_work_unit_id.foreign_keys)
    )
    assert foreign_key.target_fullname == "work.work_units.work_id"
    assert foreign_key.ondelete == "RESTRICT"
'''
    if "test_pipeline_advancement_metadata_targets_canonical_work_identity" not in metadata_text:
        metadata_test.write_text(
            metadata_text.rstrip() + metadata_assertion,
            encoding="utf-8",
        )

    supervision_test = Path(
        "packages/collection_application/tests/test_pipeline_supervision.py"
    )
    supervision_text = supervision_test.read_text(encoding="utf-8")
    constants_anchor = (
        "        expires_at_utc=_NOW + timedelta(minutes=5),\n"
        "    )\n\n\n"
        "def _status(\n"
    )
    constants_replacement = (
        "        expires_at_utc=_NOW + timedelta(minutes=5),\n"
        "    )\n\n\n"
        "_DEFAULT_SOURCES = (_source(),)\n"
        "_DEFAULT_LEASE = _lease()\n\n\n"
        "def _status(\n"
    )
    if "_DEFAULT_SOURCES = (_source(),)" not in supervision_text:
        if constants_anchor not in supervision_text:
            raise RuntimeError("pipeline supervision constants anchor is missing")
        supervision_text = supervision_text.replace(
            constants_anchor,
            constants_replacement,
            1,
        )
    supervision_text = supervision_text.replace(
        "class Discovery:\n"
        "    def __init__(self, sources=(_source(),)) -> None:\n"
        "        self.sources = sources\n",
        "class Discovery:\n"
        "    def __init__(\n"
        "        self,\n"
        "        sources: tuple[SucceededWorkOutput, ...] = _DEFAULT_SOURCES,\n"
        "    ) -> None:\n"
        "        self.sources = sources\n",
        1,
    )
    supervision_text = supervision_text.replace(
        "class Port:\n"
        "    def __init__(self, *, lease=_lease()) -> None:\n"
        "        self.lease = lease\n",
        "class Port:\n"
        "    def __init__(\n"
        "        self,\n"
        "        *,\n"
        "        lease: PipelineAdvancementLease | None = _DEFAULT_LEASE,\n"
        "    ) -> None:\n"
        "        self.lease = lease\n",
        1,
    )
    if "sources=(_source(),)" in supervision_text or "lease=_lease()" in supervision_text:
        raise RuntimeError("unsafe test defaults remain in pipeline supervision tests")
    supervision_test.write_text(supervision_text, encoding="utf-8")

    advancement = Path(
        "packages/collection_infrastructure/src/collection_infrastructure/postgres/"
        "pipeline_advancement.py"
    )
    advancement_text = advancement.read_text(encoding="utf-8")
    advancement_text = advancement_text.replace(
        'message="The transition result digest differs from the requested completion.",',
        'message=(\n'
        '                            "The transition result digest differs from the "\n'
        '                            "requested completion."\n'
        '                        ),',
    )
    advancement_text = advancement_text.replace(
        '"Re-read the exact transition result and complete with its canonical digest."',
        '"Re-read the exact transition result and complete with its "\n'
        '                            "canonical digest."',
    )
    advancement.write_text(advancement_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
