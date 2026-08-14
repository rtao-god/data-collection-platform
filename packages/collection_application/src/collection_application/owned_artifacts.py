from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from collection_application.artifacts import ArtifactKind
from collection_contracts import owner_error

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_CONTENT_TYPE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$"
)
_MAX_OWNED_ARTIFACT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PublishOwnedArtifact:
    artifact_id: UUID
    operation_id: UUID
    producer_identity: str
    artifact_kind: ArtifactKind
    content: bytes
    content_type: str
    source_policy_digest: str | None
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("producer_identity", self.producer_identity)
        _require_token("correlation_id", self.correlation_id)
        if not 1 <= len(self.content) <= _MAX_OWNED_ARTIFACT_BYTES:
            raise ValueError("owned artifact size is outside the supported range")
        if _CONTENT_TYPE_PATTERN.fullmatch(self.content_type) is None:
            raise ValueError("owned artifact content type has an invalid format")
        if self.source_policy_digest is not None:
            _require_digest("source_policy_digest", self.source_policy_digest)


@dataclass(frozen=True, slots=True)
class PublishedOwnedArtifact:
    artifact_id: UUID
    operation_id: UUID
    producer_identity: str
    artifact_kind: ArtifactKind
    content_digest: str
    size_bytes: int
    content_type: str
    storage_reference: str
    recorded_at_utc: datetime

    def __post_init__(self) -> None:
        _require_token("producer_identity", self.producer_identity)
        _require_digest("content_digest", self.content_digest)
        if self.size_bytes < 1:
            raise ValueError("published artifact size must be positive")
        if _CONTENT_TYPE_PATTERN.fullmatch(self.content_type) is None:
            raise ValueError("published artifact content type has an invalid format")
        if not self.storage_reference:
            raise ValueError("published artifact storage reference is required")
        if self.recorded_at_utc.tzinfo is None or self.recorded_at_utc.utcoffset() != timedelta(0):
            raise ValueError("published artifact timestamp must be timezone-aware UTC")


class OwnedArtifactPublishConflict(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: Mapping[str, object],
        required_action: str,
    ) -> None:
        self.code = code
        self.message = message
        self.context = dict(context)
        self.required_action = required_action
        super().__init__(message)


class OwnedArtifactPublisherPort(Protocol):
    def publish(self, command: PublishOwnedArtifact) -> PublishedOwnedArtifact: ...


class OwnedArtifactPublisherService:
    def __init__(self, port: OwnedArtifactPublisherPort) -> None:
        self._port = port

    def publish(self, command: PublishOwnedArtifact) -> PublishedOwnedArtifact:
        return self._invoke(command.correlation_id, lambda: self._port.publish(command))

    @staticmethod
    def _invoke[ResultT](correlation_id: str, operation: Callable[[], ResultT]) -> ResultT:
        try:
            return operation()
        except OwnedArtifactPublishConflict as exc:
            raise owner_error(
                error_type=f"collection/{exc.code.lower().replace('_', '-')}",
                owner="OwnedArtifactPublisher",
                code=exc.code,
                message=exc.message,
                context=exc.context,
                required_action=exc.required_action,
                correlation_id=correlation_id,
            ) from exc


def _require_digest(name: str, value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be canonical SHA-256")


def _require_token(name: str, value: str) -> None:
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid token format")
