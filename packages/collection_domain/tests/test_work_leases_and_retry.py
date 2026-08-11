from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from collection_domain import (
    RetryPolicy,
    SourcePermit,
    StaleWorkLease,
    WorkAttemptOutcome,
    WorkCapability,
    WorkFailureKind,
    WorkLease,
    WorkStage,
    WorkUnitState,
)

_LEASE_ID = UUID("019c0000-0000-7000-8000-000000000001")
_WORK_ID = UUID("019c0000-0000-7000-8000-000000000002")
_LEASE_TOKEN = UUID("019c0000-0000-7000-8000-000000000003")
_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + ("a" * 64)


def _permit() -> SourcePermit:
    return SourcePermit(
        source_key="official_website",
        policy_digest=_DIGEST,
        permit_not_before_utc=_NOW,
    )


def _lease() -> WorkLease:
    return WorkLease(
        lease_id=_LEASE_ID,
        work_id=_WORK_ID,
        lease_token=_LEASE_TOKEN,
        worker_id="worker-1",
        stage=WorkStage.ACQUISITION,
        capability=WorkCapability.HTTP_FETCH,
        input_digest=_DIGEST,
        expected_output_contract="fetch-observation",
        issued_at_utc=_NOW,
        expires_at_utc=_NOW + timedelta(minutes=5),
        heartbeat_deadline_utc=_NOW + timedelta(minutes=1),
        source_permit=_permit(),
        correlation_id="correlation-1",
    )


def test_active_lease_requires_exact_identity_and_input() -> None:
    _lease().require_active(
        lease_id=_LEASE_ID,
        lease_token=_LEASE_TOKEN,
        worker_id="worker-1",
        input_digest=_DIGEST,
        now_utc=_NOW + timedelta(seconds=30),
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("lease_id", UUID("019c0000-0000-7000-8000-000000000010"), "lease_id_mismatch"),
        (
            "lease_token",
            UUID("019c0000-0000-7000-8000-000000000011"),
            "lease_token_mismatch",
        ),
        ("worker_id", "worker-2", "worker_id_mismatch"),
        ("input_digest", "sha256:" + ("b" * 64), "input_digest_mismatch"),
    ],
)
def test_lease_rejects_stale_identity(field: str, value: object, reason: str) -> None:
    values: dict[str, object] = {
        "lease_id": _LEASE_ID,
        "lease_token": _LEASE_TOKEN,
        "worker_id": "worker-1",
        "input_digest": _DIGEST,
        "now_utc": _NOW + timedelta(seconds=30),
    }
    values[field] = value

    with pytest.raises(StaleWorkLease) as raised:
        _lease().require_active(**values)  # type: ignore[arg-type]

    assert raised.value.reason == reason


def test_lease_rejects_completion_after_heartbeat_deadline() -> None:
    with pytest.raises(StaleWorkLease) as raised:
        _lease().require_active(
            lease_id=_LEASE_ID,
            lease_token=_LEASE_TOKEN,
            worker_id="worker-1",
            input_digest=_DIGEST,
            now_utc=_NOW + timedelta(minutes=1),
        )

    assert raised.value.reason == "heartbeat_overdue"


def test_lease_rejects_non_utc_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        WorkLease(
            lease_id=_LEASE_ID,
            work_id=_WORK_ID,
            lease_token=_LEASE_TOKEN,
            worker_id="worker-1",
            stage=WorkStage.ACQUISITION,
            capability=WorkCapability.HTTP_FETCH,
            input_digest=_DIGEST,
            expected_output_contract="fetch-observation",
            issued_at_utc=datetime(2026, 8, 11, 12, 0),
            expires_at_utc=_NOW + timedelta(minutes=5),
            heartbeat_deadline_utc=_NOW + timedelta(minutes=1),
            source_permit=_permit(),
            correlation_id="correlation-1",
        )


def test_lease_rejects_capability_from_another_stage() -> None:
    with pytest.raises(ValueError, match="not valid for the lease stage"):
        WorkLease(
            lease_id=_LEASE_ID,
            work_id=_WORK_ID,
            lease_token=_LEASE_TOKEN,
            worker_id="worker-1",
            stage=WorkStage.EXTRACTION,
            capability=WorkCapability.HTTP_FETCH,
            input_digest=_DIGEST,
            expected_output_contract="fetch-observation",
            issued_at_utc=_NOW,
            expires_at_utc=_NOW + timedelta(minutes=5),
            heartbeat_deadline_utc=_NOW + timedelta(minutes=1),
            source_permit=None,
            correlation_id="correlation-1",
        )


def test_source_bound_lease_requires_source_permit() -> None:
    with pytest.raises(ValueError, match="source permit does not match"):
        WorkLease(
            lease_id=_LEASE_ID,
            work_id=_WORK_ID,
            lease_token=_LEASE_TOKEN,
            worker_id="worker-1",
            stage=WorkStage.ACQUISITION,
            capability=WorkCapability.HTTP_FETCH,
            input_digest=_DIGEST,
            expected_output_contract="fetch-observation",
            issued_at_utc=_NOW,
            expires_at_utc=_NOW + timedelta(minutes=5),
            heartbeat_deadline_utc=_NOW + timedelta(minutes=1),
            source_permit=None,
            correlation_id="correlation-1",
        )


def test_processing_lease_rejects_source_permit() -> None:
    with pytest.raises(ValueError, match="source permit does not match"):
        WorkLease(
            lease_id=_LEASE_ID,
            work_id=_WORK_ID,
            lease_token=_LEASE_TOKEN,
            worker_id="worker-1",
            stage=WorkStage.EXTRACTION,
            capability=WorkCapability.EXTRACTION,
            input_digest=_DIGEST,
            expected_output_contract="extracted-record",
            issued_at_utc=_NOW,
            expires_at_utc=_NOW + timedelta(minutes=5),
            heartbeat_deadline_utc=_NOW + timedelta(minutes=1),
            source_permit=_permit(),
            correlation_id="correlation-1",
        )


def test_heartbeat_renews_both_deadlines_from_observed_time() -> None:
    renewed = _lease().renew(
        now_utc=_NOW + timedelta(seconds=30),
        lease_duration=timedelta(minutes=10),
        heartbeat_interval=timedelta(minutes=2),
    )

    assert renewed.expires_at_utc == _NOW + timedelta(minutes=10, seconds=30)
    assert renewed.heartbeat_deadline_utc == _NOW + timedelta(minutes=2, seconds=30)


def test_transient_failure_retries_with_capped_exponential_delay() -> None:
    policy = RetryPolicy(
        max_attempts=5,
        initial_delay_seconds=10,
        multiplier=3,
        max_delay_seconds=60,
    )

    first = policy.decide(WorkFailureKind.TRANSIENT, 1)
    third = policy.decide(WorkFailureKind.TRANSIENT, 3)

    assert first.retry_delay_seconds == 10
    assert third.retry_delay_seconds == 60
    assert third.target_state is WorkUnitState.RETRY_WAIT
    assert third.attempt_outcome is WorkAttemptOutcome.RETRY_SCHEDULED


def test_retry_budget_exhaustion_dead_letters() -> None:
    decision = RetryPolicy(3, 10, 2, 60).decide(WorkFailureKind.TRANSIENT, 3)

    assert decision.target_state is WorkUnitState.DEAD_LETTER
    assert decision.attempt_outcome is WorkAttemptOutcome.DEAD_LETTERED
    assert decision.retry_delay_seconds is None


def test_permanent_and_contract_failures_do_not_retry() -> None:
    policy = RetryPolicy(3, 10, 2, 60)

    for failure_kind in (WorkFailureKind.PERMANENT, WorkFailureKind.CONTRACT_INVALID):
        decision = policy.decide(failure_kind, 1)
        assert decision.target_state is WorkUnitState.DEAD_LETTER
        assert decision.retry_delay_seconds is None


def test_policy_failure_is_blocked_without_retry() -> None:
    decision = RetryPolicy(3, 10, 2, 60).decide(WorkFailureKind.POLICY_BLOCKED, 1)

    assert decision.target_state is WorkUnitState.BLOCKED_BY_POLICY
    assert decision.attempt_outcome is WorkAttemptOutcome.BLOCKED_BY_POLICY
    assert decision.retry_delay_seconds is None
