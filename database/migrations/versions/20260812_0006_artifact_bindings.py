"""Complete artifact identity and work input/output bindings.

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
        ALTER TABLE sources.artifact_objects
        ADD COLUMN artifact_kind TEXT
        """
    )
    op.execute(
        """
        UPDATE sources.artifact_objects AS object
        SET artifact_kind = upload.artifact_kind
        FROM sources.raw_artifacts AS artifact
        JOIN sources.artifact_uploads AS upload
          ON upload.upload_id = artifact.upload_id
        WHERE artifact.object_id = object.object_id
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM sources.artifact_objects
                WHERE artifact_kind IS NULL
            ) THEN
                RAISE EXCEPTION
                    'artifact object kind cannot be recovered from its verified upload';
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE sources.artifact_objects
        ALTER COLUMN artifact_kind SET NOT NULL,
        DROP CONSTRAINT uq_artifact_objects_content_digest,
        ADD CONSTRAINT ck_artifact_objects_kind
            CHECK (artifact_kind IN ('raw_artifact', 'diagnostic_artifact')),
        ADD CONSTRAINT uq_artifact_objects_kind_content_digest
            UNIQUE (artifact_kind, content_digest)
        """
    )

    op.execute("ALTER TABLE sources.raw_artifacts RENAME TO artifact_records")
    _rename_artifact_record_constraints(to_records=True)
    op.execute(
        """
        ALTER TABLE work.work_output_artifacts
        RENAME CONSTRAINT fk_work_output_artifacts_artifact_id_raw_artifacts
        TO fk_work_output_artifacts_artifact_id_artifact_records
        """
    )

    op.execute(
        """
        ALTER TABLE work.work_output_artifacts
        ADD COLUMN role TEXT
        """
    )
    op.execute(
        """
        UPDATE work.work_output_artifacts
        SET role = 'output_' || position::TEXT
        """
    )
    op.execute(
        """
        ALTER TABLE work.work_output_artifacts
        ALTER COLUMN role SET NOT NULL,
        ADD CONSTRAINT ck_work_output_artifacts_role
            CHECK (role ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$'),
        ADD CONSTRAINT uq_work_output_artifacts_work_role
            UNIQUE (work_id, role)
        """
    )

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
            CONSTRAINT fk_work_input_artifacts_artifact_id_artifact_records
                FOREIGN KEY(artifact_id) REFERENCES sources.artifact_records (artifact_id),
            CONSTRAINT uq_work_input_artifacts_work_artifact
                UNIQUE (work_id, artifact_id),
            CONSTRAINT uq_work_input_artifacts_work_role
                UNIQUE (work_id, role),
            CONSTRAINT ck_work_input_artifacts_position
                CHECK (position BETWEEN 0 AND 31),
            CONSTRAINT ck_work_input_artifacts_role
                CHECK (role ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$')
        )
        """
    )

    op.execute("DROP INDEX sources.ix_artifact_uploads_orphan_candidates")
    op.execute(
        """
        CREATE INDEX ix_artifact_uploads_orphan_candidates
        ON sources.artifact_uploads (state, expires_at_utc)
        WHERE state IN ('prepared', 'verified', 'consumed')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX sources.ix_artifact_uploads_orphan_candidates")
    op.execute(
        """
        CREATE INDEX ix_artifact_uploads_orphan_candidates
        ON sources.artifact_uploads (state, expires_at_utc)
        WHERE state IN ('prepared', 'verified')
        """
    )

    op.execute("DROP TABLE work.work_input_artifacts")
    op.execute(
        """
        ALTER TABLE work.work_output_artifacts
        DROP CONSTRAINT uq_work_output_artifacts_work_role,
        DROP CONSTRAINT ck_work_output_artifacts_role,
        DROP COLUMN role
        """
    )
    op.execute(
        """
        ALTER TABLE work.work_output_artifacts
        RENAME CONSTRAINT fk_work_output_artifacts_artifact_id_artifact_records
        TO fk_work_output_artifacts_artifact_id_raw_artifacts
        """
    )

    _rename_artifact_record_constraints(to_records=False)
    op.execute("ALTER TABLE sources.artifact_records RENAME TO raw_artifacts")

    op.execute(
        """
        ALTER TABLE sources.artifact_objects
        DROP CONSTRAINT uq_artifact_objects_kind_content_digest,
        DROP CONSTRAINT ck_artifact_objects_kind,
        DROP COLUMN artifact_kind,
        ADD CONSTRAINT uq_artifact_objects_content_digest UNIQUE (content_digest)
        """
    )


def _rename_artifact_record_constraints(*, to_records: bool) -> None:
    forward_statements = (
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT pk_raw_artifacts TO pk_artifact_records",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT fk_raw_artifacts_object_id_artifact_objects "
        "TO fk_artifact_records_object_id_artifact_objects",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT fk_raw_artifacts_upload_id_artifact_uploads "
        "TO fk_artifact_records_upload_id_artifact_uploads",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT fk_raw_artifacts_work_id_work_units "
        "TO fk_artifact_records_work_id_work_units",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT fk_raw_artifacts_attempt_id_work_attempts "
        "TO fk_artifact_records_attempt_id_work_attempts",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT fk_raw_artifacts_worker_id_worker_registrations "
        "TO fk_artifact_records_worker_id_worker_registrations",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT uq_raw_artifacts_upload_id TO uq_artifact_records_upload_id",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT ck_raw_artifacts_content_type TO ck_artifact_records_content_type",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT ck_raw_artifacts_source_policy_digest_format "
        "TO ck_artifact_records_source_policy_digest_format",
    )
    reverse_statements = (
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT pk_artifact_records TO pk_raw_artifacts",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT fk_artifact_records_object_id_artifact_objects "
        "TO fk_raw_artifacts_object_id_artifact_objects",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT fk_artifact_records_upload_id_artifact_uploads "
        "TO fk_raw_artifacts_upload_id_artifact_uploads",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT fk_artifact_records_work_id_work_units "
        "TO fk_raw_artifacts_work_id_work_units",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT fk_artifact_records_attempt_id_work_attempts "
        "TO fk_raw_artifacts_attempt_id_work_attempts",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT fk_artifact_records_worker_id_worker_registrations "
        "TO fk_raw_artifacts_worker_id_worker_registrations",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT uq_artifact_records_upload_id TO uq_raw_artifacts_upload_id",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT ck_artifact_records_content_type TO ck_raw_artifacts_content_type",
        "ALTER TABLE sources.artifact_records "
        "RENAME CONSTRAINT ck_artifact_records_source_policy_digest_format "
        "TO ck_raw_artifacts_source_policy_digest_format",
    )
    statements = forward_statements if to_records else reverse_statements
    for statement in statements:
        op.execute(statement)
