from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import UUID

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ArtifactCleanupPolicy:
    grace_period: timedelta
    claim_timeout: timedelta
    retry_delay: timedelta
    batch_size: int = 100
    max_attempts: int = 10

    def __post_init__(self) -> None:
        if self.grace_period < timedelta(minutes=5):
            raise ValueError("artifact cleanup grace period must be at least five minutes")
        if not timedelta(seconds=30) <= self.claim_timeout <= timedelta(hours=24):
            raise ValueError("artifact cleanup claim timeout is outside the supported range")
        if not timedelta(seconds=1) <= self.retry_delay <= timedelta(days=7):
            raise ValueError("artifact cleanup retry delay is outside the supported range")
        if not 1 <= self.batch_size <= 1_000:
            raise ValueError("artifact cleanup batch size must be between 1 and 1000")
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("artifact cleanup max attempts must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class ArtifactCleanupClaim:
    tombstone_id: UUID
    upload_id: UUID
    storage_reference: str
    attempt_count: int

    def __post_init__(self) -> None:
        if not self.storage_reference or len(self.storage_reference) > 1_024:
            raise ValueError("artifact cleanup storage reference is invalid")
        if self.attempt_count < 1:
            raise ValueError("artifact cleanup attempt count must be positive")


@dataclass(frozen=True, slots=True)
class ArtifactCleanupRunResult:
    claimed_count: int
    deleted_count: int
    retry_scheduled_count: int
    failed_count: int

    def __post_init__(self) -> None:
        values = (
            self.claimed_count,
            self.deleted_count,
            self.retry_scheduled_count,
            self.failed_count,
        )
        if any(value < 0 for value in values):
            raise ValueError("artifact cleanup result counts cannot be negative")
        if self.deleted_count + self.retry_scheduled_count + self.failed_count != self.claimed_count:
            raise ValueError("artifact cleanup result counts are inconsistent")


class ArtifactCleanupStore(Protocol):
    def claim(
        self,
        *,
        now_utc: datetime,
        policy: ArtifactCleanupPolicy,
    ) -> Sequence[ArtifactCleanupClaim]: ...

    def mark_deleted(
        self,
        claim: ArtifactCleanupClaim,
        *,
        deleted_at_utc: datetime,
    ) -> None: ...

    def mark_failed(
        self,
        claim: ArtifactCleanupClaim,
        *,
        failed_at_utc: datetime,
        retry_not_before_utc: datetime | None,
        error_code: str,
        error_digest: str,
        terminal: bool,
    ) -> None: ...


class ArtifactCleanupObjectStore(Protocol):
    def delete(self, storage_reference: str) -> None: ...


class ArtifactCleanupService:
    def __init__(
        self,
        store: ArtifactCleanupStore,
        object_store: ArtifactCleanupObjectStore,
    ) -> None:
        self._store = store
        self._object_store = object_store

    def run_once(
        self,
        *,
        now_utc: datetime,
        policy: ArtifactCleanupPolicy,
    ) -> ArtifactCleanupRunResult:
        _require_utc(now_utc)
        claims = tuple(self._store.claim(now_utc=now_utc, policy=policy))
        deleted = 0
        retry_scheduled = 0
        failed = 0
        for claim in claims:
            try:
                self._object_store.delete(claim.storage_reference)
            except Exception as exc:
                terminal = claim.attempt_count >= policy.max_attempts
                error_code = _error_code(exc)
                error_digest = f"sha256:{sha256(_error_material(exc)).hexdigest()}"
                retry_not_before = None if terminal else now_utc + policy.retry_delay
                self._store.mark_failed(
                    claim,
                    failed_at_utc=now_utc,
                    retry_not_before_utc=retry_not_before,
                    error_code=error_code,
                    error_digest=error_digest,
                    terminal=terminal,
                )
                if terminal:
                    failed += 1
                else:
                    retry_scheduled += 1
            else:
                self._store.mark_deleted(claim, deleted_at_utc=now_utc)
                deleted += 1
        return ArtifactCleanupRunResult(
            claimed_count=len(claims),
            deleted_count=deleted,
            retry_scheduled_count=retry_scheduled,
            failed_count=failed,
        )


def validate_cleanup_failure(error_code: str, error_digest: str) -> None:
    if _ERROR_CODE.fullmatch(error_code) is None:
        raise ValueError("artifact cleanup error code is invalid")
    if _DIGEST.fullmatch(error_digest) is None:
        raise ValueError("artifact cleanup error digest is invalid")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("artifact cleanup time must be timezone-aware UTC")
    if value.tzinfo is not UTC:
        value.astimezone(UTC)


def _error_code(exc: Exception) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).upper()
    normalized = re.sub(r"[^A-Z0-9_]", "_", name)[:100]
    if not normalized or normalized[0].isdigit():
        return "ARTIFACT_DELETE_FAILED"
    return normalized


def _error_material(exc: Exception) -> bytes:
    return f"{type(exc).__module__}.{type(exc).__qualname__}:{exc}".encode(
        "utf-8",
        errors="replace",
    )
