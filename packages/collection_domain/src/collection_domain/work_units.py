from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class WorkStage(StrEnum):
    DISCOVERY = "discovery"
    ACQUISITION = "acquisition"
    EXTRACTION = "extraction"
    NORMALIZATION = "normalization"
    GEOGRAPHY = "geography"
    ENTITY_RESOLUTION = "entity_resolution"
    QUALITY = "quality"
    EXPORT = "export"


class WorkCapability(StrEnum):
    MANUAL_IMPORT = "manual_import"
    OSM_QUERY = "osm_query"
    HTTP_FETCH = "http_fetch"
    BROWSER_FETCH = "browser_fetch"
    EXTRACTION = "extraction"
    NORMALIZATION = "normalization"
    GEOGRAPHY = "geography"
    ENTITY_RESOLUTION = "entity_resolution"
    QUALITY = "quality"
    EXPORT = "export"


_STAGE_CAPABILITIES: dict[WorkStage, frozenset[WorkCapability]] = {
    WorkStage.DISCOVERY: frozenset({WorkCapability.MANUAL_IMPORT, WorkCapability.OSM_QUERY}),
    WorkStage.ACQUISITION: frozenset({WorkCapability.HTTP_FETCH, WorkCapability.BROWSER_FETCH}),
    WorkStage.EXTRACTION: frozenset({WorkCapability.EXTRACTION}),
    WorkStage.NORMALIZATION: frozenset({WorkCapability.NORMALIZATION}),
    WorkStage.GEOGRAPHY: frozenset({WorkCapability.GEOGRAPHY}),
    WorkStage.ENTITY_RESOLUTION: frozenset({WorkCapability.ENTITY_RESOLUTION}),
    WorkStage.QUALITY: frozenset({WorkCapability.QUALITY}),
    WorkStage.EXPORT: frozenset({WorkCapability.EXPORT}),
}


class WorkUnitState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


_ALLOWED_TRANSITIONS: dict[WorkUnitState, frozenset[WorkUnitState]] = {
    WorkUnitState.PENDING: frozenset(
        {
            WorkUnitState.LEASED,
            WorkUnitState.BLOCKED_BY_POLICY,
            WorkUnitState.CANCELLED,
            WorkUnitState.SUPERSEDED,
        }
    ),
    WorkUnitState.LEASED: frozenset(
        {
            WorkUnitState.PENDING,
            WorkUnitState.RETRY_WAIT,
            WorkUnitState.SUCCEEDED,
            WorkUnitState.DEAD_LETTER,
            WorkUnitState.BLOCKED_BY_POLICY,
        }
    ),
    WorkUnitState.RETRY_WAIT: frozenset(
        {
            WorkUnitState.PENDING,
            WorkUnitState.BLOCKED_BY_POLICY,
            WorkUnitState.CANCELLED,
            WorkUnitState.SUPERSEDED,
        }
    ),
    WorkUnitState.SUCCEEDED: frozenset(),
    WorkUnitState.DEAD_LETTER: frozenset(),
    WorkUnitState.BLOCKED_BY_POLICY: frozenset(),
    WorkUnitState.CANCELLED: frozenset(),
    WorkUnitState.SUPERSEDED: frozenset(),
}


class InvalidWorkUnitTransition(ValueError):
    def __init__(self, current: WorkUnitState, requested: WorkUnitState) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f"work unit cannot transition from {current.value} to {requested.value}")


@dataclass(frozen=True, slots=True)
class WorkUnitLifecycle:
    state: WorkUnitState
    revision: int

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("work unit revision cannot be negative")

    def transition(self, requested: WorkUnitState) -> WorkUnitLifecycle:
        if requested not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidWorkUnitTransition(self.state, requested)
        return replace(self, state=requested, revision=self.revision + 1)


def allowed_transitions(state: WorkUnitState) -> frozenset[WorkUnitState]:
    return _ALLOWED_TRANSITIONS[state]


def capability_belongs_to_stage(stage: WorkStage, capability: WorkCapability) -> bool:
    return capability in _STAGE_CAPABILITIES[stage]
