from __future__ import annotations

import sqlalchemy as sa

from collection_application import ArtifactKind
from collection_infrastructure.postgres.metadata import collector_metadata

SOURCES_SCHEMA = "sources"
WORK_SCHEMA = "work"

_ARTIFACT_KINDS = tuple(value.value for value in ArtifactKind)
_UPLOAD_STATES = ("prepared", "verified", "consumed")


def _in_values(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


artifact_uploads = sa.Table(
    "artifact_uploads",
    collector_metadata,
    sa.Column("upload_id", sa.Uuid, primary_key=True),
    sa.Column(
        "work_id",
        sa.Uuid,
        sa.ForeignKey("work.work_units.work_id"),
        nullable=False,
    ),
    sa.Column("lease_id", sa.Uuid, nullable=False),
    sa.Column("lease_token", sa.Uuid, nullable=False),
    sa.Column(
        "worker_id",
        sa.Text,
        sa.ForeignKey("work.worker_registrations.worker_id"),
        nullable=False,
    ),
    sa.Column("input_digest", sa.Text, nullable=False),
    sa.Column("artifact_kind", sa.Text, nullable=False),
    sa.Column("expected_digest", sa.Text, nullable=False),
    sa.Column("expected_size_bytes", sa.BigInteger, nullable=False),
    sa.Column("content_type", sa.Text, nullable=False),
    sa.Column("staging_reference", sa.Text, nullable=False),
    sa.Column("final_reference", sa.Text, nullable=True),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("prepared_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("verified_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("consumed_at_utc", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revision", sa.BigInteger, nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.UniqueConstraint("staging_reference", name="uq_artifact_uploads_staging_reference"),
    sa.CheckConstraint(
        "input_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_artifact_uploads_input_digest_format",
    ),
    sa.CheckConstraint(
        "expected_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_artifact_uploads_expected_digest_format",
    ),
    sa.CheckConstraint(
        _in_values("artifact_kind", _ARTIFACT_KINDS),
        name="ck_artifact_uploads_kind",
    ),
    sa.CheckConstraint(
        _in_values("state", _UPLOAD_STATES),
        name="ck_artifact_uploads_state",
    ),
    sa.CheckConstraint(
        "expected_size_bytes BETWEEN 1 AND 5368709120",
        name="ck_artifact_uploads_size",
    ),
    sa.CheckConstraint(
        "content_type ~ '^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/"
        "[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$'",
        name="ck_artifact_uploads_content_type",
    ),
    sa.CheckConstraint(
        "staging_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}$' AND "
        "(final_reference IS NULL OR "
        "final_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}$')",
        name="ck_artifact_uploads_storage_reference",
    ),
    sa.CheckConstraint(
        "prepared_at_utc < expires_at_utc",
        name="ck_artifact_uploads_expiry_order",
    ),
    sa.CheckConstraint(
        "(state = 'prepared' AND final_reference IS NULL AND "
        "verified_at_utc IS NULL AND consumed_at_utc IS NULL) OR "
        "(state = 'verified' AND final_reference IS NOT NULL AND "
        "verified_at_utc IS NOT NULL AND consumed_at_utc IS NULL) OR "
        "(state = 'consumed' AND final_reference IS NOT NULL AND "
        "verified_at_utc IS NOT NULL AND consumed_at_utc IS NOT NULL AND "
        "consumed_at_utc >= verified_at_utc)",
        name="ck_artifact_uploads_state_shape",
    ),
    sa.CheckConstraint("revision >= 0", name="ck_artifact_uploads_revision"),
    schema=SOURCES_SCHEMA,
)

sa.Index(
    "ix_artifact_uploads_orphan_candidates",
    artifact_uploads.c.state,
    artifact_uploads.c.expires_at_utc,
    postgresql_where=artifact_uploads.c.state.in_(("prepared", "verified")),
)

artifact_objects = sa.Table(
    "artifact_objects",
    collector_metadata,
    sa.Column("object_id", sa.Uuid, primary_key=True),
    sa.Column("content_digest", sa.Text, nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("storage_reference", sa.Text, nullable=False),
    sa.Column("verified_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.UniqueConstraint("content_digest", name="uq_artifact_objects_content_digest"),
    sa.UniqueConstraint("storage_reference", name="uq_artifact_objects_storage_reference"),
    sa.CheckConstraint(
        "content_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_artifact_objects_digest_format",
    ),
    sa.CheckConstraint(
        "size_bytes BETWEEN 1 AND 5368709120",
        name="ck_artifact_objects_size",
    ),
    sa.CheckConstraint(
        "storage_reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}$'",
        name="ck_artifact_objects_storage_reference",
    ),
    sa.CheckConstraint(
        "recorded_at_utc >= verified_at_utc",
        name="ck_artifact_objects_time_order",
    ),
    schema=SOURCES_SCHEMA,
)

raw_artifacts = sa.Table(
    "raw_artifacts",
    collector_metadata,
    sa.Column("artifact_id", sa.Uuid, primary_key=True),
    sa.Column(
        "object_id",
        sa.Uuid,
        sa.ForeignKey("sources.artifact_objects.object_id"),
        nullable=False,
    ),
    sa.Column(
        "upload_id",
        sa.Uuid,
        sa.ForeignKey("sources.artifact_uploads.upload_id"),
        nullable=False,
    ),
    sa.Column(
        "work_id",
        sa.Uuid,
        sa.ForeignKey("work.work_units.work_id"),
        nullable=False,
    ),
    sa.Column(
        "attempt_id",
        sa.Uuid,
        sa.ForeignKey("work.work_attempts.attempt_id"),
        nullable=False,
    ),
    sa.Column(
        "worker_id",
        sa.Text,
        sa.ForeignKey("work.worker_registrations.worker_id"),
        nullable=False,
    ),
    sa.Column("content_type", sa.Text, nullable=False),
    sa.Column("source_policy_digest", sa.Text, nullable=True),
    sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text, nullable=False),
    sa.UniqueConstraint("upload_id", name="uq_raw_artifacts_upload_id"),
    sa.CheckConstraint(
        "content_type ~ '^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/"
        "[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$'",
        name="ck_raw_artifacts_content_type",
    ),
    sa.CheckConstraint(
        "source_policy_digest IS NULL OR "
        "source_policy_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_raw_artifacts_source_policy_digest_format",
    ),
    schema=SOURCES_SCHEMA,
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
