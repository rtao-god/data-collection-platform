"""Create durable content-addressed artifact transfer metadata.

Revision ID: 20260811_0005
Revises: 20260811_0004
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260811_0005"
down_revision: str | None = "20260811_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sources.artifact_uploads (
            upload_id UUID NOT NULL,
            work_id UUID NOT NULL,
            lease_id UUID NOT NULL,
            lease_token UUID NOT NULL,
            worker_id TEXT NOT NULL,
            input_digest TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            expected_digest TEXT NOT NULL,
            expected_size_bytes BIGINT NOT NULL,
            content_type TEXT NOT NULL,
            staging_reference TEXT NOT NULL,
            final_reference TEXT,
            state TEXT NOT NULL,
            prepared_at_utc TIMESTAMPTZ NOT NULL,
            expires_at_utc TIMESTAMPTZ NOT NULL,
            verified_at_utc TIMESTAMPTZ,
            consumed_at_utc TIMESTAMPTZ,
            revision BIGINT NOT NULL,
            correlation_id TEXT NOT NULL,
            CONSTRAINT pk_artifact_uploads PRIMARY KEY (upload_id),
            CONSTRAINT fk_artifact_uploads_work_id_work_units
                FOREIGN KEY(work_id) REFERENCES work.work_units (work_id),
            CONSTRAINT fk_artifact_uploads_worker_id_worker_registrations
                FOREIGN KEY(worker_id) REFERENCES work.worker_registrations (worker_id),
            CONSTRAINT uq_artifact_uploads_staging_reference UNIQUE (staging_reference),
            CONSTRAINT ck_artifact_uploads_input_digest_format
                CHECK (input_digest ~ '^sha256:[0-9a-f]{64}$'),
            CONSTRAINT ck_artifact_uploads_expected_digest_format
                CHECK (expected_digest ~ '^sha256:[0-9a-f]{64}$'),
            CONSTRAINT ck_artifact_uploads_kind
                CHECK (artifact_kind IN ('raw_artifact', 'diagnostic_artifact')),
            CONSTRAINT ck_artifact_uploads_state
                CHECK (state IN ('prepared', 'verified', 'consumed')),
            CONSTRAINT ck_artifact_uploads_size
                CHECK (expected_size_bytes BETWEEN 1 AND 5368709120),
            CONSTRAINT ck_artifact_uploads_content_type
                CHECK (
                    content_type ~ '^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/'
                                   '[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$'
                ),
            CONSTRAINT ck_artifact_uploads_storage_reference
                CHECK (
                    staging_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}$'
                    AND (
                        final_reference IS NULL
                        OR final_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}$'
                    )
                ),
            CONSTRAINT ck_artifact_uploads_expiry_order
                CHECK (prepared_at_utc < expires_at_utc),
            CONSTRAINT ck_artifact_uploads_state_shape CHECK (
                (
                    state = 'prepared'
                    AND final_reference IS NULL
                    AND verified_at_utc IS NULL
                    AND consumed_at_utc IS NULL
                ) OR (
                    state = 'verified'
                    AND final_reference IS NOT NULL
                    AND verified_at_utc IS NOT NULL
                    AND consumed_at_utc IS NULL
                ) OR (
                    state = 'consumed'
                    AND final_reference IS NOT NULL
                    AND verified_at_utc IS NOT NULL
                    AND consumed_at_utc IS NOT NULL
                    AND consumed_at_utc >= verified_at_utc
                )
            ),
            CONSTRAINT ck_artifact_uploads_revision CHECK (revision >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_artifact_uploads_orphan_candidates
        ON sources.artifact_uploads (state, expires_at_utc)
        WHERE state IN ('prepared', 'verified')
        """
    )
    op.execute(
        """
        CREATE TABLE sources.artifact_objects (
            object_id UUID NOT NULL,
            content_digest TEXT NOT NULL,
            size_bytes BIGINT NOT NULL,
            storage_reference TEXT NOT NULL,
            verified_at_utc TIMESTAMPTZ NOT NULL,
            recorded_at_utc TIMESTAMPTZ NOT NULL,
            correlation_id TEXT NOT NULL,
            CONSTRAINT pk_artifact_objects PRIMARY KEY (object_id),
            CONSTRAINT uq_artifact_objects_content_digest UNIQUE (content_digest),
            CONSTRAINT uq_artifact_objects_storage_reference UNIQUE (storage_reference),
            CONSTRAINT ck_artifact_objects_digest_format
                CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
            CONSTRAINT ck_artifact_objects_size
                CHECK (size_bytes BETWEEN 1 AND 5368709120),
            CONSTRAINT ck_artifact_objects_storage_reference
                CHECK (storage_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}$'),
            CONSTRAINT ck_artifact_objects_time_order
                CHECK (recorded_at_utc >= verified_at_utc)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE sources.raw_artifacts (
            artifact_id UUID NOT NULL,
            object_id UUID NOT NULL,
            upload_id UUID NOT NULL,
            work_id UUID NOT NULL,
            attempt_id UUID NOT NULL,
            worker_id TEXT NOT NULL,
            content_type TEXT NOT NULL,
            source_policy_digest TEXT,
            recorded_at_utc TIMESTAMPTZ NOT NULL,
            correlation_id TEXT NOT NULL,
            CONSTRAINT pk_raw_artifacts PRIMARY KEY (artifact_id),
            CONSTRAINT fk_raw_artifacts_object_id_artifact_objects
                FOREIGN KEY(object_id) REFERENCES sources.artifact_objects (object_id),
            CONSTRAINT fk_raw_artifacts_upload_id_artifact_uploads
                FOREIGN KEY(upload_id) REFERENCES sources.artifact_uploads (upload_id),
            CONSTRAINT fk_raw_artifacts_work_id_work_units
                FOREIGN KEY(work_id) REFERENCES work.work_units (work_id),
            CONSTRAINT fk_raw_artifacts_attempt_id_work_attempts
                FOREIGN KEY(attempt_id) REFERENCES work.work_attempts (attempt_id),
            CONSTRAINT fk_raw_artifacts_worker_id_worker_registrations
                FOREIGN KEY(worker_id) REFERENCES work.worker_registrations (worker_id),
            CONSTRAINT uq_raw_artifacts_upload_id UNIQUE (upload_id),
            CONSTRAINT ck_raw_artifacts_content_type
                CHECK (
                    content_type ~ '^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/'
                                   '[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$'
                ),
            CONSTRAINT ck_raw_artifacts_source_policy_digest_format
                CHECK (
                    source_policy_digest IS NULL
                    OR source_policy_digest ~ '^sha256:[0-9a-f]{64}$'
                )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE work.work_output_artifacts (
            work_id UUID NOT NULL,
            position INTEGER NOT NULL,
            artifact_id UUID NOT NULL,
            CONSTRAINT pk_work_output_artifacts PRIMARY KEY (work_id, position),
            CONSTRAINT fk_work_output_artifacts_work_id_work_units
                FOREIGN KEY(work_id) REFERENCES work.work_units (work_id),
            CONSTRAINT fk_work_output_artifacts_artifact_id_raw_artifacts
                FOREIGN KEY(artifact_id) REFERENCES sources.raw_artifacts (artifact_id),
            CONSTRAINT uq_work_output_artifacts_artifact_id UNIQUE (artifact_id),
            CONSTRAINT ck_work_output_artifacts_position CHECK (position BETWEEN 0 AND 31)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE work.work_output_artifacts")
    op.execute("DROP TABLE sources.raw_artifacts")
    op.execute("DROP TABLE sources.artifact_objects")
    op.execute("DROP INDEX sources.ix_artifact_uploads_orphan_candidates")
    op.execute("DROP TABLE sources.artifact_uploads")
