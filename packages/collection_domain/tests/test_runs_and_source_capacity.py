from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from collection_domain import (
    CollectionRunLifecycle,
    CollectionRunState,
    InvalidRunTransition,
    SourceCapacity,
    SourceOperationalState,
    SourcePermitUnavailable,
)

_NOW = datetime(2026, 8, 11, tzinfo=UTC)
_DIGEST = "sha256:" + ("a" * 64)


def _capacity(**changes: object) -> SourceCapacity:
    values: dict[str, object] = {
        "source_key": "official_website",
        "state": SourceOperationalState.ACTIVE,
        "policy_digest": _DIGEST,
        "max_active_requests": 2,
        "active_requests": 0,
        "next_allowed_request_at_utc": _NOW,
        "retry_after_utc": None,
        "revision": 0,
    }
    values.update(changes)
    return SourceCapacity(**values)  # type: ignore[arg-type]


def test_run_pause_resume_is_explicit_and_revisioned() -> None:
    running = CollectionRunLifecycle(CollectionRunState.RUNNING, revision=3)

    paused = running.transition(CollectionRunState.PAUSED)
    resumed = paused.transition(CollectionRunState.RUNNING)

    assert paused.revision == 4
    assert resumed == CollectionRunLifecycle(CollectionRunState.RUNNING, revision=5)


def test_terminal_run_cannot_resume() -> None:
    with pytest.raises(InvalidRunTransition):
        CollectionRunLifecycle(CollectionRunState.COMPLETED, revision=1).transition(
            CollectionRunState.RUNNING
        )


def test_source_capacity_reserve_emits_policy_bound_permit() -> None:
    reservation = _capacity().reserve(
        now_utc=_NOW,
        minimum_interval=timedelta(milliseconds=500),
    )

    assert reservation.capacity.active_requests == 1
    assert reservation.capacity.next_allowed_request_at_utc == _NOW + timedelta(milliseconds=500)
    assert reservation.capacity.revision == 1
    assert reservation.permit.source_key == "official_website"
    assert reservation.permit.policy_digest == _DIGEST
    assert reservation.permit.permit_not_before_utc == _NOW


def test_source_capacity_release_is_explicit_and_revisioned() -> None:
    reserved = _capacity().reserve(now_utc=_NOW, minimum_interval=timedelta(0)).capacity

    released = reserved.release()

    assert released.active_requests == 0
    assert released.revision == 2


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"state": SourceOperationalState.SUSPENDED}, "source_suspended"),
        ({"state": SourceOperationalState.CIRCUIT_OPEN}, "source_circuit_open"),
        ({"retry_after_utc": _NOW + timedelta(minutes=1)}, "source_retry_after"),
        ({"next_allowed_request_at_utc": _NOW + timedelta(seconds=1)}, "source_rate_limited"),
        ({"active_requests": 2}, "source_capacity_exhausted"),
    ],
)
def test_source_permit_failures_are_typed(changes: dict[str, object], reason: str) -> None:
    with pytest.raises(SourcePermitUnavailable) as raised:
        _capacity(**changes).reserve(now_utc=_NOW, minimum_interval=timedelta(0))

    assert raised.value.reason == reason


def test_source_capacity_cannot_release_unreserved_slot() -> None:
    with pytest.raises(ValueError, match="unreserved"):
        _capacity().release()
