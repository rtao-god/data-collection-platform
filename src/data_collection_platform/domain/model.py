"""Framework-free lifecycle owners for collection runs and leased work."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from data_collection_platform.shared.contracts import (
    ContractViolation,
    require_non_empty_text,
    require_sha256_hex,
    require_utc,
)


class DomainRuleViolation(ContractViolation):
    """A stable domain invariant or state-transition failure."""


class CollectionRunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            CollectionRunState.SUCCEEDED,
            CollectionRunState.FAILED,
            CollectionRunState.CANCELLED,
        }


class WorkUnitState(StrEnum):
    READY = "ready"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {WorkUnitState.SUCCEEDED, WorkUnitState.FAILED}


@dataclass(frozen=True, slots=True)
class CollectionRunId:
    value: UUID

    @classmethod
    def new(cls) -> CollectionRunId:
        return cls(uuid4())

    @classmethod
    def parse(cls, value: str) -> CollectionRunId:
        try:
            return cls(UUID(value))
        except ValueError as error:
            raise DomainRuleViolation(
                code="collection_run.invalid_id",
                message="Collection run id must be a UUID.",
                context={"value": value},
            ) from error

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class WorkUnitId:
    value: UUID

    @classmethod
    def new(cls) -> WorkUnitId:
        return cls(uuid4())

    @classmethod
    def parse(cls, value: str) -> WorkUnitId:
        try:
            return cls(UUID(value))
        except ValueError as error:
            raise DomainRuleViolation(
                code="work_unit.invalid_id",
                message="Work unit id must be a UUID.",
                context={"value": value},
            ) from error

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class WorkLease:
    worker_id: str
    token: str
    acquired_at: datetime
    expires_at: datetime


@dataclass(slots=True)
class CollectionRun:
    id: CollectionRunId
    campaign_id: str
    campaign_bundle_sha256: str
    created_at: datetime
    _state: CollectionRunState = field(default=CollectionRunState.CREATED, init=False, repr=False)
    started_at: datetime | None = field(default=None, init=False)
    cancellation_requested_at: datetime | None = field(default=None, init=False)
    finished_at: datetime | None = field(default=None, init=False)
    cancellation_reason: str | None = field(default=None, init=False)
    failure_code: str | None = field(default=None, init=False)
    failure_message: str | None = field(default=None, init=False)

    @classmethod
    def create(
        cls,
        *,
        run_id: CollectionRunId,
        campaign_id: str,
        campaign_bundle_sha256: str,
        at: datetime,
    ) -> CollectionRun:
        try:
            normalized_campaign_id = require_non_empty_text(
                campaign_id,
                field_name="campaign_id",
            )
            normalized_digest = require_sha256_hex(
                campaign_bundle_sha256,
                field_name="campaign_bundle_sha256",
            )
            created_at = require_utc(at, field_name="created_at")
        except ContractViolation as error:
            raise DomainRuleViolation(
                code=error.code,
                message=error.message,
                context=error.context,
            ) from error
        return cls(
            id=run_id,
            campaign_id=normalized_campaign_id,
            campaign_bundle_sha256=normalized_digest,
            created_at=created_at,
        )

    @property
    def state(self) -> CollectionRunState:
        return self._state

    def start(self, *, at: datetime) -> None:
        self._require_state(CollectionRunState.CREATED, operation="start")
        timestamp = self._require_timestamp(at, field_name="started_at")
        self.started_at = timestamp
        self._state = CollectionRunState.RUNNING

    def request_cancellation(self, *, at: datetime, reason: str) -> None:
        normalized_reason = self._require_text(reason, field_name="cancellation_reason")
        timestamp = self._require_timestamp(at, field_name="cancellation_requested_at")

        if self._state is CollectionRunState.CREATED:
            self.cancellation_requested_at = timestamp
            self.cancellation_reason = normalized_reason
            self.finished_at = timestamp
            self._state = CollectionRunState.CANCELLED
            return

        self._require_state(CollectionRunState.RUNNING, operation="request_cancellation")
        self.cancellation_requested_at = timestamp
        self.cancellation_reason = normalized_reason
        self._state = CollectionRunState.CANCELLING

    def confirm_cancelled(self, *, at: datetime) -> None:
        self._require_state(CollectionRunState.CANCELLING, operation="confirm_cancelled")
        self.finished_at = self._require_timestamp(at, field_name="finished_at")
        self._state = CollectionRunState.CANCELLED

    def succeed(self, *, at: datetime) -> None:
        self._require_state(CollectionRunState.RUNNING, operation="succeed")
        self.finished_at = self._require_timestamp(at, field_name="finished_at")
        self._state = CollectionRunState.SUCCEEDED

    def fail(self, *, at: datetime, code: str, message: str) -> None:
        if self._state not in {
            CollectionRunState.CREATED,
            CollectionRunState.RUNNING,
            CollectionRunState.CANCELLING,
        }:
            self._raise_invalid_transition(operation="fail")
        self.failure_code = self._require_text(code, field_name="failure_code")
        self.failure_message = self._require_text(message, field_name="failure_message")
        self.finished_at = self._require_timestamp(at, field_name="finished_at")
        self._state = CollectionRunState.FAILED

    def _require_state(self, expected: CollectionRunState, *, operation: str) -> None:
        if self._state is not expected:
            self._raise_invalid_transition(operation=operation, expected=expected)

    def _raise_invalid_transition(
        self,
        *,
        operation: str,
        expected: CollectionRunState | None = None,
    ) -> None:
        context: dict[str, object] = {
            "run_id": str(self.id),
            "state": self._state.value,
            "operation": operation,
        }
        if expected is not None:
            context["expected_state"] = expected.value
        raise DomainRuleViolation(
            code="collection_run.invalid_transition",
            message="Collection run state does not allow the requested transition.",
            context=context,
        )

    def _require_timestamp(self, value: datetime, *, field_name: str) -> datetime:
        try:
            timestamp = require_utc(value, field_name=field_name)
        except ContractViolation as error:
            raise DomainRuleViolation(
                code=error.code,
                message=error.message,
                context=error.context,
            ) from error
        baseline = self.started_at or self.created_at
        if timestamp < baseline:
            raise DomainRuleViolation(
                code="collection_run.non_monotonic_time",
                message="Collection run timestamps must be monotonic.",
                context={
                    "run_id": str(self.id),
                    "field": field_name,
                    "value": timestamp.isoformat(),
                    "minimum": baseline.isoformat(),
                },
            )
        return timestamp

    @staticmethod
    def _require_text(value: str, *, field_name: str) -> str:
        try:
            return require_non_empty_text(value, field_name=field_name)
        except ContractViolation as error:
            raise DomainRuleViolation(
                code=error.code,
                message=error.message,
                context=error.context,
            ) from error


@dataclass(slots=True)
class WorkUnit:
    id: WorkUnitId
    run_id: CollectionRunId
    source_id: str
    work_kind: str
    deduplication_key: str
    max_attempts: int
    created_at: datetime
    _state: WorkUnitState = field(default=WorkUnitState.READY, init=False, repr=False)
    attempt_count: int = field(default=0, init=False)
    lease: WorkLease | None = field(default=None, init=False)
    finished_at: datetime | None = field(default=None, init=False)
    failure_reason: str | None = field(default=None, init=False)

    @classmethod
    def create(
        cls,
        *,
        work_unit_id: WorkUnitId,
        run_id: CollectionRunId,
        source_id: str,
        work_kind: str,
        deduplication_key: str,
        max_attempts: int,
        at: datetime,
    ) -> WorkUnit:
        if max_attempts < 1:
            raise DomainRuleViolation(
                code="work_unit.invalid_max_attempts",
                message="Work unit max_attempts must be at least one.",
                context={"max_attempts": max_attempts},
            )
        return cls(
            id=work_unit_id,
            run_id=run_id,
            source_id=cls._require_text(source_id, field_name="source_id"),
            work_kind=cls._require_text(work_kind, field_name="work_kind"),
            deduplication_key=cls._require_text(
                deduplication_key,
                field_name="deduplication_key",
            ),
            max_attempts=max_attempts,
            created_at=cls._require_utc(at, field_name="created_at"),
        )

    @property
    def state(self) -> WorkUnitState:
        return self._state

    def acquire_lease(
        self,
        *,
        worker_id: str,
        token: str,
        at: datetime,
        duration: timedelta,
    ) -> WorkLease:
        if self._state is not WorkUnitState.READY:
            self._raise_invalid_transition(operation="acquire_lease")
        if self.attempt_count >= self.max_attempts:
            raise DomainRuleViolation(
                code="work_unit.attempt_budget_exhausted",
                message="A work unit cannot be leased after its attempt budget is exhausted.",
                context={
                    "work_unit_id": str(self.id),
                    "attempt_count": self.attempt_count,
                    "max_attempts": self.max_attempts,
                },
            )
        timestamp = self._require_timestamp(at, field_name="lease.acquired_at")
        lease_duration = self._require_positive_duration(duration)
        current_lease = WorkLease(
            worker_id=self._require_text(worker_id, field_name="worker_id"),
            token=self._require_text(token, field_name="lease_token"),
            acquired_at=timestamp,
            expires_at=timestamp + lease_duration,
        )
        self.attempt_count += 1
        self.lease = current_lease
        self._state = WorkUnitState.LEASED
        return current_lease

    def renew_lease(
        self,
        *,
        worker_id: str,
        token: str,
        at: datetime,
        duration: timedelta,
    ) -> WorkLease:
        current = self._require_active_lease(
            worker_id=worker_id,
            token=token,
            at=at,
            operation="renew_lease",
        )
        timestamp = self._require_timestamp(at, field_name="lease.renewed_at")
        lease_duration = self._require_positive_duration(duration)
        renewed = WorkLease(
            worker_id=current.worker_id,
            token=current.token,
            acquired_at=current.acquired_at,
            expires_at=timestamp + lease_duration,
        )
        self.lease = renewed
        return renewed

    def succeed(self, *, worker_id: str, token: str, at: datetime) -> None:
        self._require_active_lease(
            worker_id=worker_id,
            token=token,
            at=at,
            operation="succeed",
        )
        self.finished_at = self._require_timestamp(at, field_name="finished_at")
        self.lease = None
        self._state = WorkUnitState.SUCCEEDED

    def release_for_retry(
        self,
        *,
        worker_id: str,
        token: str,
        at: datetime,
        reason: str,
    ) -> WorkUnitState:
        self._require_active_lease(
            worker_id=worker_id,
            token=token,
            at=at,
            operation="release_for_retry",
        )
        timestamp = self._require_timestamp(at, field_name="released_at")
        normalized_reason = self._require_text(reason, field_name="failure_reason")
        return self._release_or_fail(at=timestamp, reason=normalized_reason)

    def expire_lease(self, *, at: datetime) -> WorkUnitState:
        if self._state is not WorkUnitState.LEASED or self.lease is None:
            self._raise_invalid_transition(operation="expire_lease")
        timestamp = self._require_timestamp(at, field_name="lease.expired_at")
        if timestamp < self.lease.expires_at:
            raise DomainRuleViolation(
                code="work_unit.lease_not_expired",
                message="A lease cannot be expired before its owned expiry timestamp.",
                context={
                    "work_unit_id": str(self.id),
                    "expires_at": self.lease.expires_at.isoformat(),
                    "attempted_at": timestamp.isoformat(),
                },
            )
        return self._release_or_fail(at=timestamp, reason="lease_expired")

    def fail_permanently(
        self,
        *,
        worker_id: str,
        token: str,
        at: datetime,
        reason: str,
    ) -> None:
        self._require_active_lease(
            worker_id=worker_id,
            token=token,
            at=at,
            operation="fail_permanently",
        )
        self.finished_at = self._require_timestamp(at, field_name="finished_at")
        self.failure_reason = self._require_text(reason, field_name="failure_reason")
        self.lease = None
        self._state = WorkUnitState.FAILED

    def _release_or_fail(self, *, at: datetime, reason: str) -> WorkUnitState:
        self.lease = None
        self.failure_reason = reason
        if self.attempt_count >= self.max_attempts:
            self.finished_at = at
            self._state = WorkUnitState.FAILED
        else:
            self._state = WorkUnitState.READY
        return self._state

    def _require_active_lease(
        self,
        *,
        worker_id: str,
        token: str,
        at: datetime,
        operation: str,
    ) -> WorkLease:
        if self._state is not WorkUnitState.LEASED or self.lease is None:
            self._raise_invalid_transition(operation=operation)
        timestamp = self._require_timestamp(at, field_name=f"{operation}.at")
        normalized_worker = self._require_text(worker_id, field_name="worker_id")
        normalized_token = self._require_text(token, field_name="lease_token")
        if self.lease.worker_id != normalized_worker or self.lease.token != normalized_token:
            raise DomainRuleViolation(
                code="work_unit.lease_owner_mismatch",
                message="Only the current lease owner can mutate leased work.",
                context={
                    "work_unit_id": str(self.id),
                    "worker_id": normalized_worker,
                    "operation": operation,
                },
            )
        if timestamp >= self.lease.expires_at:
            raise DomainRuleViolation(
                code="work_unit.lease_expired",
                message="An expired lease cannot mutate work.",
                context={
                    "work_unit_id": str(self.id),
                    "expires_at": self.lease.expires_at.isoformat(),
                    "attempted_at": timestamp.isoformat(),
                    "operation": operation,
                },
            )
        return self.lease

    def _raise_invalid_transition(self, *, operation: str) -> None:
        raise DomainRuleViolation(
            code="work_unit.invalid_transition",
            message="Work unit state does not allow the requested transition.",
            context={
                "work_unit_id": str(self.id),
                "state": self._state.value,
                "operation": operation,
            },
        )

    def _require_timestamp(self, value: datetime, *, field_name: str) -> datetime:
        timestamp = self._require_utc(value, field_name=field_name)
        if timestamp < self.created_at:
            raise DomainRuleViolation(
                code="work_unit.non_monotonic_time",
                message="Work unit timestamps must not precede creation.",
                context={
                    "work_unit_id": str(self.id),
                    "field": field_name,
                    "value": timestamp.isoformat(),
                    "minimum": self.created_at.isoformat(),
                },
            )
        return timestamp

    @staticmethod
    def _require_positive_duration(value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise DomainRuleViolation(
                code="work_unit.invalid_lease_duration",
                message="Lease duration must be positive.",
                context={"duration_seconds": value.total_seconds()},
            )
        return value

    @staticmethod
    def _require_text(value: str, *, field_name: str) -> str:
        try:
            return require_non_empty_text(value, field_name=field_name)
        except ContractViolation as error:
            raise DomainRuleViolation(
                code=error.code,
                message=error.message,
                context=error.context,
            ) from error

    @staticmethod
    def _require_utc(value: datetime, *, field_name: str) -> datetime:
        try:
            return require_utc(value, field_name=field_name)
        except ContractViolation as error:
            raise DomainRuleViolation(
                code=error.code,
                message=error.message,
                context=error.context,
            ) from error
