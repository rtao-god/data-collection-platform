from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from hashlib import sha256
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

_DATABASE_URL_VARIABLE = "COLLECTOR_DATABASE_URL"


def database_url(purpose: str) -> str:
    """Resolve the integration database URL, failing loudly when it is unconfigured."""
    value = os.environ.get(_DATABASE_URL_VARIABLE, "").strip()
    if not value:
        pytest.fail(f"{_DATABASE_URL_VARIABLE} is required for {purpose}.")
    return value


def unique_label(prefix: str) -> str:
    """Produce a per-run unique label so parallel or repeated runs cannot collide."""
    return f"{prefix}_{uuid4().hex}"


def digest(*parts: str) -> str:
    return f"sha256:{sha256(':'.join(parts).encode()).hexdigest()}"


def engine_fixture(purpose: str) -> Iterator[Engine]:
    value = sa.create_engine(database_url(purpose), poolclass=NullPool)
    try:
        yield value
    finally:
        value.dispose()


def insert_config_artifact(
    connection: sa.Connection,
    bundle_digest: str,
    *,
    recorded_at_utc: datetime | str,
) -> None:
    object_id = uuid4()
    artifact_id = uuid4()
    operation_id = uuid4()
    digest_value = bundle_digest.removeprefix("sha256:")
    connection.execute(
        sa.text(
            """
            INSERT INTO sources.artifact_objects (
                object_id, artifact_kind, content_digest, size_bytes, storage_reference,
                verified_at_utc, recorded_at_utc, correlation_id
            ) VALUES (
                :object_id, 'config_bundle', :bundle_digest, 1, :storage_reference,
                :recorded_at_utc, :recorded_at_utc, 'integration-config-artifact'
            )
            """
        ),
        {
            "object_id": object_id,
            "bundle_digest": bundle_digest,
            "storage_reference": (
                f"config-bundles/sha256/{digest_value[:2]}/{digest_value[2:4]}/{digest_value}"
            ),
            "recorded_at_utc": recorded_at_utc,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO sources.artifact_records (
                artifact_id, object_id, upload_id, work_id, attempt_id, worker_id,
                producer_kind, producer_identity, owner_operation_id, content_type,
                source_policy_digest, recorded_at_utc, correlation_id
            ) VALUES (
                :artifact_id, :object_id, NULL, NULL, NULL, NULL,
                'control_plane', 'integration-test', :operation_id, 'application/json',
                NULL, :recorded_at_utc, 'integration-config-artifact'
            )
            """
        ),
        {
            "artifact_id": artifact_id,
            "object_id": object_id,
            "operation_id": operation_id,
            "recorded_at_utc": recorded_at_utc,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO config.config_bundle_artifacts (
                bundle_digest, artifact_id, recorded_at_utc, correlation_id
            ) VALUES (
                :bundle_digest, :artifact_id, :recorded_at_utc,
                'integration-config-artifact'
            )
            """
        ),
        {
            "bundle_digest": bundle_digest,
            "artifact_id": artifact_id,
            "recorded_at_utc": recorded_at_utc,
        },
    )
