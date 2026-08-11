from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from collection_application import (
    ArtifactKind,
    ArtifactTransferConflict,
    PrepareArtifactRead,
    PrepareArtifactUpload,
    PreparedArtifactRead,
    PreparedArtifactUpload,
    VerifiedArtifactUpload,
    VerifyArtifactUpload,
)
from collection_infrastructure.object_store import (
    ArtifactObjectStoreError,
    S3ArtifactObjectStore,
)
from collection_infrastructure.postgres.artifact_metadata import (
    artifact_objects,
    artifact_uploads,
    raw_artifacts,
    work_input_artifacts,
)
from collection_infrastructure.postgres.work_metadata import work_attempts, work_units

_ResultT = TypeVar("_ResultT")


class PostgresArtifactTransfer:
    """Binds S3-compatible artifact transfer to exact PostgreSQL lease ownership."""

    def __init__(
        self,
        engine: Engine,
        object_store: S3ArtifactObjectStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._object_store = object_store
        self._clock = clock or (lambda: datetime.now(UTC))

    def prepare_upload(self, command: PrepareArtifactUpload) -> PreparedArtifactUpload:
        now_utc = self._now_utc()
        prepared = self._object_call(
            lambda: self._object_store.prepare_upload(
                upload_id=command.upload_id,
                artifact_kind=command.artifact_kind,
                expected_digest=command.expected_digest,
                expected_size_bytes=command.expected_size_bytes,
                content_type=command.content_type,
                expires_in_seconds=command.expires_in_seconds,
                now_utc=now_utc,
            )
        )
        self._transaction(
            lambda connection: self._record_prepared_upload(
                connection,
                command,
                staging_reference=prepared.staging_reference,
                prepared_at_utc=now_utc,
                expires_at_utc=prepared.expires_at_utc,
            )
        )
        return PreparedArtifactUpload(
            upload_id=command.upload_id,
            method="PUT",
            url=prepared.url,
            required_headers=prepared.required_headers,
            expires_at_utc=prepared.expires_at_utc,
        )

    def verify_upload(self, command: VerifyArtifactUpload) -> VerifiedArtifactUpload:
        prepared = self._transaction(
            lambda connection: self._load_upload_for_verification(connection, command)
        )
        if prepared["state"] in {"verified", "consumed"}:
            return _verified_upload_from_row(prepared)
        now_utc = self._now_utc()
        verified = self._object_call(
            lambda: self._object_store.verify_and_promote(
                staging_reference=str(prepared["staging_reference"]),
                artifact_kind=ArtifactKind(str(prepared["artifact_kind"])),
                expected_digest=str(prepared["expected_digest"]),
                expected_size_bytes=int(prepared["expected_size_bytes"]),
                expected_content_type=str(prepared["content_type"]),
                now_utc=now_utc,
            )
        )
        row = self._transaction(
            lambda connection: self._mark_verified(
                connection,
                command,
                final_reference=verified.final_reference,
                verified_at_utc=verified.verified_at_utc,
            )
        )
        return _verified_upload_from_row(row)

    def prepare_read(self, command: PrepareArtifactRead) -> PreparedArtifactRead:
        storage_reference = self._transaction(
            lambda connection: self._authorize_scoped_read(connection, command)
        )
        now_utc = self._now_utc()
        prepared = self._object_call(
            lambda: self._object_store.prepare_read(
                storage_reference=storage_reference,
                expires_in_seconds=command.expires_in_seconds,
                now_utc=now_utc,
            )
        )
        return PreparedArtifactRead(
            artifact_id=command.artifact_id,
            method="GET",
            url=prepared.url,
            expires_at_utc=prepared.expires_at_utc,
        )

    def _transaction(self, operation: Callable[[Connection], _ResultT]) -> _ResultT:
        try:
            with self._engine.begin() as connection:
                return operation(connection)
        except ArtifactTransferConflict:
            raise
        except SQLAlchemyError as exc:
            raise ArtifactTransferConflict(
                code="ARTIFACT_STORAGE_STATE_FAILED",
                message="The artifact transfer database operation did not complete.",
                context={"causeType": type(exc).__name__},
                required_action=(
                    "Inspect the artifact and Work Engine rows, correct the owner state, and retry "
                    "the exact artifact command."
                ),
            ) from exc

    def _object_call(self, operation: Callable[[], _ResultT]) -> _ResultT:
        try:
            return operation()
        except ArtifactObjectStoreError as exc:
            raise ArtifactTransferConflict(
                code=exc.code,
                message=exc.message,
                context=exc.context,
                required_action=exc.required_action,
            ) from exc

    def _record_prepared_upload(
        self,
        connection: Connection,
        command: PrepareArtifactUpload,
        *,
        staging_reference: str,
        prepared_at_utc: datetime,
        expires_at_utc: datetime,
    ) -> None:
        self._require_active_lease(
            connection,
            now_utc=prepared_at_utc,
            work_id=command.work_id,
            lease_id=command.lease_id,
            lease_token=command.lease_token,
            worker_id=command.worker_id,
            input_digest=command.input_digest,
        )
        existing = (
            connection.execute(
                sa.select(artifact_uploads)
                .where(artifact_uploads.c.upload_id == command.upload_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if not _same_upload_identity(existing, command, staging_reference):
                raise _conflict(
                    code="ARTIFACT_UPLOAD_IDENTITY_CONFLICT",
                    message="The upload ID is already bound to different immutable input.",
                    context={"uploadId": str(command.upload_id)},
                    required_action="Use the existing upload identity or create a new upload ID.",
                )
            if existing["state"] != "prepared":
                raise _conflict(
                    code="ARTIFACT_UPLOAD_ALREADY_VERIFIED",
                    message="The upload has already passed object-store verification.",
                    context={"uploadId": str(command.upload_id), "state": existing["state"]},
                    required_action="Use the verified upload in the exact work completion.",
                )
            connection.execute(
                sa.update(artifact_uploads)
                .where(artifact_uploads.c.upload_id == command.upload_id)
                .values(
                    expires_at_utc=expires_at_utc,
                    revision=artifact_uploads.c.revision + 1,
                    correlation_id=command.correlation_id,
                )
            )
            return
        connection.execute(
            sa.insert(artifact_uploads).values(
                upload_id=command.upload_id,
                work_id=command.work_id,
                lease_id=command.lease_id,
                lease_token=command.lease_token,
                worker_id=command.worker_id,
                input_digest=command.input_digest,
                artifact_kind=command.artifact_kind.value,
                expected_digest=command.expected_digest,
                expected_size_bytes=command.expected_size_bytes,
                content_type=command.content_type,
                staging_reference=staging_reference,
                final_reference=None,
                state="prepared",
                prepared_at_utc=prepared_at_utc,
                expires_at_utc=expires_at_utc,
                verified_at_utc=None,
                consumed_at_utc=None,
                revision=0,
                correlation_id=command.correlation_id,
            )
        )

    def _load_upload_for_verification(
        self,
        connection: Connection,
        command: VerifyArtifactUpload,
    ) -> RowMapping:
        now_utc = self._now_utc()
        row = (
            connection.execute(
                sa.select(artifact_uploads)
                .where(artifact_uploads.c.upload_id == command.upload_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise _conflict(
                code="ARTIFACT_UPLOAD_NOT_REGISTERED",
                message="The upload ID was not prepared by the Artifact Transfer owner.",
                context={"uploadId": str(command.upload_id)},
                required_action="Prepare the upload before uploading or verifying an object.",
            )
        _require_upload_lease_identity(row, command)
        if row["state"] == "prepared" and now_utc >= row["expires_at_utc"]:
            raise _conflict(
                code="ARTIFACT_UPLOAD_EXPIRED",
                message="The prepared upload expired before verification.",
                context={"uploadId": str(command.upload_id)},
                required_action="Prepare a new upload under an active work lease.",
            )
        self._require_active_lease(
            connection,
            now_utc=now_utc,
            work_id=command.work_id,
            lease_id=command.lease_id,
            lease_token=command.lease_token,
            worker_id=command.worker_id,
            input_digest=command.input_digest,
        )
        return row

    def _mark_verified(
        self,
        connection: Connection,
        command: VerifyArtifactUpload,
        *,
        final_reference: str,
        verified_at_utc: datetime,
    ) -> RowMapping:
        row = (
            connection.execute(
                sa.select(artifact_uploads)
                .where(artifact_uploads.c.upload_id == command.upload_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise _conflict(
                code="ARTIFACT_UPLOAD_NOT_REGISTERED",
                message="The upload disappeared before verification could be recorded.",
                context={"uploadId": str(command.upload_id)},
                required_action="Inspect artifact persistence before retrying verification.",
            )
        _require_upload_lease_identity(row, command)
        if row["state"] in {"verified", "consumed"}:
            if row["final_reference"] != final_reference:
                raise _conflict(
                    code="ARTIFACT_VERIFICATION_CONFLICT",
                    message="The upload is already bound to another content-addressed object.",
                    context={"uploadId": str(command.upload_id)},
                    required_action="Use the previously verified object identity.",
                )
            return row
        self._require_active_lease(
            connection,
            now_utc=verified_at_utc,
            work_id=command.work_id,
            lease_id=command.lease_id,
            lease_token=command.lease_token,
            worker_id=command.worker_id,
            input_digest=command.input_digest,
        )
        return (
            connection.execute(
                sa.update(artifact_uploads)
                .where(artifact_uploads.c.upload_id == command.upload_id)
                .values(
                    final_reference=final_reference,
                    state="verified",
                    verified_at_utc=verified_at_utc,
                    revision=artifact_uploads.c.revision + 1,
                    correlation_id=command.correlation_id,
                )
                .returning(*artifact_uploads.c)
            )
            .mappings()
            .one()
        )

    def _authorize_scoped_read(
        self,
        connection: Connection,
        command: PrepareArtifactRead,
    ) -> str:
        self._require_active_lease(
            connection,
            now_utc=self._now_utc(),
            work_id=command.work_id,
            lease_id=command.lease_id,
            lease_token=command.lease_token,
            worker_id=command.worker_id,
            input_digest=command.input_digest,
        )
        storage_reference = connection.execute(
            sa.select(artifact_objects.c.storage_reference)
            .select_from(
                work_input_artifacts.join(
                    raw_artifacts,
                    raw_artifacts.c.artifact_id == work_input_artifacts.c.artifact_id,
                ).join(
                    artifact_objects,
                    artifact_objects.c.object_id == raw_artifacts.c.object_id,
                )
            )
            .where(
                work_input_artifacts.c.work_id == command.work_id,
                work_input_artifacts.c.artifact_id == command.artifact_id,
            )
        ).scalar_one_or_none()
        if storage_reference is None:
            raise _conflict(
                code="ARTIFACT_READ_FORBIDDEN",
                message="The requested artifact is not an input to the leased work unit.",
                context={
                    "artifactId": str(command.artifact_id),
                    "workId": str(command.work_id),
                },
                required_action="Request only an artifact declared by the leased work input contract.",
            )
        return str(storage_reference)

    def _require_active_lease(
        self,
        connection: Connection,
        *,
        now_utc: datetime,
        work_id: UUID,
        lease_id: UUID,
        lease_token: UUID,
        worker_id: str,
        input_digest: str,
    ) -> None:
        row = (
            connection.execute(
                sa.select(
                    work_units.c.work_id,
                    work_units.c.state,
                    work_units.c.active_lease_id,
                    work_units.c.active_lease_token,
                    work_units.c.active_worker_id,
                    work_units.c.input_digest,
                    work_units.c.lease_expires_at_utc,
                    work_units.c.heartbeat_deadline_utc,
                    work_attempts.c.outcome,
                )
                .select_from(
                    work_units.join(
                        work_attempts,
                        work_attempts.c.lease_id == work_units.c.active_lease_id,
                    )
                )
                .where(work_units.c.work_id == work_id)
                .with_for_update(of=work_units)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise _stale_conflict(work_id, "lease_not_active")
        checks = (
            (row["state"] == "leased", "lease_not_active"),
            (row["active_lease_id"] == lease_id, "lease_id_mismatch"),
            (row["active_lease_token"] == lease_token, "lease_token_mismatch"),
            (row["active_worker_id"] == worker_id, "worker_id_mismatch"),
            (row["input_digest"] == input_digest, "input_digest_mismatch"),
            (row["outcome"] == "leased", "attempt_not_active"),
        )
        for valid, reason in checks:
            if not valid:
                raise _stale_conflict(work_id, reason)
        expires_at_utc = row["lease_expires_at_utc"]
        heartbeat_deadline_utc = row["heartbeat_deadline_utc"]
        if not isinstance(expires_at_utc, datetime) or not isinstance(
            heartbeat_deadline_utc, datetime
        ):
            raise _conflict(
                code="ARTIFACT_LEASE_STATE_INVALID",
                message="The leased work has invalid persisted deadline values.",
                context={"workId": str(work_id)},
                required_action="Repair the Work Engine state through its owner recovery path.",
            )
        if now_utc >= expires_at_utc or now_utc >= heartbeat_deadline_utc:
            raise _stale_conflict(work_id, "lease_expired")

    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Artifact Transfer clock must return timezone-aware UTC")
        return value


def _same_upload_identity(
    row: Mapping[str, object],
    command: PrepareArtifactUpload,
    staging_reference: str,
) -> bool:
    return bool(
        row["work_id"] == command.work_id
        and row["lease_id"] == command.lease_id
        and row["lease_token"] == command.lease_token
        and row["worker_id"] == command.worker_id
        and row["input_digest"] == command.input_digest
        and row["artifact_kind"] == command.artifact_kind.value
        and row["expected_digest"] == command.expected_digest
        and row["expected_size_bytes"] == command.expected_size_bytes
        and row["content_type"] == command.content_type
        and row["staging_reference"] == staging_reference
    )


def _require_upload_lease_identity(
    row: Mapping[str, object],
    command: VerifyArtifactUpload,
) -> None:
    checks = (
        (row["work_id"] == command.work_id, "work_id_mismatch"),
        (row["lease_id"] == command.lease_id, "lease_id_mismatch"),
        (row["lease_token"] == command.lease_token, "lease_token_mismatch"),
        (row["worker_id"] == command.worker_id, "worker_id_mismatch"),
        (row["input_digest"] == command.input_digest, "input_digest_mismatch"),
    )
    for valid, reason in checks:
        if not valid:
            raise _stale_conflict(command.work_id, reason)


def _verified_upload_from_row(row: Mapping[str, object]) -> VerifiedArtifactUpload:
    final_reference = row["final_reference"]
    verified_at_utc = row["verified_at_utc"]
    if not isinstance(final_reference, str) or not isinstance(verified_at_utc, datetime):
        raise _conflict(
            code="ARTIFACT_VERIFICATION_STATE_INVALID",
            message="The verified upload has incomplete persisted object identity.",
            context={"uploadId": str(row["upload_id"])},
            required_action="Repair the artifact upload through its owner recovery path.",
        )
    return VerifiedArtifactUpload(
        upload_id=UUID(str(row["upload_id"])),
        work_id=UUID(str(row["work_id"])),
        artifact_kind=ArtifactKind(str(row["artifact_kind"])),
        content_digest=str(row["expected_digest"]),
        size_bytes=int(row["expected_size_bytes"]),
        content_type=str(row["content_type"]),
        storage_reference=final_reference,
        verified_at_utc=verified_at_utc,
    )


def _stale_conflict(work_id: UUID, reason: str) -> ArtifactTransferConflict:
    return _conflict(
        code="ARTIFACT_LEASE_STALE",
        message="The worker no longer owns the lease for this artifact operation.",
        context={"workId": str(work_id), "reason": reason},
        required_action="Discard the artifact operation and acquire a new work lease.",
    )


def _conflict(
    *,
    code: str,
    message: str,
    context: Mapping[str, object],
    required_action: str,
) -> ArtifactTransferConflict:
    return ArtifactTransferConflict(
        code=code,
        message=message,
        context=context,
        required_action=required_action,
    )
