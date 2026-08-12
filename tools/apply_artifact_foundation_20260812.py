from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def write(relative: str, content: str) -> None:
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def replace_once(relative: str, old: str, new: str) -> None:
    text = read(relative)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"{relative}: expected one occurrence, found {count}: {old[:100]!r}"
        )
    write(relative, text.replace(old, new, 1))


def patch_s3_adapter() -> None:
    relative = (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "object_store/s3.py"
    )
    text = read(relative)
    text = text.replace(
        "from dataclasses import dataclass\n",
        "from dataclasses import dataclass\nfrom importlib import import_module\n",
        1,
    )
    text = text.replace(
        "from typing import Protocol, cast\n",
        "from typing import Any, Protocol, cast\n",
        1,
    )
    text = text.replace(
        "\nimport boto3\nfrom botocore.client import Config\n"
        "from botocore.exceptions import ClientError\n",
        "\n",
        1,
    )
    replace_factory = '''        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region_name,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return cls(cast(S3Client, client), bucket=bucket)
'''
    replacement_factory = '''        try:
            boto3_module = import_module("boto3")
            config_module = import_module("botocore.config")
            client_factory = cast(Any, getattr(boto3_module, "client"))
            config_type = cast(Any, getattr(config_module, "Config"))
            client = client_factory(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name=region_name,
                config=config_type(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        except (AttributeError, ImportError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "The S3 artifact adapter requires compatible boto3 and botocore packages"
            ) from exc
        return cls(cast(S3Client, client), bucket=bucket)
'''
    if replace_factory not in text:
        raise RuntimeError("S3 client factory pattern is not current")
    text = text.replace(replace_factory, replacement_factory, 1)

    get_staging = '''    def _get_staging(self, staging_reference: str) -> Mapping[str, object]:
        try:
            return self._client.get_object(Bucket=self._bucket, Key=staging_reference)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "unknown"))
            raise ArtifactObjectStoreError(
                code="ARTIFACT_UPLOAD_NOT_FOUND",
                message="The prepared artifact upload is not available for verification.",
                context={"stagingReference": staging_reference, "storageCode": code},
                required_action="Upload the exact body to the prepared URL before verification.",
            ) from exc
        except Exception as exc:
            raise ArtifactObjectStoreError(
                code="ARTIFACT_UPLOAD_READ_FAILED",
                message="The object store could not read the prepared artifact upload.",
                context={
                    "stagingReference": staging_reference,
                    "causeType": type(exc).__name__,
                },
                required_action="Restore the object store and retry artifact verification.",
            ) from exc
'''
    get_staging_replacement = '''    def _get_staging(self, staging_reference: str) -> Mapping[str, object]:
        try:
            return self._client.get_object(Bucket=self._bucket, Key=staging_reference)
        except Exception as exc:
            code, status = _storage_error(exc)
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                raise ArtifactObjectStoreError(
                    code="ARTIFACT_UPLOAD_NOT_FOUND",
                    message="The prepared artifact upload is not available for verification.",
                    context={"stagingReference": staging_reference, "storageCode": code},
                    required_action=(
                        "Upload the exact body to the prepared URL before verification."
                    ),
                ) from exc
            raise ArtifactObjectStoreError(
                code="ARTIFACT_UPLOAD_READ_FAILED",
                message="The object store could not read the prepared artifact upload.",
                context={
                    "stagingReference": staging_reference,
                    "storageCode": code,
                    "causeType": type(exc).__name__,
                },
                required_action="Restore the object store and retry artifact verification.",
            ) from exc
'''
    if get_staging not in text:
        raise RuntimeError("S3 staging read pattern is not current")
    text = text.replace(get_staging, get_staging_replacement, 1)

    head_block = '''        try:
            response = self._client.head_object(Bucket=self._bucket, Key=final_reference)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = str(exc.response.get("Error", {}).get("Code", "unknown"))
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise ArtifactObjectStoreError(
                code="ARTIFACT_FINAL_HEAD_FAILED",
                message="The content-addressed artifact could not be inspected.",
                context={"finalReference": final_reference, "storageCode": code},
                required_action="Restore the object store and retry artifact verification.",
            ) from exc
        except Exception as exc:
            raise ArtifactObjectStoreError(
                code="ARTIFACT_FINAL_HEAD_FAILED",
                message="The content-addressed artifact could not be inspected.",
                context={
                    "finalReference": final_reference,
                    "causeType": type(exc).__name__,
                },
                required_action="Restore the object store and retry artifact verification.",
            ) from exc
'''
    head_replacement = '''        try:
            response = self._client.head_object(Bucket=self._bucket, Key=final_reference)
        except Exception as exc:
            code, status = _storage_error(exc)
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise ArtifactObjectStoreError(
                code="ARTIFACT_FINAL_HEAD_FAILED",
                message="The content-addressed artifact could not be inspected.",
                context={
                    "finalReference": final_reference,
                    "storageCode": code,
                    "causeType": type(exc).__name__,
                },
                required_action="Restore the object store and retry artifact verification.",
            ) from exc
'''
    if head_block not in text:
        raise RuntimeError("S3 final-object inspection pattern is not current")
    text = text.replace(head_block, head_replacement, 1)

    helper = '''

def _storage_error(exc: Exception) -> tuple[str, int | None]:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return type(exc).__name__, None
    error = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = str(error.get("Code", "unknown")) if isinstance(error, Mapping) else "unknown"
    status_value = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    return code, status_value if isinstance(status_value, int) else None
'''
    anchor = "\n\ndef _staging_reference("
    if anchor not in text:
        raise RuntimeError("S3 helper anchor is missing")
    text = text.replace(anchor, helper + anchor, 1)
    write(relative, text)


def patch_dependencies() -> None:
    relative = "packages/collection_infrastructure/pyproject.toml"
    text = read(relative)
    if '"boto3' not in text:
        text = text.replace(
            'dependencies = [\n  "alembic==1.18.5",\n',
            'dependencies = [\n  "alembic==1.18.5",\n  "boto3",\n  "botocore",\n',
            1,
        )
    write(relative, text)


def patch_artifact_metadata() -> None:
    relative = (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/artifact_metadata.py"
    )
    text = read(relative)
    old = '''work_output_artifacts = sa.Table(
    "work_output_artifacts",
    collector_metadata,
    sa.Column(
        "work_id",
        sa.Uuid,
        sa.ForeignKey("work.work_units.work_id"),
        primary_key=True,
    ),
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column(
        "artifact_id",
        sa.Uuid,
        sa.ForeignKey("sources.raw_artifacts.artifact_id"),
        nullable=False,
    ),
    sa.UniqueConstraint("artifact_id", name="uq_work_output_artifacts_artifact_id"),
    sa.CheckConstraint("position BETWEEN 0 AND 31", name="ck_work_output_artifacts_position"),
    schema=WORK_SCHEMA,
)

ARTIFACT_TABLES = (
    artifact_uploads,
    artifact_objects,
    raw_artifacts,
    work_output_artifacts,
)
'''
    new = '''work_input_artifacts = sa.Table(
    "work_input_artifacts",
    collector_metadata,
    sa.Column(
        "work_id",
        sa.Uuid,
        sa.ForeignKey("work.work_units.work_id"),
        primary_key=True,
    ),
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column(
        "artifact_id",
        sa.Uuid,
        sa.ForeignKey("sources.raw_artifacts.artifact_id"),
        nullable=False,
    ),
    sa.Column("role", sa.Text, nullable=False),
    sa.UniqueConstraint(
        "work_id",
        "artifact_id",
        name="uq_work_input_artifacts_work_artifact",
    ),
    sa.UniqueConstraint("work_id", "role", name="uq_work_input_artifacts_work_role"),
    sa.CheckConstraint("position BETWEEN 0 AND 31", name="ck_work_input_artifacts_position"),
    sa.CheckConstraint(
        "role ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$'",
        name="ck_work_input_artifacts_role",
    ),
    schema=WORK_SCHEMA,
)

work_output_artifacts = sa.Table(
    "work_output_artifacts",
    collector_metadata,
    sa.Column(
        "work_id",
        sa.Uuid,
        sa.ForeignKey("work.work_units.work_id"),
        primary_key=True,
    ),
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column(
        "artifact_id",
        sa.Uuid,
        sa.ForeignKey("sources.raw_artifacts.artifact_id"),
        nullable=False,
    ),
    sa.Column("role", sa.Text, nullable=False),
    sa.UniqueConstraint("artifact_id", name="uq_work_output_artifacts_artifact_id"),
    sa.UniqueConstraint("work_id", "role", name="uq_work_output_artifacts_work_role"),
    sa.CheckConstraint("position BETWEEN 0 AND 31", name="ck_work_output_artifacts_position"),
    sa.CheckConstraint(
        "role ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$'",
        name="ck_work_output_artifacts_role",
    ),
    schema=WORK_SCHEMA,
)

ARTIFACT_TABLES = (
    artifact_uploads,
    artifact_objects,
    raw_artifacts,
    work_input_artifacts,
    work_output_artifacts,
)
'''
    if old not in text:
        raise RuntimeError("artifact metadata binding pattern is not current")
    write(relative, text.replace(old, new, 1))


def patch_transfer_typing() -> None:
    relative = (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/artifact_transfer.py"
    )
    text = read(relative)
    text = text.replace("from typing import TypeVar\n", "from typing import Any, TypeVar\n", 1)
    text = text.replace("Mapping[str, object]", "Mapping[str, Any]")
    text = text.replace(
        'expected_size_bytes=int(prepared["expected_size_bytes"]),',
        'expected_size_bytes=_required_int(prepared, "expected_size_bytes"),',
    )
    text = text.replace(
        'size_bytes=int(row["expected_size_bytes"]),',
        'size_bytes=_required_int(row, "expected_size_bytes"),',
    )
    helper = '''

def _required_int(row: Mapping[str, Any], key: str) -> int:
    value = row[key]
    if not isinstance(value, int):
        raise _conflict(
            code="ARTIFACT_STORAGE_STATE_INVALID",
            message="Artifact persistence contains an invalid integer value.",
            context={"field": key, "actualType": type(value).__name__},
            required_action="Repair the artifact metadata through its owner migration path.",
        )
    return value
'''
    anchor = "\n\ndef _stale_conflict("
    if anchor not in text:
        raise RuntimeError("artifact transfer helper anchor is missing")
    text = text.replace(anchor, helper + anchor, 1)
    write(relative, text)


def add_migration() -> None:
    write(
        "database/migrations/versions/20260812_0006_artifact_bindings.py",
        '''"""Add exact work input and output artifact bindings.

Revision ID: 20260812_0006
Revises: 20260811_0005
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260812_0006"
down_revision: str | None = "20260811_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE work.work_input_artifacts (
            work_id UUID NOT NULL,
            position INTEGER NOT NULL,
            artifact_id UUID NOT NULL,
            role TEXT NOT NULL,
            CONSTRAINT pk_work_input_artifacts PRIMARY KEY (work_id, position),
            CONSTRAINT fk_work_input_artifacts_work_id_work_units
                FOREIGN KEY(work_id) REFERENCES work.work_units (work_id),
            CONSTRAINT fk_work_input_artifacts_artifact_id_raw_artifacts
                FOREIGN KEY(artifact_id) REFERENCES sources.raw_artifacts (artifact_id),
            CONSTRAINT uq_work_input_artifacts_work_artifact UNIQUE (work_id, artifact_id),
            CONSTRAINT uq_work_input_artifacts_work_role UNIQUE (work_id, role),
            CONSTRAINT ck_work_input_artifacts_position CHECK (position BETWEEN 0 AND 31),
            CONSTRAINT ck_work_input_artifacts_role
                CHECK (role ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$')
        )
        """
    )
    op.execute("ALTER TABLE work.work_output_artifacts ADD COLUMN role TEXT")
    op.execute(
        """
        UPDATE work.work_output_artifacts
        SET role = 'artifact_' || position::text
        WHERE role IS NULL
        """
    )
    op.execute("ALTER TABLE work.work_output_artifacts ALTER COLUMN role SET NOT NULL")
    op.execute(
        """
        ALTER TABLE work.work_output_artifacts
        ADD CONSTRAINT uq_work_output_artifacts_work_role UNIQUE (work_id, role)
        """
    )
    op.execute(
        """
        ALTER TABLE work.work_output_artifacts
        ADD CONSTRAINT ck_work_output_artifacts_role
        CHECK (role ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$')
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE work.work_output_artifacts "
        "DROP CONSTRAINT ck_work_output_artifacts_role"
    )
    op.execute(
        "ALTER TABLE work.work_output_artifacts "
        "DROP CONSTRAINT uq_work_output_artifacts_work_role"
    )
    op.execute("ALTER TABLE work.work_output_artifacts DROP COLUMN role")
    op.execute("DROP TABLE work.work_input_artifacts")
''',
    )


def patch_exports() -> None:
    relative = (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/__init__.py"
    )
    text = read(relative)
    if "work_input_artifacts" not in text:
        text = text.replace(
            "    raw_artifacts,\n",
            "    raw_artifacts,\n    work_input_artifacts,\n    work_output_artifacts,\n",
            1,
        )
    if '"work_input_artifacts"' not in text:
        marker = '    "work_attempts",\n'
        if marker not in text:
            raise RuntimeError("PostgreSQL export anchor is missing")
        text = text.replace(
            marker,
            marker + '    "work_input_artifacts",\n    "work_output_artifacts",\n',
            1,
        )
    write(relative, text)


def patch_architecture_contract() -> None:
    relative = "tools/architecture_checks/check_dependencies.py"
    text = read(relative)
    patterns = (
        (
            'allowed_external_dependencies=frozenset(\n'
            '            {"alembic", "psycopg", "sqlalchemy"}\n'
            "        ),",
            'allowed_external_dependencies=frozenset(\n'
            '            {"alembic", "boto3", "botocore", "psycopg", "sqlalchemy"}\n'
            "        ),",
        ),
        (
            'allowed_external_dependencies=frozenset('
            '{"alembic", "psycopg", "sqlalchemy"}),',
            'allowed_external_dependencies=frozenset(\n'
            '            {"alembic", "boto3", "botocore", "psycopg", "sqlalchemy"}\n'
            "        ),",
        ),
    )
    for old, new in patterns:
        if old in text:
            text = text.replace(old, new, 1)
            break
    else:
        raise RuntimeError("collection-infrastructure dependency rule is not current")
    write(relative, text)

    relative = "docs/architecture/dependency-rules.md"
    text = read(relative).replace(
        "`alembic`, `psycopg`, `sqlalchemy`",
        "`alembic`, `boto3`, `botocore`, `psycopg`, `sqlalchemy`",
    )
    write(relative, text)


def add_metadata_tests() -> None:
    write(
        "packages/collection_infrastructure/tests/test_artifact_metadata.py",
        '''from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from collection_infrastructure.postgres.artifact_metadata import (
    ARTIFACT_TABLES,
    artifact_objects,
    artifact_uploads,
    raw_artifacts,
    work_input_artifacts,
    work_output_artifacts,
)


def test_artifact_metadata_has_exact_owner_tables() -> None:
    assert tuple(table.fullname for table in ARTIFACT_TABLES) == (
        "sources.artifact_uploads",
        "sources.artifact_objects",
        "sources.raw_artifacts",
        "work.work_input_artifacts",
        "work.work_output_artifacts",
    )
    assert artifact_uploads.primary_key.columns.keys() == ["upload_id"]
    assert artifact_objects.primary_key.columns.keys() == ["object_id"]
    assert raw_artifacts.primary_key.columns.keys() == ["artifact_id"]
    assert work_input_artifacts.primary_key.columns.keys() == ["work_id", "position"]
    assert work_output_artifacts.primary_key.columns.keys() == ["work_id", "position"]


def test_artifact_bindings_preserve_roles_and_immutable_evidence() -> None:
    dialect = postgresql.dialect()
    input_sql = str(CreateTable(work_input_artifacts).compile(dialect=dialect))
    output_sql = str(CreateTable(work_output_artifacts).compile(dialect=dialect))

    assert "CONSTRAINT uq_work_input_artifacts_work_artifact" in input_sql
    assert "CONSTRAINT uq_work_input_artifacts_work_role" in input_sql
    assert "CONSTRAINT ck_work_input_artifacts_role" in input_sql
    assert "CONSTRAINT uq_work_output_artifacts_artifact_id" in output_sql
    assert "CONSTRAINT uq_work_output_artifacts_work_role" in output_sql
    assert "CONSTRAINT ck_work_output_artifacts_role" in output_sql
    for table in ARTIFACT_TABLES:
        assert all(foreign_key.ondelete is None for foreign_key in table.foreign_keys)
        assert all(column.server_default is None for column in table.columns)
''',
    )


def patch_schema_test() -> None:
    relative = "database/tests/test_work_engine_schema.py"
    text = read(relative)
    current_sources = '''    assert set(inspector.get_table_names(schema="sources")) == {
        "artifact_objects",
        "artifact_uploads",
        "raw_artifacts",
        "source_capacity_states",
    }
'''
    stale_sources = '''    assert set(inspector.get_table_names(schema="sources")) == {
        "source_capacity_states",
    }
'''
    if stale_sources in text:
        text = text.replace(stale_sources, current_sources, 1)
    elif current_sources not in text:
        raise RuntimeError("source schema assertion is not current")
    if '"work_input_artifacts"' not in text:
        anchor = '        "work_output_artifacts",\n'
        if anchor in text:
            text = text.replace(anchor, '        "work_input_artifacts",\n' + anchor, 1)
        else:
            anchor = '        "work_units",\n'
            if anchor not in text:
                raise RuntimeError("work schema assertion anchor is missing")
            text = text.replace(
                anchor,
                anchor + '        "work_input_artifacts",\n        "work_output_artifacts",\n',
                1,
            )
    write(relative, text)


def patch_module_status() -> None:
    relative = ".codex/modules/work-engine.md"
    text = read(relative)
    old = '''Pre-signed object upload/read commands, uploaded-object size and digest verification, immutable raw
artifact metadata, and atomic artifact-plus-completion commit are not implemented yet. No worker may
receive PostgreSQL credentials or bypass `WorkEngineService`; the future artifact routes must compose
through an application-owned object-store port and preserve this boundary.
'''
    new = '''The application-owned artifact transfer port, S3-compatible content-addressed adapter, immutable
upload/object/raw-artifact metadata, and exact work input/output binding schema are implemented. The
Worker Gateway routes, runtime object-store composition, and atomic verified-artifact-plus-completion
transaction remain pending. No worker may receive PostgreSQL credentials or bypass application
services; the remaining artifact routes must preserve this boundary.
'''
    if old not in text:
        raise RuntimeError("work-engine remaining-boundary text is not current")
    write(relative, text.replace(old, new, 1))


def main() -> None:
    patch_s3_adapter()
    patch_dependencies()
    patch_artifact_metadata()
    patch_transfer_typing()
    add_migration()
    patch_exports()
    patch_architecture_contract()
    add_metadata_tests()
    patch_schema_test()
    patch_module_status()


if __name__ == "__main__":
    main()
