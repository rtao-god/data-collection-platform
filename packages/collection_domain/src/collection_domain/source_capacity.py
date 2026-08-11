from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


class SourceOperationalState(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CIRCUIT_OPEN = "circuit_open"


class SourcePermitUnavailable(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"source permit is unavailable: {reason}")


@dataclass(frozen=True, slots=True)
class SourcePermit:
    source_key: str
    policy_digest: str
    permit_not_before_utc: datetime

    def __post_init__(self) -> None:
        _require_source_key(self.source_key)
        _require_digest(self.policy_digest)
        _require_aware_utc("permit_not_before_utc", self.permit_not_before_utc)


@dataclass(frozen=True, slots=True)
class SourceReservation:
    capacity: SourceCapacity
    permit: SourcePermit


@dataclass(frozen=True, slots=True)
class SourceCapacity:
    source_key: str
    state: SourceOperationalState
    policy_digest: str
    max_active_requests: int
    active_requests: int
    next_allowed_request_at_utc: datetime
    retry_after_utc: datetime | None
    revision: int

    def __post_init__(self) -> None:
        _require_source_key(self.source_key)
        _require_digest(self.policy_digest)
        if not 1 <= self.max_active_requests <= 10_000:
            raise ValueError("source max active requests must be between 1 and 10000")
        if not 0 <= self.active_requests <= self.max_active_requests:
            raise ValueError("source active requests are outside capacity")
        _require_aware_utc("next_allowed_request_at_utc", self.next_allowed_request_at_utc)
        if self.retry_after_utc is not None:
            _require_aware_utc("retry_after_utc", self.retry_after_utc)
        if self.revision < 0:
            raise ValueError("source capacity revision cannot be negative")

    def reserve(
        self,
        *,
        now_utc: datetime,
        minimum_interval: timedelta,
    ) -> SourceReservation:
        _require_aware_utc("now_utc", now_utc)
        if minimum_interval < timedelta(0):
            raise ValueError("source minimum interval cannot be negative")
        if self.state is SourceOperationalState.SUSPENDED:
            raise SourcePermitUnavailable("source_suspended")
        if self.state is SourceOperationalState.CIRCUIT_OPEN:
            raise SourcePermitUnavailable("source_circuit_open")
        if self.retry_after_utc is not None and now_utc < self.retry_after_utc:
            raise SourcePermitUnavailable("source_retry_after")
        if now_utc < self.next_allowed_request_at_utc:
            raise SourcePermitUnavailable("source_rate_limited")
        if self.active_requests >= self.max_active_requests:
            raise SourcePermitUnavailable("source_capacity_exhausted")
        return SourceReservation(
            capacity=replace(
                self,
                active_requests=self.active_requests + 1,
                next_allowed_request_at_utc=now_utc + minimum_interval,
                revision=self.revision + 1,
            ),
            permit=SourcePermit(
                source_key=self.source_key,
                policy_digest=self.policy_digest,
                permit_not_before_utc=now_utc,
            ),
        )

    def release(self) -> SourceCapacity:
        if self.active_requests == 0:
            raise ValueError("source capacity cannot release an unreserved request")
        return replace(
            self,
            active_requests=self.active_requests - 1,
            revision=self.revision + 1,
        )


def _require_source_key(value: str) -> None:
    if _SOURCE_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("source key has an invalid format")


def _require_digest(value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("source policy digest must be canonical SHA-256")


def _require_aware_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
