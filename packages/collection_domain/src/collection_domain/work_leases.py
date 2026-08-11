from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

from collection_domain.work_units import WorkCapability, WorkStage, capability_belongs_to_stage

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")


class StaleWorkLease(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"work lease is stale: {reason}")


@dataclass(frozen=True, slots=True)
class WorkLease:
    lease_id: UUID
    work_id: UUID
    lease_token: UUID
    worker_id: str
    stage: WorkStage
    capability: WorkCapability
    input_digest: str
    expected_output_contract: str
    issued_at_utc: datetime
    expires_at_utc: datetime
    heartbeat_deadline_utc: datetime
    permit_not_before_utc: datetime | None
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("worker_id", self.worker_id)
        if not capability_belongs_to_stage(self.stage, self.capability):
            raise ValueError("work capability is not valid for the lease stage")
        _require_token("expected_output_contract", self.expected_output_contract)
        _require_token("correlation_id", self.correlation_id)
        if _SHA256_PATTERN.fullmatch(self.input_digest) is None:
            raise ValueError("work lease input digest must be canonical SHA-256")
        _require_aware_utc("issued_at_utc", self.issued_at_utc)
        _require_aware_utc("expires_at_utc", self.expires_at_utc)
        _require_aware_utc("heartbeat_deadline_utc", self.heartbeat_deadline_utc)
        if self.permit_not_before_utc is not None:
            _require_aware_utc("permit_not_before_utc", self.permit_not_before_utc)
        if self.expires_at_utc <= self.issued_at_utc:
            raise ValueError("work lease expiry must be after issuance")
        if not self.issued_at_utc < self.heartbeat_deadline_utc <= self.expires_at_utc:
            raise ValueError("heartbeat deadline must be inside the lease interval")

    def require_active(
        self,
        *,
        lease_id: UUID,
        lease_token: UUID,
        worker_id: str,
        input_digest: str,
        now_utc: datetime,
    ) -> None:
        _require_aware_utc("now_utc", now_utc)
        if lease_id != self.lease_id:
            raise StaleWorkLease("lease_id_mismatch")
        if lease_token != self.lease_token:
            raise StaleWorkLease("lease_token_mismatch")
        if worker_id != self.worker_id:
            raise StaleWorkLease("worker_id_mismatch")
        if input_digest != self.input_digest:
            raise StaleWorkLease("input_digest_mismatch")
        if now_utc >= self.expires_at_utc:
            raise StaleWorkLease("lease_expired")
        if now_utc >= self.heartbeat_deadline_utc:
            raise StaleWorkLease("heartbeat_overdue")

    def renew(
        self,
        *,
        now_utc: datetime,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
    ) -> WorkLease:
        self.require_active(
            lease_id=self.lease_id,
            lease_token=self.lease_token,
            worker_id=self.worker_id,
            input_digest=self.input_digest,
            now_utc=now_utc,
        )
        _require_positive_duration("lease_duration", lease_duration)
        _require_positive_duration("heartbeat_interval", heartbeat_interval)
        if heartbeat_interval > lease_duration:
            raise ValueError("heartbeat interval cannot exceed lease duration")
        return replace(
            self,
            expires_at_utc=now_utc + lease_duration,
            heartbeat_deadline_utc=now_utc + heartbeat_interval,
        )


def _require_token(name: str, value: str) -> None:
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid token format")


def _require_aware_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _require_positive_duration(name: str, value: timedelta) -> None:
    if value <= timedelta(0):
        raise ValueError(f"{name} must be positive")
