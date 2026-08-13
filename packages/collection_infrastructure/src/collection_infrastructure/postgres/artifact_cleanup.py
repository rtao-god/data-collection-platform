from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from collection_application.artifact_cleanup import (
    ArtifactCleanupClaim,
    ArtifactCleanupPolicy,
    ArtifactCleanupStore,
    validate_cleanup_failure,
)
from sqlalchemy import Engine
from sqlalchemy.engine import RowMapping

from collection_infrastructure.postgres.artifact_metadata import (
    artifact_cleanup_tombstones,
    artifact_objects,
    artifact_records,
    artifact_uploads,
)


class PostgresArtifactCleanupStore(ArtifactCleanupStore):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def claim(
        self,
        *,
        now_utc: datetime,
        policy: ArtifactCleanupPolicy,
    ) -> Sequence[ArtifactCleanupClaim]:
        cutoff = now_utc - policy.grace_period
        claim_expires = now_utc + policy.claim_timeout
        claims: list[ArtifactCleanupClaim] = []
        with self._engine.begin() as connection:
            retry_rows = (
                connection.execute(
                    sa.select(artifact_cleanup_tombstones)
                    .where(
                        sa.or_(
                            sa.and_(
                                artifact_cleanup_tombstones.c.state == "pending",
                                artifact_cleanup_tombstones.c.claim_expires_at_utc <= now_utc,
                            ),
                            sa.and_(
                                artifact_cleanup_tombstones.c.state == "retry_wait",
                                artifact_cleanup_tombstones.c.retry_not_before_utc <= now_utc,
                            ),
                        )
                    )
                    .order_by(
                        artifact_cleanup_tombstones.c.retry_not_before_utc.nullsfirst(),
                        artifact_cleanup_tombstones.c.created_at_utc,
                        artifact_cleanup_tombstones.c.tombstone_id,
                    )
                    .limit(policy.batch_size)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .all()
            )
            for row in retry_rows:
                attempt_count = int(row["attempt_count"]) + 1
                connection.execute(
                    sa.update(artifact_cleanup_tombstones)
                    .where(artifact_cleanup_tombstones.c.tombstone_id == row["tombstone_id"])
                    .values(
                        state="pending",
                        claimed_at_utc=now_utc,
                        claim_expires_at_utc=claim_expires,
                        attempt_count=attempt_count,
                        retry_not_before_utc=None,
                        error_code=None,
                        error_digest=None,
                        revision=artifact_cleanup_tombstones.c.revision + 1,
                    )
                )
                claims.append(_claim(row, attempt_count=attempt_count))

            remaining = policy.batch_size - len(claims)
            if remaining > 0:
                artifact_record_exists = sa.exists(
                    sa.select(1).where(artifact_records.c.upload_id == artifact_uploads.c.upload_id)
                )
                final_object_exists = sa.exists(
                    sa.select(1).where(
                        artifact_objects.c.storage_reference == artifact_uploads.c.final_reference
                    )
                )
                existing_tombstone = sa.exists(
                    sa.select(1).where(
                        artifact_cleanup_tombstones.c.upload_id == artifact_uploads.c.upload_id
                    )
                )
                storage_reference = sa.case(
                    (
                        artifact_uploads.c.state == "prepared",
                        artifact_uploads.c.staging_reference,
                    ),
                    else_=artifact_uploads.c.final_reference,
                ).label("cleanup_storage_reference")
                candidates = (
                    connection.execute(
                        sa.select(
                            artifact_uploads.c.upload_id,
                            artifact_uploads.c.state,
                            artifact_uploads.c.prepared_at_utc,
                            storage_reference,
                        )
                        .where(
                            artifact_uploads.c.state.in_(("prepared", "verified")),
                            artifact_uploads.c.prepared_at_utc <= cutoff,
                            sa.not_(artifact_record_exists),
                            sa.not_(existing_tombstone),
                            sa.or_(
                                artifact_uploads.c.state == "prepared",
                                sa.and_(
                                    artifact_uploads.c.state == "verified",
                                    artifact_uploads.c.final_reference.is_not(None),
                                    sa.not_(final_object_exists),
                                ),
                            ),
                        )
                        .order_by(
                            artifact_uploads.c.prepared_at_utc,
                            artifact_uploads.c.upload_id,
                        )
                        .limit(remaining)
                        .with_for_update(skip_locked=True)
                    )
                    .mappings()
                    .all()
                )
                for row in candidates:
                    tombstone_id = uuid4()
                    reference = str(row["cleanup_storage_reference"])
                    reason = "orphan_staging" if row["state"] == "prepared" else "orphan_verified"
                    connection.execute(
                        sa.insert(artifact_cleanup_tombstones).values(
                            tombstone_id=tombstone_id,
                            upload_id=row["upload_id"],
                            storage_reference=reference,
                            reason=reason,
                            state="pending",
                            created_at_utc=now_utc,
                            eligible_at_utc=_datetime(row, "prepared_at_utc") + policy.grace_period,
                            claimed_at_utc=now_utc,
                            claim_expires_at_utc=claim_expires,
                            attempt_count=1,
                            retry_not_before_utc=None,
                            deleted_at_utc=None,
                            error_code=None,
                            error_digest=None,
                            revision=0,
                        )
                    )
                    claims.append(
                        ArtifactCleanupClaim(
                            tombstone_id=tombstone_id,
                            upload_id=_uuid(row, "upload_id"),
                            storage_reference=reference,
                            attempt_count=1,
                        )
                    )
        return tuple(claims)

    def mark_deleted(
        self,
        claim: ArtifactCleanupClaim,
        *,
        deleted_at_utc: datetime,
    ) -> None:
        with self._engine.begin() as connection:
            result = connection.execute(
                sa.update(artifact_cleanup_tombstones)
                .where(
                    artifact_cleanup_tombstones.c.tombstone_id == claim.tombstone_id,
                    artifact_cleanup_tombstones.c.upload_id == claim.upload_id,
                    artifact_cleanup_tombstones.c.state == "pending",
                    artifact_cleanup_tombstones.c.attempt_count == claim.attempt_count,
                )
                .values(
                    state="deleted",
                    deleted_at_utc=deleted_at_utc,
                    retry_not_before_utc=None,
                    error_code=None,
                    error_digest=None,
                    revision=artifact_cleanup_tombstones.c.revision + 1,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("artifact cleanup claim is no longer active")

    def mark_failed(
        self,
        claim: ArtifactCleanupClaim,
        *,
        failed_at_utc: datetime,
        retry_not_before_utc: datetime | None,
        error_code: str,
        error_digest: str,
        terminal: bool,
    ) -> None:
        validate_cleanup_failure(error_code, error_digest)
        state = "failed" if terminal else "retry_wait"
        if terminal != (retry_not_before_utc is None):
            raise ValueError("artifact cleanup retry state is inconsistent")
        with self._engine.begin() as connection:
            result = connection.execute(
                sa.update(artifact_cleanup_tombstones)
                .where(
                    artifact_cleanup_tombstones.c.tombstone_id == claim.tombstone_id,
                    artifact_cleanup_tombstones.c.upload_id == claim.upload_id,
                    artifact_cleanup_tombstones.c.state == "pending",
                    artifact_cleanup_tombstones.c.attempt_count == claim.attempt_count,
                )
                .values(
                    state=state,
                    claimed_at_utc=failed_at_utc,
                    claim_expires_at_utc=failed_at_utc,
                    retry_not_before_utc=retry_not_before_utc,
                    error_code=error_code,
                    error_digest=error_digest,
                    revision=artifact_cleanup_tombstones.c.revision + 1,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("artifact cleanup claim is no longer active")


def _claim(row: RowMapping, *, attempt_count: int) -> ArtifactCleanupClaim:
    return ArtifactCleanupClaim(
        tombstone_id=_uuid(row, "tombstone_id"),
        upload_id=_uuid(row, "upload_id"),
        storage_reference=str(row["storage_reference"]),
        attempt_count=attempt_count,
    )


def _uuid(row: RowMapping, key: str) -> UUID:
    value = row[key]
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _datetime(row: RowMapping, key: str) -> datetime:
    value = row[key]
    if not isinstance(value, datetime):
        raise RuntimeError(f"artifact cleanup row {key} is not a datetime")
    return value
