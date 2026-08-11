from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class CollectionRunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class StageRunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


_RUN_TRANSITIONS: dict[CollectionRunState, frozenset[CollectionRunState]] = {
    CollectionRunState.CREATED: frozenset(
        {CollectionRunState.RUNNING, CollectionRunState.CANCELLED, CollectionRunState.BLOCKED}
    ),
    CollectionRunState.RUNNING: frozenset(
        {
            CollectionRunState.PAUSED,
            CollectionRunState.CANCELLED,
            CollectionRunState.COMPLETED,
            CollectionRunState.BLOCKED,
        }
    ),
    CollectionRunState.PAUSED: frozenset(
        {CollectionRunState.RUNNING, CollectionRunState.CANCELLED, CollectionRunState.BLOCKED}
    ),
    CollectionRunState.CANCELLED: frozenset(),
    CollectionRunState.COMPLETED: frozenset(),
    CollectionRunState.BLOCKED: frozenset(),
}

_STAGE_TRANSITIONS: dict[StageRunState, frozenset[StageRunState]] = {
    StageRunState.PENDING: frozenset(
        {StageRunState.RUNNING, StageRunState.BLOCKED, StageRunState.CANCELLED}
    ),
    StageRunState.RUNNING: frozenset(
        {
            StageRunState.SUCCEEDED,
            StageRunState.FAILED,
            StageRunState.BLOCKED,
            StageRunState.CANCELLED,
        }
    ),
    StageRunState.SUCCEEDED: frozenset(),
    StageRunState.FAILED: frozenset(),
    StageRunState.BLOCKED: frozenset(),
    StageRunState.CANCELLED: frozenset(),
}


class InvalidRunTransition(ValueError):
    def __init__(self, current: StrEnum, requested: StrEnum) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f"run cannot transition from {current.value} to {requested.value}")


@dataclass(frozen=True, slots=True)
class CollectionRunLifecycle:
    state: CollectionRunState
    revision: int

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("collection run revision cannot be negative")

    def transition(self, requested: CollectionRunState) -> CollectionRunLifecycle:
        if requested not in _RUN_TRANSITIONS[self.state]:
            raise InvalidRunTransition(self.state, requested)
        return replace(self, state=requested, revision=self.revision + 1)


@dataclass(frozen=True, slots=True)
class StageRunLifecycle:
    state: StageRunState
    revision: int

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("stage run revision cannot be negative")

    def transition(self, requested: StageRunState) -> StageRunLifecycle:
        if requested not in _STAGE_TRANSITIONS[self.state]:
            raise InvalidRunTransition(self.state, requested)
        return replace(self, state=requested, revision=self.revision + 1)
