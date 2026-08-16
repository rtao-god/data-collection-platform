from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy import create_engine

from collection_application.artifact_cleanup import (
    ArtifactCleanupPolicy,
    ArtifactCleanupService,
)
from collection_infrastructure.artifact_cleanup_object_store import (
    S3ArtifactCleanupObjectStore,
)
from collection_infrastructure.postgres.artifact_cleanup import (
    PostgresArtifactCleanupStore,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collector-artifact-cleanup")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_integer("ARTIFACT_CLEANUP_BATCH_SIZE", 100),
    )
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=_integer("ARTIFACT_CLEANUP_GRACE_SECONDS", 86_400),
    )
    parser.add_argument(
        "--claim-seconds",
        type=int,
        default=_integer("ARTIFACT_CLEANUP_CLAIM_SECONDS", 900),
    )
    parser.add_argument(
        "--retry-seconds",
        type=int,
        default=_integer("ARTIFACT_CLEANUP_RETRY_SECONDS", 300),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=_integer("ARTIFACT_CLEANUP_MAX_ATTEMPTS", 10),
    )
    arguments = parser.parse_args(argv)

    engine = create_engine(_required("COLLECTOR_DATABASE_URL"), pool_pre_ping=True)
    try:
        store = PostgresArtifactCleanupStore(engine)
        object_store = S3ArtifactCleanupObjectStore(
            bucket=_required("ARTIFACT_S3_BUCKET"),
            endpoint_url=_required("ARTIFACT_S3_ENDPOINT_URL"),
            access_key_id=_required("ARTIFACT_S3_ACCESS_KEY_ID"),
            secret_access_key=_required("ARTIFACT_S3_SECRET_ACCESS_KEY"),
            region=_required("ARTIFACT_S3_REGION"),
        )
        result = ArtifactCleanupService(store, object_store).run_once(
            now_utc=datetime.now(UTC),
            policy=ArtifactCleanupPolicy(
                grace_period=timedelta(seconds=arguments.grace_seconds),
                claim_timeout=timedelta(seconds=arguments.claim_seconds),
                retry_delay=timedelta(seconds=arguments.retry_seconds),
                batch_size=arguments.batch_size,
                max_attempts=arguments.max_attempts,
            ),
        )
    finally:
        engine.dispose()

    print(
        json.dumps(
            {
                "claimedCount": result.claimed_count,
                "deletedCount": result.deleted_count,
                "retryScheduledCount": result.retry_scheduled_count,
                "failedCount": result.failed_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        _configuration_error(name)
    return value.strip()


def _integer(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        _configuration_error(name)


def _configuration_error(name: str) -> NoReturn:
    raise RuntimeError(f"required artifact cleanup setting {name} is missing or invalid")


if __name__ == "__main__":
    raise SystemExit(main())
