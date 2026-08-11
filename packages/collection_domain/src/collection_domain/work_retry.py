from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from collection_domain.work_units import WorkUnitState


class WorkFailureKind(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    POLICY_BLOCKED = "policy_blocked"
    CONTRACT_INVALID = "contract_invalid"


class WorkAttemptOutcome(StrEnum):
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    RELEASED = "released"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class WorkFailureDecision:
    target_state: WorkUnitState
    attempt_outcome: WorkAttemptOutcome
    retry_delay_seconds: int | None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    initial_delay_seconds: int
    multiplier: int
    max_delay_seconds: int

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("retry policy max attempts must be between 1 and 100")
        if self.initial_delay_seconds < 1:
            raise ValueError("retry policy initial delay must be positive")
        if self.multiplier < 1:
            raise ValueError("retry policy multiplier must be positive")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("retry policy max delay cannot be below initial delay")

    def decide(self, failure_kind: WorkFailureKind, attempt_number: int) -> WorkFailureDecision:
        if attempt_number < 1 or attempt_number > self.max_attempts:
            raise ValueError("attempt number is outside the retry policy")
        if failure_kind is WorkFailureKind.POLICY_BLOCKED:
            return WorkFailureDecision(
                target_state=WorkUnitState.BLOCKED_BY_POLICY,
                attempt_outcome=WorkAttemptOutcome.BLOCKED_BY_POLICY,
                retry_delay_seconds=None,
            )
        if failure_kind is not WorkFailureKind.TRANSIENT or attempt_number == self.max_attempts:
            return WorkFailureDecision(
                target_state=WorkUnitState.DEAD_LETTER,
                attempt_outcome=WorkAttemptOutcome.DEAD_LETTERED,
                retry_delay_seconds=None,
            )
        return WorkFailureDecision(
            target_state=WorkUnitState.RETRY_WAIT,
            attempt_outcome=WorkAttemptOutcome.RETRY_SCHEDULED,
            retry_delay_seconds=self._retry_delay(attempt_number),
        )

    def _retry_delay(self, attempt_number: int) -> int:
        delay = self.initial_delay_seconds
        for _ in range(attempt_number - 1):
            if delay >= self.max_delay_seconds:
                break
            delay = min(delay * self.multiplier, self.max_delay_seconds)
        return delay
