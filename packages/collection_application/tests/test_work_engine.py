from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from collection_application import (
    CollectionRunSpec,
    LeaseExpirySweep,
    LeaseExpirySweepResult,
    LeaseHeartbeat,
    LeaseRequest,
    SourceCapacitySpec,
    StageRunSpec,
    WorkCompletion,
    WorkCompletionResult,
    WorkEngineConflict,
    WorkEngineService,
    WorkerRegistration,
    WorkerRegistrationResult,
    WorkerRegistrationStatus,
    WorkFailure,
    WorkMutationResult,
    WorkRelease,
    WorkUnitSpec,
)
from collection_contracts import OwnerContextError
from collection_domain import (
    RetryPolicy,
    StageRunState,
    WorkCapability,
    WorkLease,
    WorkStage,
)

_ID1 = UUID("019c0000-0000-7000-8000-000000000001")
_ID2 = UUID("019c0000-0000-7000-8000-000000000002")
_ID3 = UUID("019c0000-0000-7000-8000-000000000003")
_DIGEST = "sha256:" + ("a" * 64)
_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


class FakePort:
    conflict: WorkEngineConflict | None = None

    def register_worker(self, command: WorkerRegistration) -> WorkerRegistrationResult:
        return WorkerRegistrationResult(command.worker_id, WorkerRegistrationStatus.REGISTERED)

    def configure_source(self, command: SourceCapacitySpec) -> None:
        del command

    def create_run(self, command: CollectionRunSpec) -> None:
        del command

    def create_stage(self, command: StageRunSpec) -> None:
        del command

    def enqueue_work(self, command: WorkUnitSpec) -> None:
        del command

    def acquire_lease(self, command: LeaseRequest) -> WorkLease | None:
        del command
        if self.conflict is not None:
            raise self.conflict
        return None

    def heartbeat(self, command: LeaseHeartbeat) -> WorkLease:
        del command
        raise AssertionError("heartbeat was not expected")

    def complete(self, command: WorkCompletion) -> WorkCompletionResult:
        del command
        raise AssertionError("completion was not expected")

    def fail(self, command: WorkFailure) -> WorkMutationResult:
        del command
        raise AssertionError("failure was not expected")

    def release(self, command: WorkRelease) -> WorkMutationResult:
        del command
        raise AssertionError("release was not expected")

    def expire_leases(self, command: LeaseExpirySweep) -> LeaseExpirySweepResult:
        del command
        return LeaseExpirySweepResult(expired_count=0, retry_wait_count=0, dead_letter_count=0)


def _work_unit(**changes: object) -> WorkUnitSpec:
    values: dict[str, object] = {
        "work_id": _ID1,
        "run_id": _ID2,
        "stage_run_id": _ID3,
        "stage": WorkStage.EXTRACTION,
        "capability": WorkCapability.EXTRACTION,
        "source_key": None,
        "semantic_key": _DIGEST,
        "input_digest": _DIGEST,
        "expected_output_contract": "extracted-record",
        "priority": 0,
        "retry_policy": RetryPolicy(3, 10, 2, 60),
        "available_at_utc": _NOW,
        "correlation_id": "correlation-1",
    }
    values.update(changes)
    return WorkUnitSpec(**values)  # type: ignore[arg-type]


def test_work_unit_requires_stage_capability_compatibility() -> None:
    with pytest.raises(ValueError, match="not valid for the stage"):
        _work_unit(capability=WorkCapability.HTTP_FETCH)


def test_source_bound_work_requires_source_key() -> None:
    with pytest.raises(ValueError, match="source key does not match"):
        _work_unit(
            stage=WorkStage.ACQUISITION,
            capability=WorkCapability.HTTP_FETCH,
            expected_output_contract="fetch-observation",
        )


def test_processing_work_rejects_source_key() -> None:
    with pytest.raises(ValueError, match="source key does not match"):
        _work_unit(source_key="official_website")


def test_work_availability_requires_utc() -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        _work_unit(available_at_utc=datetime(2026, 8, 11, 12, 0))


def test_worker_registration_requires_capability() -> None:
    with pytest.raises(ValueError, match="at least one capability"):
        WorkerRegistration(
            worker_id="worker-1",
            build_identity="build-1",
            capabilities=frozenset(),
            supported_output_contracts=frozenset({"fetch-observation"}),
            max_concurrency=1,
            resource_profile="http-small",
            correlation_id="correlation-1",
        )


def test_worker_registration_requires_output_contract() -> None:
    with pytest.raises(ValueError, match="at least one output contract"):
        WorkerRegistration(
            worker_id="worker-1",
            build_identity="build-1",
            capabilities=frozenset({WorkCapability.HTTP_FETCH}),
            supported_output_contracts=frozenset(),
            max_concurrency=1,
            resource_profile="http-small",
            correlation_id="correlation-1",
        )


def test_stage_run_requires_explicit_nonterminal_initial_state() -> None:
    with pytest.raises(ValueError, match="pending or running"):
        StageRunSpec(
            stage_run_id=_ID1,
            run_id=_ID2,
            stage=WorkStage.DISCOVERY,
            initial_state=StageRunState.SUCCEEDED,
            correlation_id="correlation-1",
        )


def test_lease_expiry_sweep_is_bounded() -> None:
    with pytest.raises(ValueError, match="between 1 and 1000"):
        LeaseExpirySweep(limit=0, correlation_id="correlation-1")


def test_lease_expiry_result_requires_complete_partition() -> None:
    with pytest.raises(ValueError, match="must equal"):
        LeaseExpirySweepResult(expired_count=2, retry_wait_count=1, dead_letter_count=0)


def test_no_eligible_work_is_valid_none_result() -> None:
    result = WorkEngineService(FakePort()).acquire_lease(
        LeaseRequest(
            worker_id="worker-1",
            capability=WorkCapability.HTTP_FETCH,
            lease_duration_seconds=300,
            heartbeat_interval_seconds=60,
            correlation_id="correlation-1",
        )
    )

    assert result is None


def test_expiry_sweep_forwards_to_owner_port() -> None:
    result = WorkEngineService(FakePort()).expire_leases(
        LeaseExpirySweep(limit=10, correlation_id="correlation-1")
    )

    assert result == LeaseExpirySweepResult(
        expired_count=0,
        retry_wait_count=0,
        dead_letter_count=0,
    )


def test_port_conflict_becomes_owner_context_error() -> None:
    port = FakePort()
    port.conflict = WorkEngineConflict(
        code="WORK_LEASE_STALE",
        message="The worker no longer owns this lease.",
        context={"workId": str(_ID1), "reason": "lease_token_mismatch"},
        required_action="Discard the result and acquire a new lease.",
    )

    with pytest.raises(OwnerContextError) as raised:
        WorkEngineService(port).acquire_lease(
            LeaseRequest(
                worker_id="worker-1",
                capability=WorkCapability.HTTP_FETCH,
                lease_duration_seconds=300,
                heartbeat_interval_seconds=60,
                correlation_id="correlation-1",
            )
        )

    assert raised.value.envelope.owner == "WorkEngine"
    assert raised.value.envelope.code == "WORK_LEASE_STALE"
    assert raised.value.envelope.correlation_id == "correlation-1"
    assert raised.value.envelope.context["reason"] == "lease_token_mismatch"
