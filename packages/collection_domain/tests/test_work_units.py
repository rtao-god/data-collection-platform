from __future__ import annotations

import pytest

from collection_domain import (
    InvalidWorkUnitTransition,
    WorkUnitLifecycle,
    WorkUnitState,
)


def test_pending_work_can_be_leased_and_revision_advances() -> None:
    pending = WorkUnitLifecycle(state=WorkUnitState.PENDING, revision=3)

    leased = pending.transition(WorkUnitState.LEASED)

    assert leased == WorkUnitLifecycle(state=WorkUnitState.LEASED, revision=4)
    assert pending == WorkUnitLifecycle(state=WorkUnitState.PENDING, revision=3)


def test_leased_work_can_be_explicitly_released_to_pending() -> None:
    leased = WorkUnitLifecycle(state=WorkUnitState.LEASED, revision=2)

    pending = leased.transition(WorkUnitState.PENDING)

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
        with pytest.raises(InvalidWorkUnitTransition) as failure:
            lifecycle.transition(requested)
        assert (failure.value.current, failure.value.requested) == (terminal_state, requested)


def test_retry_wait_returns_only_to_explicit_eligible_states() -> None:
    """Retry-wait must never silently resume leasing or reach a succeeded terminal state."""
    for forbidden in (WorkUnitState.LEASED, WorkUnitState.SUCCEEDED, WorkUnitState.RETRY_WAIT):
        with pytest.raises(InvalidWorkUnitTransition):
            WorkUnitLifecycle(state=WorkUnitState.RETRY_WAIT, revision=1).transition(forbidden)

    for permitted in (
        WorkUnitState.PENDING,
        WorkUnitState.BLOCKED_BY_POLICY,
        WorkUnitState.CANCELLED,
        WorkUnitState.SUPERSEDED,
    ):
        assert (
            WorkUnitLifecycle(state=WorkUnitState.RETRY_WAIT, revision=1)
            .transition(permitted)
            .state
            is permitted
        )


def test_negative_revision_is_invalid() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        WorkUnitLifecycle(state=WorkUnitState.PENDING, revision=-1)
