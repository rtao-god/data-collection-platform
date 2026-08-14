from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from collection_application import (
    ArtifactKind,
    OwnedArtifactPublishConflict,
    PublishedOwnedArtifact,
    PublishOwnedArtifact,
)
from collection_infrastructure.object_store import ArtifactObjectStoreError, S3ArtifactObjectStore
from collection_infrastructure.postgres.artifact_metadata import artifact_objects, artifact_records

_ResultT = TypeVar("_ResultT")


class PostgresOwnedArtifactPublisher:
    """Publishes trusted control-plane artifacts before recording immutable metadata."""

    def __init__(
        self,
        engine: Engine,
        object_store: S3ArtifactObjectStore,
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._engine = engine
        self._object_store = object_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4

    def publish(self, command: PublishOwnedArtifact) -> PublishedOwnedArtifact:
        now_utc = self._now_utc()
        try:
            stored = self._object_store.store_bytes(
                artifact_kind=command.artifact_kind,
                content=command.content,
                content_type=command.content_type,
                now_utc=now_utc,
            )
        except ArtifactObjectStoreError as exc:
            raise OwnedArtifactPublishConflict(
                code=exc.code,
                message=exc.message,
                context=exc.context,
                required_action=exc.required_action,
            ) from exc
        return self._transaction(
            lambda connection: self._record(
                connection,
                command,
                content_digest=stored.content_digest,
                size_bytes=stored.size_bytes,
                storage_reference=stored.final_reference,
                verified_at_utc=stored.verified_at_utc,
                recorded_at_utc=now_utc,
            )
        )

    def _record(
        self,
        connection: Connection,
        command: PublishOwnedArtifact,
        *,
        content_digest: str,
        size_bytes: int,
        storage_reference: str,
        verified_at_utc: datetime,
        recorded_at_utc: datetime,
    ) -> PublishedOwnedArtifact:
        _advisory_lock(
            connection,
            f"owned-artifact:{command.producer_identity}:{command.operation_id}",
        )
        existing = (
            connection.execute(
                sa.select(
                    artifact_records,
                    artifact_objects.c.artifact_kind,
                    artifact_objects.c.content_digest,
                    artifact_objects.c.size_bytes,
                    artifact_objects.c.storage_reference,
                )
                .select_from(
                    artifact_records.join(
                        artifact_objects,
                        artifact_objects.c.object_id == artifact_records.c.object_id,
                    )
                )
                .where(
                    sa.or_(
                        artifact_records.c.artifact_id == command.artifact_id,
                        sa.and_(
                            artifact_records.c.producer_kind == "control_plane",
                            artifact_records.c.producer_identity == command.producer_identity,
                            artifact_records.c.owner_operation_id == command.operation_id,
                        ),
                    )
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        if existing:
            if len(existing) != 1:
                raise _conflict(
                    code="OWNED_ARTIFACT_IDENTITY_CORRUPT",
                    message="The owner artifact identity resolves to multiple records.",
                    context={"artifactId": str(command.artifact_id)},
                    required_action="Repair the artifact identity through an owner migration.",
                )
            row = existing[0]
            mismatches = _record_mismatches(
                row,
                command,
                content_digest=content_digest,
                size_bytes=size_bytes,
                storage_reference=storage_reference,
            )
            if mismatches:
                raise _conflict(
                    code="OWNED_ARTIFACT_IDENTITY_CONFLICT",
                    message="The owner operation is already bound to different artifact content.",
                    context={
                        "artifactId": str(command.artifact_id),
                        "operationId": str(command.operation_id),
                        "mismatches": mismatches,
                    },
                    required_action=(
                        "Reuse the exact original owner operation or create a new operation ID."
                    ),
                )
            return _published(row)

        object_id = self._get_or_create_object(
            connection,
            command.artifact_kind,
            content_digest=content_digest,
            size_bytes=size_bytes,
            storage_reference=storage_reference,
            verified_at_utc=verified_at_utc,
            recorded_at_utc=recorded_at_utc,
            correlation_id=command.correlation_id,
        )
        connection.execute(
            sa.insert(artifact_records).values(
                artifact_id=command.artifact_id,
                object_id=object_id,
                upload_id=None,
                work_id=None,
                attempt_id=None,
                worker_id=None,
                producer_kind="control_plane",
                producer_identity=command.producer_identity,
                owner_operation_id=command.operation_id,
                content_type=command.content_type,
                source_policy_digest=command.source_policy_digest,
                recorded_at_utc=recorded_at_utc,
                correlation_id=command.correlation_id,
            )
        )
        return PublishedOwnedArtifact(
            artifact_id=command.artifact_id,
            operation_id=command.operation_id,
            producer_identity=command.producer_identity,
            artifact_kind=command.artifact_kind,
            content_digest=content_digest,
            size_bytes=size_bytes,
            content_type=command.content_type,
            storage_reference=storage_reference,
            recorded_at_utc=recorded_at_utc,
        )

    def _get_or_create_object(
        self,
        connection: Connection,
        artifact_kind: ArtifactKind,
        *,
        content_digest: str,
        size_bytes: int,
        storage_reference: str,
        verified_at_utc: datetime,
        recorded_at_utc: datetime,
        correlation_id: str,
    ) -> UUID:
        _advisory_lock(connection, f"artifact-object:{artifact_kind.value}:{content_digest}")
        row = (
            connection.execute(
                sa.select(artifact_objects)
                .where(
                    artifact_objects.c.artifact_kind == artifact_kind.value,
                    artifact_objects.c.content_digest == content_digest,
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            if row["size_bytes"] != size_bytes or row["storage_reference"] != storage_reference:
                raise _conflict(
                    code="ARTIFACT_OBJECT_IDENTITY_CONFLICT",
                    message="The content identity is bound to inconsistent object metadata.",
                    context={
                        "artifactKind": artifact_kind.value,
                        "contentDigest": content_digest,
                    },
                    required_action="Inspect and repair the object metadata before retrying.",
                )
            return UUID(str(row["object_id"]))
        object_id = self._uuid_factory()
        connection.execute(
            sa.insert(artifact_objects).values(
                object_id=object_id,
                artifact_kind=artifact_kind.value,
                content_digest=content_digest,
                size_bytes=size_bytes,
                storage_reference=storage_reference,
                verified_at_utc=verified_at_utc,
                recorded_at_utc=recorded_at_utc,
                correlation_id=correlation_id,
            )
        )
        return object_id

    def _transaction(self, operation: Callable[[Connection], _ResultT]) -> _ResultT:
        try:
            with self._engine.begin() as connection:
                return operation(connection)
        except OwnedArtifactPublishConflict:
            raise
        except SQLAlchemyError as exc:
            raise _conflict(
                code="OWNED_ARTIFACT_STORAGE_FAILED",
                message="The owner artifact metadata operation did not complete.",
                context={"causeType": type(exc).__name__},
                required_action=(
                    "Inspect the PostgreSQL artifact state and retry the exact owner operation."
                ),
            ) from exc

    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("owned artifact publisher clock must return timezone-aware UTC")
        return value


def _record_mismatches(
    row: RowMapping,
    command: PublishOwnedArtifact,
    *,
    content_digest: str,
    size_bytes: int,
    storage_reference: str,
) -> list[str]:
    expected: dict[str, object] = {
        "artifact_id": command.artifact_id,
        "producer_kind": "control_plane",
        "producer_identity": command.producer_identity,
        "owner_operation_id": command.operation_id,
        "content_type": command.content_type,
        "source_policy_digest": command.source_policy_digest,
        "artifact_kind": command.artifact_kind.value,
        "content_digest": content_digest,
        "size_bytes": size_bytes,
        "storage_reference": storage_reference,
    }
    return [key for key, value in expected.items() if row[key] != value]


def _published(row: RowMapping) -> PublishedOwnedArtifact:
    return PublishedOwnedArtifact(
        artifact_id=UUID(str(row["artifact_id"])),
        operation_id=UUID(str(row["owner_operation_id"])),
        producer_identity=str(row["producer_identity"]),
        artifact_kind=ArtifactKind(str(row["artifact_kind"])),
        content_digest=str(row["content_digest"]),
        size_bytes=int(row["size_bytes"]),
        content_type=str(row["content_type"]),
        storage_reference=str(row["storage_reference"]),
        recorded_at_utc=row["recorded_at_utc"],
    )


def _advisory_lock(connection: Connection, identity: str) -> None:
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": identity},
    )


def _conflict(
    *,
    code: str,
    message: str,
    context: Mapping[str, object],
    required_action: str,
) -> OwnedArtifactPublishConflict:
    return OwnedArtifactPublishConflict(
        code=code,
        message=message,
        context=context,
        required_action=required_action,
    )
