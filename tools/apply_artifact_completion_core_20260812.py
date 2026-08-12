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


def patch_domain_contract() -> None:
    write(
        "packages/collection_domain/src/collection_domain/work_artifacts.py",
        '''from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

_ROLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$")


@dataclass(frozen=True, slots=True)
class WorkInputArtifact:
    artifact_id: UUID
    role: str

    def __post_init__(self) -> None:
        if _ROLE_PATTERN.fullmatch(self.role) is None:
            raise ValueError("work artifact role has an invalid format")


def validate_work_input_artifacts(artifacts: tuple[WorkInputArtifact, ...]) -> None:
    if len(artifacts) > 32:
        raise ValueError("work input cannot contain more than 32 artifact bindings")
    identities = tuple(item.artifact_id for item in artifacts)
    roles = tuple(item.role for item in artifacts)
    if len(set(identities)) != len(identities):
        raise ValueError("work input artifact identities must be unique")
    if len(set(roles)) != len(roles):
        raise ValueError("work input artifact roles must be unique")
''',
    )

    relative = "packages/collection_application/src/collection_application/work_artifacts.py"
    text = read(relative)
    text = text.replace(
        "from dataclasses import dataclass\n",
        "from dataclasses import dataclass\n\nfrom collection_domain import WorkInputArtifact\n",
        1,
    )
    text, count = re.subn(
        r"@dataclass\(frozen=True, slots=True\)\nclass WorkInputArtifact:.*?\n\n\n@dataclass",
        "@dataclass",
        text,
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise RuntimeError("application WorkInputArtifact block is not current")
    write(relative, text)

    relative = "packages/collection_domain/src/collection_domain/__init__.py"
    text = read(relative)
    if "from collection_domain.work_artifacts import" not in text:
        anchor = "from collection_domain.work_leases import"
        position = text.find(anchor)
        if position < 0:
            raise RuntimeError("domain public import anchor is missing")
        text = (
            text[:position]
            + "from collection_domain.work_artifacts import (\n"
            + "    WorkInputArtifact,\n"
            + "    validate_work_input_artifacts,\n"
            + ")\n"
            + text[position:]
        )
    if '"WorkInputArtifact"' not in text:
        marker = '    "WorkFailureKind",\n'
        if marker not in text:
            marker = "__all__ = [\n"
        text = text.replace(marker, marker + '    "WorkInputArtifact",\n', 1)
    if '"validate_work_input_artifacts"' not in text:
        position = text.rfind("]\n")
        text = text[:position] + '    "validate_work_input_artifacts",\n' + text[position:]
    write(relative, text)

    relative = "packages/collection_domain/src/collection_domain/work_leases.py"
    text = read(relative)
    if "from collection_domain.work_artifacts import" not in text:
        anchor = "from collection_domain.work_units import"
        position = text.find(anchor)
        if position < 0:
            raise RuntimeError("work lease import anchor is missing")
        text = (
            text[:position]
            + "from collection_domain.work_artifacts import (\n"
            + "    WorkInputArtifact,\n"
            + "    validate_work_input_artifacts,\n"
            + ")\n"
            + text[position:]
        )
    match = re.search(
        r"(@dataclass\(frozen=True, slots=True\)\nclass WorkLease:.*?)(?=\n\nclass |\Z)",
        text,
        re.S,
    )
    if match is None:
        raise RuntimeError("WorkLease class is missing")
    block = match.group(1)
    if "input_artifacts:" not in block:
        block = block.replace(
            "    correlation_id: str\n",
            "    correlation_id: str\n"
            "    input_artifacts: tuple[WorkInputArtifact, ...] = ()\n",
            1,
        )
        block = block.replace(
            "    def __post_init__(self) -> None:\n",
            "    def __post_init__(self) -> None:\n"
            "        validate_work_input_artifacts(self.input_artifacts)\n",
            1,
        )
    write(relative, text[: match.start()] + block + text[match.end() :])


def patch_application_commands() -> None:
    relative = "packages/collection_application/src/collection_application/work_engine.py"
    text = read(relative)
    if "from collection_application.work_artifacts import" not in text:
        anchor = "from collection_contracts import owner_error\n"
        if anchor not in text:
            raise RuntimeError("application work-engine import anchor is missing")
        text = text.replace(
            anchor,
            anchor
            + "from collection_application.work_artifacts import (\n"
            + "    WorkInputArtifact,\n"
            + "    WorkOutputArtifact,\n"
            + "    validate_artifact_bindings,\n"
            + ")\n",
            1,
        )
    definitions = (
        (
            "WorkUnitSpec",
            "    input_artifacts: tuple[WorkInputArtifact, ...] = ()\n",
            '''        validate_artifact_bindings(
            identities=tuple(item.artifact_id for item in self.input_artifacts),
            roles=tuple(item.role for item in self.input_artifacts),
            owner_name="work input",
        )
''',
        ),
        (
            "WorkCompletion",
            "    output_artifacts: tuple[WorkOutputArtifact, ...] = ()\n",
            '''        validate_artifact_bindings(
            identities=tuple(item.upload_id for item in self.output_artifacts),
            roles=tuple(item.role for item in self.output_artifacts),
            owner_name="work output",
        )
''',
        ),
    )
    for class_name, field, validation in definitions:
        match = re.search(
            rf"(@dataclass\(frozen=True, slots=True\)\nclass {class_name}:.*?)"
            r"(?=\n\n@dataclass|\n\nclass )",
            text,
            re.S,
        )
        if match is None:
            raise RuntimeError(f"{class_name} command block is missing")
        block = match.group(1)
        if field.strip() not in block:
            block = block.replace("    correlation_id: str\n", "    correlation_id: str\n" + field, 1)
            block = block.replace(
                "    def __post_init__(self) -> None:\n",
                "    def __post_init__(self) -> None:\n" + validation,
                1,
            )
        text = text[: match.start()] + block + text[match.end() :]
    write(relative, text)


def patch_database_contract() -> None:
    relative = (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/artifact_metadata.py"
    )
    text = read(relative)
    raw_start = text.find("raw_artifacts = sa.Table(")
    raw_end = text.find("\n\nwork_input_artifacts =", raw_start)
    if raw_start < 0 or raw_end < 0:
        raise RuntimeError("raw-artifact metadata block is missing")
    raw = text[raw_start:raw_end]
    if 'sa.Column("artifact_kind", sa.Text, nullable=False),' not in raw:
        raw = raw.replace(
            '    sa.Column("content_type", sa.Text, nullable=False),\n',
            '    sa.Column("artifact_kind", sa.Text, nullable=False),\n'
            '    sa.Column("content_type", sa.Text, nullable=False),\n',
            1,
        )
    if "fk_raw_artifacts_upload_lineage" not in raw:
        raw = raw.replace(
            '    sa.Column("correlation_id", sa.Text, nullable=False),\n',
            '''    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.ForeignKeyConstraint(
        ("upload_id", "work_id", "worker_id"),
        (
            "sources.artifact_uploads.upload_id",
            "sources.artifact_uploads.work_id",
            "sources.artifact_uploads.worker_id",
        ),
        name="fk_raw_artifacts_upload_lineage",
    ),
    sa.ForeignKeyConstraint(
        ("attempt_id", "work_id", "worker_id"),
        (
            "work.work_attempts.attempt_id",
            "work.work_attempts.work_id",
            "work.work_attempts.worker_id",
        ),
        name="fk_raw_artifacts_attempt_lineage",
    ),
''',
            1,
        )
    if "ck_raw_artifacts_kind" not in raw:
        raw = raw.replace(
            '    sa.CheckConstraint(\n        "content_type ~',
            '    sa.CheckConstraint(\n'
            '        _in_values("artifact_kind", _ARTIFACT_KINDS),\n'
            '        name="ck_raw_artifacts_kind",\n'
            '    ),\n'
            '    sa.CheckConstraint(\n'
            '        "content_type ~',
            1,
        )
    text = text[:raw_start] + raw + text[raw_end:]
    if "uq_artifact_uploads_lineage" not in text:
        anchor = (
            '    sa.UniqueConstraint("staging_reference", '
            'name="uq_artifact_uploads_staging_reference"),\n'
        )
        if anchor not in text:
            raise RuntimeError("artifact upload unique anchor is missing")
        text = text.replace(
            anchor,
            anchor
            + "    sa.UniqueConstraint(\n"
            + '        "upload_id",\n'
            + '        "work_id",\n'
            + '        "worker_id",\n'
            + '        name="uq_artifact_uploads_lineage",\n'
            + "    ),\n",
            1,
        )
    write(relative, text)

    relative = (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/work_metadata.py"
    )
    text = read(relative)
    if "uq_work_attempts_lineage" not in text:
        anchor = '    sa.UniqueConstraint("lease_token", name="uq_work_attempts_lease_token"),\n'
        if anchor not in text:
            raise RuntimeError("work-attempt unique anchor is missing")
        text = text.replace(
            anchor,
            anchor
            + "    sa.UniqueConstraint(\n"
            + '        "attempt_id",\n'
            + '        "work_id",\n'
            + '        "worker_id",\n'
            + '        name="uq_work_attempts_lineage",\n'
            + "    ),\n",
            1,
        )
    write(relative, text)

    write(
        "database/migrations/versions/20260812_0007_artifact_completion.py",
        '''"""Add atomic artifact-completion lineage constraints.

Revision ID: 20260812_0007
Revises: 20260812_0006
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260812_0007"
down_revision: str | None = "20260812_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE sources.raw_artifacts ADD COLUMN artifact_kind TEXT")
    op.execute(
        """
        UPDATE sources.raw_artifacts AS artifact
        SET artifact_kind = upload.artifact_kind
        FROM sources.artifact_uploads AS upload
        WHERE upload.upload_id = artifact.upload_id
        """
    )
    op.execute("ALTER TABLE sources.raw_artifacts ALTER COLUMN artifact_kind SET NOT NULL")
    op.execute(
        """
        ALTER TABLE sources.raw_artifacts
        ADD CONSTRAINT ck_raw_artifacts_kind
        CHECK (artifact_kind IN ('raw_artifact', 'diagnostic_artifact'))
        """
    )
    op.execute(
        """
        ALTER TABLE sources.artifact_uploads
        ADD CONSTRAINT uq_artifact_uploads_lineage
        UNIQUE (upload_id, work_id, worker_id)
        """
    )
    op.execute(
        """
        ALTER TABLE work.work_attempts
        ADD CONSTRAINT uq_work_attempts_lineage
        UNIQUE (attempt_id, work_id, worker_id)
        """
    )
    op.execute(
        """
        ALTER TABLE sources.raw_artifacts
        ADD CONSTRAINT fk_raw_artifacts_upload_lineage
        FOREIGN KEY (upload_id, work_id, worker_id)
        REFERENCES sources.artifact_uploads (upload_id, work_id, worker_id)
        """
    )
    op.execute(
        """
        ALTER TABLE sources.raw_artifacts
        ADD CONSTRAINT fk_raw_artifacts_attempt_lineage
        FOREIGN KEY (attempt_id, work_id, worker_id)
        REFERENCES work.work_attempts (attempt_id, work_id, worker_id)
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE sources.raw_artifacts "
        "DROP CONSTRAINT fk_raw_artifacts_attempt_lineage"
    )
    op.execute(
        "ALTER TABLE sources.raw_artifacts "
        "DROP CONSTRAINT fk_raw_artifacts_upload_lineage"
    )
    op.execute("ALTER TABLE work.work_attempts DROP CONSTRAINT uq_work_attempts_lineage")
    op.execute(
        "ALTER TABLE sources.artifact_uploads DROP CONSTRAINT uq_artifact_uploads_lineage"
    )
    op.execute("ALTER TABLE sources.raw_artifacts DROP CONSTRAINT ck_raw_artifacts_kind")
    op.execute("ALTER TABLE sources.raw_artifacts DROP COLUMN artifact_kind")
''',
    )


def patch_postgres_work_engine() -> None:
    relative = (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/work_engine.py"
    )
    text = read(relative)
    for name in ("WorkInputArtifact", "WorkOutputArtifact"):
        if f"    {name},\n" not in text:
            anchor = "    WorkFailureKind,\n"
            if anchor not in text:
                raise RuntimeError("PostgresWorkEngine application import anchor is missing")
            text = text.replace(anchor, anchor + f"    {name},\n", 1)
    if "from collection_infrastructure.postgres.artifact_metadata import" not in text:
        anchor = "from collection_infrastructure.postgres.metadata import (\n"
        position = text.find(anchor)
        if position < 0:
            raise RuntimeError("PostgresWorkEngine metadata import anchor is missing")
        text = (
            text[:position]
            + "from collection_infrastructure.postgres.artifact_metadata import (\n"
            + "    artifact_objects,\n"
            + "    artifact_uploads,\n"
            + "    raw_artifacts,\n"
            + "    work_input_artifacts,\n"
            + "    work_output_artifacts,\n"
            + ")\n"
            + text[position:]
        )
    old = (
        "if len(existing_rows) == 1 and _same_work_identity(existing_rows[0], command):\n"
        "                return"
    )
    new = '''if (
                len(existing_rows) == 1
                and _same_work_identity(existing_rows[0], command)
                and self._input_artifacts_match(
                    connection,
                    command.work_id,
                    command.input_artifacts,
                )
            ):
                return'''
    if old not in text:
        raise RuntimeError("work semantic replay condition is not current")
    text = text.replace(old, new, 1)

    acquire_boundary = "\n    def _acquire_lease(\n"
    boundary = text.find(acquire_boundary)
    if boundary < 0:
        raise RuntimeError("lease acquisition boundary is missing")
    enqueue = text[:boundary]
    if "self._insert_input_artifacts(" not in enqueue[-4000:]:
        position = enqueue.rfind("        )\n") + len("        )\n")
        enqueue = (
            enqueue[:position]
            + "        self._insert_input_artifacts(\n"
            + "            connection,\n"
            + "            command.work_id,\n"
            + "            command.input_artifacts,\n"
            + "        )\n"
            + enqueue[position:]
        )
    text = enqueue + text[boundary:]

    input_helpers = '''
    def _insert_input_artifacts(
        self,
        connection: Connection,
        work_id: UUID,
        artifacts: tuple[WorkInputArtifact, ...],
    ) -> None:
        if not artifacts:
            return
        artifact_ids = tuple(item.artifact_id for item in artifacts)
        existing = frozenset(
            UUID(str(value))
            for value in connection.execute(
                sa.select(raw_artifacts.c.artifact_id).where(
                    raw_artifacts.c.artifact_id.in_(artifact_ids)
                )
            ).scalars()
        )
        missing = sorted(str(value) for value in set(artifact_ids) - existing)
        if missing:
            raise _conflict(
                code="WORK_INPUT_ARTIFACT_NOT_FOUND",
                message="One or more declared work input artifacts do not exist.",
                context={"workId": str(work_id), "missingArtifactIds": missing},
                required_action="Schedule work only after every exact input artifact is recorded.",
            )
        connection.execute(
            sa.insert(work_input_artifacts),
            [
                {
                    "work_id": work_id,
                    "position": position,
                    "artifact_id": artifact.artifact_id,
                    "role": artifact.role,
                }
                for position, artifact in enumerate(artifacts)
            ],
        )

    def _load_input_artifacts(
        self,
        connection: Connection,
        work_id: UUID,
    ) -> tuple[WorkInputArtifact, ...]:
        rows = connection.execute(
            sa.select(
                work_input_artifacts.c.artifact_id,
                work_input_artifacts.c.role,
            )
            .where(work_input_artifacts.c.work_id == work_id)
            .order_by(work_input_artifacts.c.position)
        ).mappings()
        return tuple(
            WorkInputArtifact(
                artifact_id=UUID(str(row["artifact_id"])),
                role=str(row["role"]),
            )
            for row in rows
        )

    def _input_artifacts_match(
        self,
        connection: Connection,
        work_id: UUID,
        expected: tuple[WorkInputArtifact, ...],
    ) -> bool:
        return self._load_input_artifacts(connection, work_id) == expected

'''
    if "    def _insert_input_artifacts(" not in text:
        text = text.replace(acquire_boundary, input_helpers + acquire_boundary, 1)

    text = text.replace(
        "_lease_from_work(updated_work, permit, command.correlation_id)",
        "_lease_from_work(\n"
        "            updated_work,\n"
        "            permit,\n"
        "            command.correlation_id,\n"
        "            self._load_input_artifacts(connection, command.work_id),\n"
        "        )",
    )
    text = text.replace(
        'self._load_input_artifacts(connection, command.work_id),\n'
        "        )\n\n    def _heartbeat",
        'self._load_input_artifacts(connection, UUID(str(updated_work["work_id"]))),\n'
        "        )\n\n    def _heartbeat",
        1,
    )

    replay_anchor = 'and work["output_digest"] == command.output_digest\n            ):'
    if replay_anchor not in text:
        raise RuntimeError("completion replay anchor is missing")
    text = text.replace(
        replay_anchor,
        'and work["output_digest"] == command.output_digest\n'
        "                and self._completion_artifacts_match(\n"
        "                    connection, command.work_id, command.output_artifacts\n"
        "                )\n"
        "            ):",
        1,
    )
    update_anchor = '''        connection.execute(
            sa.update(work_attempts)
            .where(work_attempts.c.attempt_id == attempt["attempt_id"])
            .values(
                finished_at_utc=now_utc,
                outcome=WorkAttemptOutcome.SUCCEEDED.value,
'''
    if update_anchor not in text:
        raise RuntimeError("successful attempt update anchor is missing")
    text = text.replace(
        update_anchor,
        "        self._materialize_output_artifacts(\n"
        "            connection,\n"
        "            now_utc,\n"
        "            work,\n"
        "            attempt,\n"
        "            command,\n"
        "        )\n"
        + update_anchor,
        1,
    )

    output_helpers = '''
    def _completion_artifacts_match(
        self,
        connection: Connection,
        work_id: UUID,
        expected: tuple[WorkOutputArtifact, ...],
    ) -> bool:
        rows = connection.execute(
            sa.select(raw_artifacts.c.upload_id, work_output_artifacts.c.role)
            .select_from(
                work_output_artifacts.join(
                    raw_artifacts,
                    raw_artifacts.c.artifact_id == work_output_artifacts.c.artifact_id,
                )
            )
            .where(work_output_artifacts.c.work_id == work_id)
            .order_by(work_output_artifacts.c.position)
        ).mappings()
        actual = tuple(
            WorkOutputArtifact(
                upload_id=UUID(str(row["upload_id"])),
                role=str(row["role"]),
            )
            for row in rows
        )
        return actual == expected

    def _materialize_output_artifacts(
        self,
        connection: Connection,
        now_utc: datetime,
        work: RowMapping,
        attempt: RowMapping,
        command: WorkCompletion,
    ) -> None:
        if not command.output_artifacts:
            return
        upload_ids = tuple(item.upload_id for item in command.output_artifacts)
        rows = (
            connection.execute(
                sa.select(artifact_uploads)
                .where(artifact_uploads.c.upload_id.in_(upload_ids))
                .order_by(artifact_uploads.c.upload_id)
                .with_for_update()
            )
            .mappings()
            .all()
        )
        by_id = {UUID(str(row["upload_id"])): row for row in rows}
        missing = sorted(str(value) for value in set(upload_ids) - set(by_id))
        if missing:
            raise _conflict(
                code="ARTIFACT_UPLOAD_NOT_VERIFIED",
                message="A completion references an unknown artifact upload.",
                context={"workId": str(command.work_id), "missingUploadIds": missing},
                required_action="Prepare, upload, and verify every exact output before completion.",
            )
        for position, binding in enumerate(command.output_artifacts):
            upload = by_id[binding.upload_id]
            self._require_completion_upload_identity(upload, command)
            if upload["state"] != "verified":
                raise _conflict(
                    code="ARTIFACT_UPLOAD_NOT_VERIFIED",
                    message="A completion output has not passed artifact verification.",
                    context={
                        "workId": str(command.work_id),
                        "uploadId": str(binding.upload_id),
                        "state": upload["state"],
                    },
                    required_action="Verify the exact upload before completing work.",
                )
            digest = str(upload["expected_digest"])
            final_reference = upload["final_reference"]
            verified_at_utc = upload["verified_at_utc"]
            if not isinstance(final_reference, str) or not isinstance(
                verified_at_utc, datetime
            ):
                raise _state_conflict(
                    code="ARTIFACT_VERIFICATION_STATE_INVALID",
                    message="A verified upload has incomplete object identity.",
                    context={"uploadId": str(binding.upload_id)},
                )
            _advisory_lock(connection, f"artifact-object:{digest}")
            object_row = (
                connection.execute(
                    sa.select(artifact_objects)
                    .where(artifact_objects.c.content_digest == digest)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if object_row is None:
                object_id = self._uuid_factory()
                connection.execute(
                    sa.insert(artifact_objects).values(
                        object_id=object_id,
                        content_digest=digest,
                        size_bytes=upload["expected_size_bytes"],
                        storage_reference=final_reference,
                        verified_at_utc=verified_at_utc,
                        recorded_at_utc=now_utc,
                        correlation_id=command.correlation_id,
                    )
                )
            else:
                if (
                    object_row["size_bytes"] != upload["expected_size_bytes"]
                    or object_row["storage_reference"] != final_reference
                ):
                    raise _conflict(
                        code="ARTIFACT_OBJECT_IDENTITY_CONFLICT",
                        message="The content digest is already bound to different object metadata.",
                        context={"contentDigest": digest},
                        required_action="Use the canonical object identity or repair storage evidence.",
                    )
                object_id = UUID(str(object_row["object_id"]))
            artifact_id = self._uuid_factory()
            connection.execute(
                sa.insert(raw_artifacts).values(
                    artifact_id=artifact_id,
                    object_id=object_id,
                    upload_id=binding.upload_id,
                    work_id=command.work_id,
                    attempt_id=attempt["attempt_id"],
                    worker_id=command.worker_id,
                    artifact_kind=upload["artifact_kind"],
                    content_type=upload["content_type"],
                    source_policy_digest=work["source_policy_digest"],
                    recorded_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
            )
            connection.execute(
                sa.insert(work_output_artifacts).values(
                    work_id=command.work_id,
                    position=position,
                    artifact_id=artifact_id,
                    role=binding.role,
                )
            )
            connection.execute(
                sa.update(artifact_uploads)
                .where(artifact_uploads.c.upload_id == binding.upload_id)
                .values(
                    state="consumed",
                    consumed_at_utc=now_utc,
                    revision=artifact_uploads.c.revision + 1,
                    correlation_id=command.correlation_id,
                )
            )

    @staticmethod
    def _require_completion_upload_identity(
        upload: RowMapping,
        command: WorkCompletion,
    ) -> None:
        checks = (
            (upload["work_id"] == command.work_id, "work_id_mismatch"),
            (upload["lease_id"] == command.lease_id, "lease_id_mismatch"),
            (upload["lease_token"] == command.lease_token, "lease_token_mismatch"),
            (upload["worker_id"] == command.worker_id, "worker_id_mismatch"),
            (upload["input_digest"] == command.input_digest, "input_digest_mismatch"),
        )
        for valid, reason in checks:
            if not valid:
                raise _conflict(
                    code="ARTIFACT_COMPLETION_IDENTITY_MISMATCH",
                    message="A completion output belongs to another work lease.",
                    context={"uploadId": str(upload["upload_id"]), "reason": reason},
                    required_action="Complete work only with uploads verified under the exact lease.",
                )

'''
    fail_boundary = "\n    def _fail(\n"
    if "    def _materialize_output_artifacts(" not in text:
        if fail_boundary not in text:
            raise RuntimeError("work failure boundary is missing")
        text = text.replace(fail_boundary, output_helpers + fail_boundary, 1)

    match = re.search(r"def _lease_from_work\(.*?(?=\n\ndef )", text, re.S)
    if match is None:
        raise RuntimeError("lease projection helper is missing")
    block = match.group(0)
    if "input_artifacts:" not in block:
        block = block.replace(
            "    correlation_id: str,\n",
            "    correlation_id: str,\n"
            "    input_artifacts: tuple[WorkInputArtifact, ...],\n",
            1,
        )
        block = block.replace(
            "        correlation_id=correlation_id,\n",
            "        correlation_id=correlation_id,\n"
            "        input_artifacts=input_artifacts,\n",
            1,
        )
    write(relative, text[: match.start()] + block + text[match.end() :])


def patch_s3_readiness() -> None:
    relative = (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "object_store/s3.py"
    )
    text = read(relative)
    if "def head_bucket(" not in text:
        text = text.replace(
            "    def head_object(self, **kwargs: object) -> Mapping[str, object]: ...\n",
            "    def head_object(self, **kwargs: object) -> Mapping[str, object]: ...\n\n"
            "    def head_bucket(self, **kwargs: object) -> Mapping[str, object]: ...\n",
            1,
        )
    if "    def check_ready(self) -> None:" not in text:
        anchor = "    def prepare_upload(\n"
        if anchor not in text:
            raise RuntimeError("S3 upload method anchor is missing")
        method = '''    def check_ready(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception as exc:
            code, _status = _storage_error(exc)
            raise ArtifactObjectStoreError(
                code="ARTIFACT_STORE_UNAVAILABLE",
                message="The artifact object-store bucket is unavailable.",
                context={"bucket": self._bucket, "storageCode": code},
                required_action="Restore the configured artifact bucket before issuing work.",
            ) from exc

'''
        text = text.replace(anchor, method + anchor, 1)
    write(relative, text)


def add_gateway_artifact_contracts() -> None:
    write(
        "apps/worker_gateway/src/worker_gateway/artifact_contracts.py",
        '''from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from collection_application import (
    ArtifactKind,
    PreparedArtifactRead,
    PreparedArtifactUpload,
    VerifiedArtifactUpload,
)

Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
ContentType = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$"
    ),
]


class ArtifactTransportModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )


class ArtifactUploadPrepareRequest(ArtifactTransportModel):
    work_id: UUID = Field(alias="workId")
    lease_id: UUID = Field(alias="leaseId")
    lease_token: UUID = Field(alias="leaseToken")
    input_digest: Digest = Field(alias="inputDigest")
    artifact_kind: ArtifactKind = Field(alias="artifactKind")
    expected_digest: Digest = Field(alias="expectedDigest")
    expected_size_bytes: int = Field(alias="expectedSizeBytes", ge=1, le=5_368_709_120)
    content_type: ContentType = Field(alias="contentType")
    expires_in_seconds: int = Field(alias="expiresInSeconds", ge=60, le=3_600)


class ArtifactUploadVerifyRequest(ArtifactTransportModel):
    work_id: UUID = Field(alias="workId")
    lease_id: UUID = Field(alias="leaseId")
    lease_token: UUID = Field(alias="leaseToken")
    input_digest: Digest = Field(alias="inputDigest")


class ArtifactReadPrepareRequest(ArtifactTransportModel):
    work_id: UUID = Field(alias="workId")
    lease_id: UUID = Field(alias="leaseId")
    lease_token: UUID = Field(alias="leaseToken")
    input_digest: Digest = Field(alias="inputDigest")
    expires_in_seconds: int = Field(alias="expiresInSeconds", ge=60, le=3_600)


class PreparedArtifactUploadResponse(ArtifactTransportModel):
    upload_id: UUID = Field(alias="uploadId")
    method: str
    url: str
    required_headers: dict[str, str] = Field(alias="requiredHeaders")
    expires_at_utc: datetime = Field(alias="expiresAtUtc")

    @classmethod
    def from_result(cls, result: PreparedArtifactUpload) -> PreparedArtifactUploadResponse:
        return cls(
            upload_id=result.upload_id,
            method=result.method,
            url=result.url,
            required_headers=dict(result.required_headers),
            expires_at_utc=result.expires_at_utc,
        )


class VerifiedArtifactUploadResponse(ArtifactTransportModel):
    upload_id: UUID = Field(alias="uploadId")
    work_id: UUID = Field(alias="workId")
    artifact_kind: ArtifactKind = Field(alias="artifactKind")
    content_digest: Digest = Field(alias="contentDigest")
    size_bytes: int = Field(alias="sizeBytes")
    content_type: ContentType = Field(alias="contentType")
    storage_reference: str = Field(alias="storageReference")
    verified_at_utc: datetime = Field(alias="verifiedAtUtc")

    @classmethod
    def from_result(cls, result: VerifiedArtifactUpload) -> VerifiedArtifactUploadResponse:
        return cls(
            upload_id=result.upload_id,
            work_id=result.work_id,
            artifact_kind=result.artifact_kind,
            content_digest=result.content_digest,
            size_bytes=result.size_bytes,
            content_type=result.content_type,
            storage_reference=result.storage_reference,
            verified_at_utc=result.verified_at_utc,
        )


class PreparedArtifactReadResponse(ArtifactTransportModel):
    artifact_id: UUID = Field(alias="artifactId")
    method: str
    url: str
    expires_at_utc: datetime = Field(alias="expiresAtUtc")

    @classmethod
    def from_result(cls, result: PreparedArtifactRead) -> PreparedArtifactReadResponse:
        return cls(
            artifact_id=result.artifact_id,
            method=result.method,
            url=result.url,
            expires_at_utc=result.expires_at_utc,
        )
''',
    )


def patch_gateway_transport() -> None:
    relative = "apps/worker_gateway/src/worker_gateway/contracts.py"
    text = read(relative)
    if "WorkOutputArtifactRequest" not in text:
        match = re.search(r"class WorkCompletionRequest\(([^)]+)\):", text)
        if match is None:
            raise RuntimeError("WorkCompletionRequest base is missing")
        base = match.group(1)
        definition = (
            f"class WorkOutputArtifactRequest({base}):\n"
            '    upload_id: UUID = Field(alias="uploadId")\n'
            "    role: str\n\n\n"
        )
        text = text[: match.start()] + definition + text[match.start() :]
        match = re.search(
            r"(class WorkCompletionRequest\([^)]*\):.*?)(?=\n\nclass )",
            text,
            re.S,
        )
        if match is None:
            raise RuntimeError("WorkCompletionRequest block is missing")
        block = match.group(1).replace(
            "    worker_build_identity:",
            "    output_artifacts: tuple[WorkOutputArtifactRequest, ...] = Field(\n"
            '        default=(), alias="outputArtifacts"\n'
            "    )\n"
            "    worker_build_identity:",
            1,
        )
        text = text[: match.start()] + block + text[match.end() :]
    if "WorkInputArtifactResponse" not in text:
        match = re.search(r"class WorkLeaseResponse\(([^)]+)\):", text)
        if match is None:
            raise RuntimeError("WorkLeaseResponse base is missing")
        base = match.group(1)
        definition = (
            f"class WorkInputArtifactResponse({base}):\n"
            '    artifact_id: UUID = Field(alias="artifactId")\n'
            "    role: str\n\n\n"
        )
        text = text[: match.start()] + definition + text[match.start() :]
        match = re.search(
            r"(class WorkLeaseResponse\([^)]*\):.*?)(?=\n\nclass )",
            text,
            re.S,
        )
        if match is None:
            raise RuntimeError("WorkLeaseResponse block is missing")
        block = match.group(1)
        if "    correlation_id:" not in block:
            raise RuntimeError("WorkLeaseResponse field anchor is missing")
        block = block.replace(
            "    correlation_id:",
            "    input_artifacts: tuple[WorkInputArtifactResponse, ...] = Field(\n"
            '        alias="inputArtifacts"\n'
            "    )\n"
            "    correlation_id:",
            1,
        )
        anchor = "            correlation_id=lease.correlation_id,\n"
        if anchor not in block:
            raise RuntimeError("WorkLeaseResponse projection anchor is missing")
        block = block.replace(
            anchor,
            "            input_artifacts=tuple(\n"
            "                WorkInputArtifactResponse(\n"
            "                    artifact_id=item.artifact_id,\n"
            "                    role=item.role,\n"
            "                )\n"
            "                for item in lease.input_artifacts\n"
            "            ),\n"
            + anchor,
            1,
        )
        text = text[: match.start()] + block + text[match.end() :]
    write(relative, text)

    relative = "apps/worker_gateway/src/worker_gateway/app.py"
    text = read(relative)
    for name in (
        "ArtifactTransferService",
        "PrepareArtifactRead",
        "PrepareArtifactUpload",
        "VerifyArtifactUpload",
        "WorkOutputArtifact",
    ):
        if f"    {name},\n" not in text:
            text = text.replace(
                "from collection_application import (\n",
                "from collection_application import (\n" + f"    {name},\n",
                1,
            )
    if "from worker_gateway.artifact_contracts import" not in text:
        anchor = "from worker_gateway.auth import (\n"
        position = text.find(anchor)
        if position < 0:
            raise RuntimeError("gateway auth import anchor is missing")
        text = (
            text[:position]
            + "from worker_gateway.artifact_contracts import (\n"
            + "    ArtifactReadPrepareRequest,\n"
            + "    ArtifactUploadPrepareRequest,\n"
            + "    ArtifactUploadVerifyRequest,\n"
            + "    PreparedArtifactReadResponse,\n"
            + "    PreparedArtifactUploadResponse,\n"
            + "    VerifiedArtifactUploadResponse,\n"
            + ")\n"
            + text[position:]
        )
    if "artifact_transfer: ArtifactTransferService | None" not in text:
        text = text.replace(
            "    readiness_probe: Callable[[], None]\n",
            "    readiness_probe: Callable[[], None]\n"
            "    artifact_transfer: ArtifactTransferService | None = None\n",
            1,
        )
    completion_start = text.find("    def complete_work(")
    completion_end = text.find("    def fail_work(", completion_start)
    completion = text[completion_start:completion_end]
    if "output_artifacts=tuple(" not in completion:
        anchor = "                output_digest=payload.output_digest,\n"
        if anchor not in text:
            raise RuntimeError("gateway completion mapping anchor is missing")
        text = text.replace(
            anchor,
            anchor
            + "                output_artifacts=tuple(\n"
            + "                    WorkOutputArtifact(\n"
            + "                        upload_id=item.upload_id,\n"
            + "                        role=item.role,\n"
            + "                    )\n"
            + "                    for item in payload.output_artifacts\n"
            + "                ),\n",
            1,
        )
    route_anchor = '    @router.get(\n        "/capabilities",'
    if route_anchor not in text:
        raise RuntimeError("gateway capabilities route anchor is missing")
    if 'operation_id="prepareArtifactUpload"' not in text:
        routes = '''    @router.post(
        "/artifacts/uploads/{upload_id}/prepare",
        response_model=PreparedArtifactUploadResponse,
        operation_id="prepareArtifactUpload",
    )
    def prepare_artifact_upload(
        upload_id: UUID,
        payload: ArtifactUploadPrepareRequest,
        request: Request,
        principal: Annotated[WorkerPrincipal, Depends(_authenticate_worker)],
    ) -> PreparedArtifactUploadResponse:
        command = _command(
            lambda: PrepareArtifactUpload(
                upload_id=upload_id,
                work_id=payload.work_id,
                lease_id=payload.lease_id,
                lease_token=payload.lease_token,
                worker_id=principal.worker_id,
                input_digest=payload.input_digest,
                artifact_kind=payload.artifact_kind,
                expected_digest=payload.expected_digest,
                expected_size_bytes=payload.expected_size_bytes,
                content_type=payload.content_type,
                expires_in_seconds=payload.expires_in_seconds,
                correlation_id=_correlation_id(request),
            )
        )
        return PreparedArtifactUploadResponse.from_result(
            _artifact_transfer(request).prepare_upload(command)
        )

    @router.post(
        "/artifacts/uploads/{upload_id}/verify",
        response_model=VerifiedArtifactUploadResponse,
        operation_id="verifyArtifactUpload",
    )
    def verify_artifact_upload(
        upload_id: UUID,
        payload: ArtifactUploadVerifyRequest,
        request: Request,
        principal: Annotated[WorkerPrincipal, Depends(_authenticate_worker)],
    ) -> VerifiedArtifactUploadResponse:
        command = _command(
            lambda: VerifyArtifactUpload(
                upload_id=upload_id,
                work_id=payload.work_id,
                lease_id=payload.lease_id,
                lease_token=payload.lease_token,
                worker_id=principal.worker_id,
                input_digest=payload.input_digest,
                correlation_id=_correlation_id(request),
            )
        )
        return VerifiedArtifactUploadResponse.from_result(
            _artifact_transfer(request).verify_upload(command)
        )

    @router.post(
        "/artifacts/{artifact_id}/reads/prepare",
        response_model=PreparedArtifactReadResponse,
        operation_id="prepareArtifactRead",
    )
    def prepare_artifact_read(
        artifact_id: UUID,
        payload: ArtifactReadPrepareRequest,
        request: Request,
        principal: Annotated[WorkerPrincipal, Depends(_authenticate_worker)],
    ) -> PreparedArtifactReadResponse:
        command = _command(
            lambda: PrepareArtifactRead(
                artifact_id=artifact_id,
                work_id=payload.work_id,
                lease_id=payload.lease_id,
                lease_token=payload.lease_token,
                worker_id=principal.worker_id,
                input_digest=payload.input_digest,
                expires_in_seconds=payload.expires_in_seconds,
                correlation_id=_correlation_id(request),
            )
        )
        return PreparedArtifactReadResponse.from_result(
            _artifact_transfer(request).prepare_read(command)
        )

'''
        text = text.replace(route_anchor, routes + route_anchor, 1)
    if "def _artifact_transfer(" not in text:
        anchor = "\n\ndef _correlation_id("
        helper = '''

def _artifact_transfer(request: Request) -> ArtifactTransferService:
    service = _dependencies(request).artifact_transfer
    if service is None:
        raise GatewayReadinessError(
            code="WORKER_GATEWAY_ARTIFACTS_NOT_CONFIGURED",
            message="Worker Gateway artifact transfer is not configured.",
            context={},
        )
    return service
'''
        if anchor not in text:
            raise RuntimeError("gateway correlation helper anchor is missing")
        text = text.replace(anchor, helper + anchor, 1)
    write(relative, text)


def patch_runtime_composition() -> None:
    relative = "apps/worker_gateway/src/worker_gateway/__main__.py"
    text = read(relative)
    text = text.replace(
        "from collection_application import WorkEngineService\n",
        "from collection_application import ArtifactTransferService, WorkEngineService\n",
        1,
    )
    text = text.replace(
        "from collection_infrastructure import PostgresWorkEngine\n",
        "from collection_infrastructure import PostgresWorkEngine\n"
        "from collection_infrastructure.object_store import S3ArtifactObjectStore\n"
        "from collection_infrastructure.postgres import PostgresArtifactTransfer\n",
        1,
    )
    if "ARTIFACT_S3_ENDPOINT_URL" not in text:
        anchor = "    authenticator = WorkerAuthenticator.from_secret_file(credential_file)\n"
        if anchor not in text:
            raise RuntimeError("gateway runtime authentication anchor is missing")
        text = text.replace(
            anchor,
            anchor
            + "    object_store = S3ArtifactObjectStore.create(\n"
            + '        endpoint_url=_required_environment("ARTIFACT_S3_ENDPOINT_URL"),\n'
            + '        bucket=_required_environment("ARTIFACT_S3_BUCKET"),\n'
            + '        access_key_id=_required_environment("ARTIFACT_S3_ACCESS_KEY_ID"),\n'
            + '        secret_access_key=_required_environment("ARTIFACT_S3_SECRET_ACCESS_KEY"),\n'
            + '        region_name=_required_environment("ARTIFACT_S3_REGION"),\n'
            + "    )\n",
            1,
        )
        text = text.replace(
            "    work_engine = WorkEngineService(PostgresWorkEngine(engine))\n",
            "    work_engine = WorkEngineService(PostgresWorkEngine(engine))\n"
            "    artifact_transfer = ArtifactTransferService(\n"
            "        PostgresArtifactTransfer(engine, object_store)\n"
            "    )\n",
            1,
        )
        text = text.replace(
            '            if connection.execute(sa.text("SELECT 1")).scalar_one() != 1:\n'
            '                raise RuntimeError("PostgreSQL readiness probe returned an unexpected result")\n',
            '            if connection.execute(sa.text("SELECT 1")).scalar_one() != 1:\n'
            '                raise RuntimeError("PostgreSQL readiness probe returned an unexpected result")\n'
            "        object_store.check_ready()\n",
            1,
        )
        text = text.replace(
            "            readiness_probe=readiness_probe,\n",
            "            readiness_probe=readiness_probe,\n"
            "            artifact_transfer=artifact_transfer,\n",
            1,
        )
    write(relative, text)

    relative = "apps/worker_gateway/tests/test_main.py"
    text = read(relative)
    if "ARTIFACT_S3_ENDPOINT_URL" not in text:
        matches = list(
            re.finditer(
                r'monkeypatch\.setenv\("COLLECTOR_DATABASE_URL",[^\n]+\)\n',
                text,
            )
        )
        for match in reversed(matches):
            settings = '''    monkeypatch.setenv("ARTIFACT_S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("ARTIFACT_S3_BUCKET", "collector-artifacts")
    monkeypatch.setenv("ARTIFACT_S3_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("ARTIFACT_S3_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("ARTIFACT_S3_REGION", "us-east-1")
'''
            text = text[: match.end()] + settings + text[match.end() :]
    write(relative, text)


def patch_contract_generation() -> None:
    relative = "tools/contract_generation/generate.py"
    text = read(relative)
    if "prepareArtifactUpload" not in text:
        position = text.find('"registerWorker"')
        if position < 0:
            raise RuntimeError("OpenAPI operation inventory anchor is missing")
        line_start = text.rfind("\n", 0, position) + 1
        indentation = text[line_start:position]
        text = (
            text[:line_start]
            + indentation
            + '"prepareArtifactRead",\n'
            + indentation
            + '"prepareArtifactUpload",\n'
            + indentation
            + '"verifyArtifactUpload",\n'
            + text[line_start:]
        )
    write(relative, text)


def patch_documentation() -> None:
    relative = ".codex/modules/work-engine.md"
    text = read(relative)
    old = '''The application-owned artifact transfer port, S3-compatible content-addressed adapter, immutable
upload/object/raw-artifact metadata, and exact work input/output binding schema are implemented. The
Worker Gateway routes, runtime object-store composition, and atomic verified-artifact-plus-completion
transaction remain pending. No worker may receive PostgreSQL credentials or bypass application
services; the remaining artifact routes must preserve this boundary.
'''
    new = '''The application-owned artifact transfer port, S3-compatible content-addressed adapter, immutable
upload/object/raw-artifact metadata, exact work input/output bindings, authenticated Worker Gateway
routes, runtime object-store composition, scoped reads, and atomic verified-artifact-plus-completion
transaction are implemented. Workers receive only pre-signed object operations and never PostgreSQL
or object-store credentials. Orphan cleanup and retention remain a separate future owner batch.
'''
    if old not in text:
        raise RuntimeError("work-engine artifact boundary text is not current")
    write(relative, text.replace(old, new, 1))

    relative = "docs/implementation-status.md"
    text = read(relative).replace("- no raw artifact/object-store owner;\n", "")
    if "| Artifact transfer |" not in text:
        anchor = "| CI proof |"
        position = text.find(anchor)
        if position < 0:
            raise RuntimeError("implementation ledger table anchor is missing")
        line_start = text.rfind("\n", 0, position) + 1
        text = (
            text[:line_start]
            + "| Artifact transfer | Lease-scoped pre-signed upload/read, streamed verification, immutable lineage, and atomic completion |\n"
            + text[line_start:]
        )
    write(relative, text)


def main() -> None:
    patch_domain_contract()
    patch_application_commands()
    patch_database_contract()
    patch_postgres_work_engine()
    patch_s3_readiness()
    add_gateway_artifact_contracts()
    patch_gateway_transport()
    patch_runtime_composition()
    patch_contract_generation()
    patch_documentation()


if __name__ == "__main__":
    main()
