from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from collection_contracts import owner_error

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_CONTENT_TYPE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$"
)
_STORAGE_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,511}$")
_MAX_SINGLE_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024


class ArtifactKind(StrEnum):
    RAW_ARTIFACT = "raw_artifact"
    DIAGNOSTIC_ARTIFACT = "diagnostic_artifact"
    DERIVED_ARTIFACT = "derived_artifact"


@dataclass(frozen=True, slots=True)
class PrepareArtifactUpload:
    upload_id: UUID
    work_id: UUID
    lease_id: UUID
    lease_token: UUID
    worker_id: str
    input_digest: str
    artifact_kind: ArtifactKind
    expected_digest: str
    expected_size_bytes: int
    content_type: str
    expires_in_seconds: int
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("worker_id", self.worker_id)
        _require_digest("input_digest", self.input_digest)
        _require_digest("expected_digest", self.expected_digest)
        _require_size(self.expected_size_bytes)
        _require_content_type(self.content_type)
        _require_expiry(self.expires_in_seconds)
        _require_token("correlation_id", self.correlation_id)


@dataclass(frozen=True, slots=True)
class VerifyArtifactUpload:
    upload_id: UUID
    work_id: UUID
    lease_id: UUID
    lease_token: UUID
    worker_id: str
    input_digest: str
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("worker_id", self.worker_id)
        _require_digest("input_digest", self.input_digest)
        _require_token("correlation_id", self.correlation_id)


@dataclass(frozen=True, slots=True)
class PrepareArtifactRead:
    artifact_id: UUID
    work_id: UUID
    lease_id: UUID
    lease_token: UUID
    worker_id: str
    input_digest: str
    expires_in_seconds: int
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("worker_id", self.worker_id)
        _require_digest("input_digest", self.input_digest)
        _require_expiry(self.expires_in_seconds)
        _require_token("correlation_id", self.correlation_id)


@dataclass(frozen=True, slots=True)
class PreparedArtifactUpload:
    upload_id: UUID
    method: str
    url: str
    required_headers: Mapping[str, str]
    expires_at_utc: datetime

    def __post_init__(self) -> None:
        if self.method != "PUT":
            raise ValueError("prepared artifact upload method must be PUT")
        _require_url(self.url)
        _require_headers(self.required_headers)
        _require_aware_utc("expires_at_utc", self.expires_at_utc)
        object.__setattr__(self, "required_headers", MappingProxyType(dict(self.required_headers)))


@dataclass(frozen=True, slots=True)
class VerifiedArtifactUpload:
    upload_id: UUID
    work_id: UUID
    artifact_kind: ArtifactKind
    content_digest: str
    size_bytes: int
    content_type: str
    storage_reference: str
    verified_at_utc: datetime

    def __post_init__(self) -> None:
        _require_digest("content_digest", self.content_digest)
        _require_size(self.size_bytes)
        _require_content_type(self.content_type)
        if _STORAGE_REFERENCE_PATTERN.fullmatch(self.storage_reference) is None:
            raise ValueError("artifact storage reference has an invalid format")
        _require_aware_utc("verified_at_utc", self.verified_at_utc)


@dataclass(frozen=True, slots=True)
class PreparedArtifactRead:
    artifact_id: UUID
    method: str
    url: str
    expires_at_utc: datetime

    def __post_init__(self) -> None:
        if self.method != "GET":
            raise ValueError("prepared artifact read method must be GET")
        _require_url(self.url)
        _require_aware_utc("expires_at_utc", self.expires_at_utc)


class ArtifactTransferConflict(Exception):
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


class ArtifactTransferPort(Protocol):
    def prepare_upload(self, command: PrepareArtifactUpload) -> PreparedArtifactUpload: ...

    def verify_upload(self, command: VerifyArtifactUpload) -> VerifiedArtifactUpload: ...

    def prepare_read(self, command: PrepareArtifactRead) -> PreparedArtifactRead: ...


class ArtifactTransferService:
    def __init__(self, port: ArtifactTransferPort) -> None:
        self._port = port

    def prepare_upload(self, command: PrepareArtifactUpload) -> PreparedArtifactUpload:
        return self._invoke(command.correlation_id, lambda: self._port.prepare_upload(command))

    def verify_upload(self, command: VerifyArtifactUpload) -> VerifiedArtifactUpload:
        return self._invoke(command.correlation_id, lambda: self._port.verify_upload(command))

    def prepare_read(self, command: PrepareArtifactRead) -> PreparedArtifactRead:
        return self._invoke(command.correlation_id, lambda: self._port.prepare_read(command))

    @staticmethod
    def _invoke[ResultT](correlation_id: str, operation: Callable[[], ResultT]) -> ResultT:
        try:
            return operation()
        except ArtifactTransferConflict as exc:
            error_type = f"collection/{exc.code.lower().replace('_', '-')}"
            raise owner_error(
                error_type=error_type,
                owner="ArtifactTransfer",
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


def _require_content_type(value: str) -> None:
    if _CONTENT_TYPE_PATTERN.fullmatch(value) is None:
        raise ValueError("artifact content type has an invalid format")


def _require_size(value: int) -> None:
    if not 1 <= value <= _MAX_SINGLE_UPLOAD_BYTES:
        raise ValueError("artifact size is outside the supported single-upload range")


def _require_expiry(value: int) -> None:
    if not 60 <= value <= 3_600:
        raise ValueError("artifact transfer expiry must be between 60 and 3600 seconds")


def _require_url(value: str) -> None:
    if not value.startswith(("http://", "https://")) or len(value) > 8_192:
        raise ValueError("prepared artifact URL has an invalid format")


def _require_headers(headers: Mapping[str, str]) -> None:
    for name, value in headers.items():
        if not name or name.lower() != name or not value or len(name) > 128 or len(value) > 1_024:
            raise ValueError("prepared artifact upload headers have an invalid format")


def _require_aware_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
