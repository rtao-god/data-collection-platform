from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from data_collection_platform.domain.model import (
    CollectionRun,
    CollectionRunId,
    CollectionRunState,
    DomainRuleViolation,
    WorkUnit,
    WorkUnitId,
    WorkUnitState,
)


class CollectionRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.created_at = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)
        self.run = CollectionRun.create(
            run_id=CollectionRunId.parse("11111111-1111-4111-8111-111111111111"),
            campaign_id="example-city-studios",
            campaign_bundle_sha256="a" * 64,
            at=self.created_at,
        )

    def test_successful_lifecycle_is_monotonic(self) -> None:
        started_at = self.created_at + timedelta(seconds=1)
        finished_at = started_at + timedelta(minutes=4)

        self.run.start(at=started_at)
        self.run.succeed(at=finished_at)

        self.assertEqual(CollectionRunState.SUCCEEDED, self.run.state)
        self.assertEqual(started_at, self.run.started_at)
        self.assertEqual(finished_at, self.run.finished_at)
        self.assertTrue(self.run.state.is_terminal)

    def test_cancellation_before_start_is_immediately_terminal(self) -> None:
        cancelled_at = self.created_at + timedelta(seconds=1)

        self.run.request_cancellation(at=cancelled_at, reason="operator_request")

        self.assertEqual(CollectionRunState.CANCELLED, self.run.state)
        self.assertEqual(cancelled_at, self.run.cancellation_requested_at)
        self.assertEqual(cancelled_at, self.run.finished_at)

    def test_terminal_run_rejects_another_transition_with_typed_code(self) -> None:
        self.run.start(at=self.created_at + timedelta(seconds=1))
        self.run.succeed(at=self.created_at + timedelta(seconds=2))

        with self.assertRaises(DomainRuleViolation) as captured:
            self.run.fail(
                at=self.created_at + timedelta(seconds=3),
                code="late_failure",
                message="must not replace terminal truth",
            )

        self.assertEqual("collection_run.invalid_transition", captured.exception.code)

    def test_naive_domain_time_is_rejected_instead_of_normalized(self) -> None:
        with self.assertRaises(DomainRuleViolation) as captured:
            CollectionRun.create(
                run_id=CollectionRunId.new(),
                campaign_id="example",
                campaign_bundle_sha256="b" * 64,
                at=datetime(2026, 1, 2, 10, 0),
            )

        self.assertEqual("contract.datetime_not_utc", captured.exception.code)


class WorkUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.created_at = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)
        self.work = WorkUnit.create(
            work_unit_id=WorkUnitId.parse("22222222-2222-4222-8222-222222222222"),
            run_id=CollectionRunId.parse("11111111-1111-4111-8111-111111111111"),
            source_id="example.http",
            work_kind="fetch_document",
            deduplication_key="https://example.org/studio/1",
            max_attempts=2,
            at=self.created_at,
        )

    def test_only_current_lease_owner_can_complete_work(self) -> None:
        acquired_at = self.created_at + timedelta(seconds=1)
        self.work.acquire_lease(
            worker_id="worker-a",
            token="lease-1",
            at=acquired_at,
            duration=timedelta(minutes=1),
        )

        with self.assertRaises(DomainRuleViolation) as captured:
            self.work.succeed(
                worker_id="worker-b",
                token="lease-1",
                at=acquired_at + timedelta(seconds=1),
            )

        self.assertEqual("work_unit.lease_owner_mismatch", captured.exception.code)
        self.assertEqual(WorkUnitState.LEASED, self.work.state)

    def test_expired_lease_cannot_commit_success(self) -> None:
        acquired_at = self.created_at + timedelta(seconds=1)
        self.work.acquire_lease(
            worker_id="worker-a",
            token="lease-1",
            at=acquired_at,
            duration=timedelta(seconds=10),
        )

        with self.assertRaises(DomainRuleViolation) as captured:
            self.work.succeed(
                worker_id="worker-a",
                token="lease-1",
                at=acquired_at + timedelta(seconds=10),
            )

        self.assertEqual("work_unit.lease_expired", captured.exception.code)

    def test_attempt_budget_turns_expired_retry_into_explicit_failure(self) -> None:
        first_acquired = self.created_at + timedelta(seconds=1)
        self.work.acquire_lease(
            worker_id="worker-a",
            token="lease-1",
            at=first_acquired,
            duration=timedelta(seconds=10),
        )
        first_state = self.work.expire_lease(at=first_acquired + timedelta(seconds=10))
        self.assertEqual(WorkUnitState.READY, first_state)

        second_acquired = first_acquired + timedelta(seconds=11)
        self.work.acquire_lease(
            worker_id="worker-b",
            token="lease-2",
            at=second_acquired,
            duration=timedelta(seconds=10),
        )
        final_state = self.work.expire_lease(at=second_acquired + timedelta(seconds=10))

        self.assertEqual(WorkUnitState.FAILED, final_state)
        self.assertEqual("lease_expired", self.work.failure_reason)
        self.assertEqual(2, self.work.attempt_count)
        self.assertTrue(self.work.state.is_terminal)

    def test_lease_renewal_preserves_owner_and_original_acquisition(self) -> None:
        acquired_at = self.created_at + timedelta(seconds=1)
        original = self.work.acquire_lease(
            worker_id="worker-a",
            token="lease-1",
            at=acquired_at,
            duration=timedelta(seconds=10),
        )
        renewed_at = acquired_at + timedelta(seconds=5)

        renewed = self.work.renew_lease(
            worker_id="worker-a",
            token="lease-1",
            at=renewed_at,
            duration=timedelta(seconds=30),
        )

        self.assertEqual(original.acquired_at, renewed.acquired_at)
        self.assertEqual(renewed_at + timedelta(seconds=30), renewed.expires_at)
        self.assertEqual("worker-a", renewed.worker_id)


if __name__ == "__main__":
    unittest.main()
