from __future__ import annotations

import pytest

from collection_domain import (
    InvalidWorkUnitTransition,
    WorkUnitLifecycle,
    WorkUnitState,
    allowed_transitions,
)


def test_pending_work_can_be_leased_and_revision_advances() -> None:
    pending = WorkUnitLifecycle(state=WorkUnitState.PENDING, revision=3)

    leased = pending.transition(WorkUnitState.LEASED)

    assert leased == WorkUnitLifecycle(state=WorkUnitState.LEASED, revision=4)
    assert pending == WorkUnitLifecycle(state=WorkUnitState.PENDING, revision=3)


@pytest.mark.parametrize(
    "terminal_state",
    [
        WorkUnitState.SUCCEEDED,
        WorkUnitState.DEAD_LETTER,
        WorkUnitState.BLOCKED_BY_POLICY,
        WorkUnitState.CANCELLED,
        WorkUnitState.SUPERSEDED,
    ],
)
def test_terminal_state_rejects_every_transition(terminal_state: WorkUnitState) -> None:
    lifecycle = WorkUnitLifecycle(state=terminal_state, revision=1)

    for requested in WorkUnitState:
        with pytest.raises(InvalidWorkUnitTransition):
            lifecycle.transition(requested)


def test_retry_wait_returns_only_to_explicit_eligible_states() -> None:
    assert allowed_transitions(WorkUnitState.RETRY_WAIT) == frozenset(
        {
            WorkUnitState.PENDING,
            WorkUnitState.BLOCKED_BY_POLICY,
            WorkUnitState.CANCELLED,
            WorkUnitState.SUPERSEDED,
        }
    )


def test_negative_revision_is_invalid() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        WorkUnitLifecycle(state=WorkUnitState.PENDING, revision=-1)
