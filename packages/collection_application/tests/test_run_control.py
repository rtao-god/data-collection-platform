from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from collection_application.run_control import (
    CollectionRunStatus,
    RunControlConflict,
    RunControlService,
    RunCoverageBlocker,
    StageCoverage,
    StageRunStatus,
    WorkStateCount,
    coverage_from_status,
)

from collection_contracts import OwnerContextError
from collection_domain import CollectionRunState, StageRunState, WorkStage, WorkUnitState

_RUN_ID = UUID("00000000-0000-0000-0000-000000000101")
_STAGE_ID = UUID("00000000-0000-0000-0000-000000000102")
_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + "a" * 64


def _status(state: CollectionRunState = CollectionRunState.RUNNING) -> CollectionRunStatus:
    return CollectionRunStatus(
        run_id=_RUN_ID,
        campaign_key="berlin_recording_services",
        config_bundle_digest=_DIGEST,
        state=state,
        revision=0,
        created_at_utc=_NOW,
        updated_at_utc=_NOW,
        stages=(
            StageRunStatus(
                stage_run_id=_STAGE_ID,
                stage=WorkStage.DISCOVERY,
                state=StageRunState.RUNNING,
                revision=0,
                work_counts=(
                    WorkStateCount(WorkUnitState.SUCCEEDED, 2),
                    WorkStateCount(WorkUnitState.PENDING, 1),
                ),
            ),
        ),
    )


class Port:
    def __init__(self) -> None:
        self.command = None

    def get(self, run_id):
        assert run_id == _RUN_ID
        return _status()

    def coverage(self, run_id):
        return coverage_from_status(self.get(run_id))

    def transition(self, command):
        self.command = command
        return _status(command.requested_state)


def test_coverage_is_derived_from_exact_work_states() -> None:
    report = coverage_from_status(_status())

    assert report.total == 3
    assert report.terminal == 2
    assert report.succeeded == 2
    assert report.blockers == ()
    assert report.stages == (
        StageCoverage(
            stage=WorkStage.DISCOVERY,
            total=3,
            pending=1,
            leased=0,
            retry_wait=0,
            succeeded=2,
            dead_letter=0,
            blocked_by_policy=0,
            cancelled=0,
            superseded=0,
        ),
    )


def test_coverage_exposes_terminal_owner_blockers() -> None:
    stage = _status().stages[0]
    status = CollectionRunStatus(
        run_id=_RUN_ID,
        campaign_key="berlin_recording_services",
        config_bundle_digest=_DIGEST,
        state=CollectionRunState.BLOCKED,
        revision=3,
        created_at_utc=_NOW,
        updated_at_utc=_NOW,
        stages=(
            StageRunStatus(
                stage_run_id=stage.stage_run_id,
                stage=stage.stage,
                state=StageRunState.FAILED,
                revision=1,
                work_counts=(
                    WorkStateCount(WorkUnitState.DEAD_LETTER, 2),
                    WorkStateCount(WorkUnitState.BLOCKED_BY_POLICY, 1),
                ),
            ),
        ),
    )

    report = coverage_from_status(status)

    assert report.blockers == (
        RunCoverageBlocker(
            code="RUN_BLOCKED",
            stage=None,
            count=1,
            message="The collection run is blocked by an unresolved owner condition.",
            required_action="Resolve the recorded owner blocker before creating replacement work.",
        ),
        RunCoverageBlocker(
            code="STAGE_FAILED",
            stage=WorkStage.DISCOVERY,
            count=1,
            message="The stage terminated with an owner-classified failure.",
            required_action=(
                "Inspect the stage failure and explicitly create replacement work after resolution."
            ),
        ),
        RunCoverageBlocker(
            code="WORK_DEAD_LETTERED",
            stage=WorkStage.DISCOVERY,
            count=2,
            message="Work exhausted its retry budget and entered the dead-letter state.",
            required_action=(
                "Inspect the classified failures and explicitly reprocess or resolve them."
            ),
        ),
        RunCoverageBlocker(
            code="WORK_BLOCKED_BY_POLICY",
            stage=WorkStage.DISCOVERY,
            count=1,
            message="Work is terminally blocked by the active collection policy.",
            required_action=(
                "Review the source policy and publish a valid new policy revision before creating "
                "replacement work."
            ),
        ),
    )


def test_pause_builds_revisioned_operator_transition() -> None:
    port = Port()
    service = RunControlService(port)

    result = service.pause(
        _RUN_ID,
        expected_revision=4,
        actor_id="operator-1",
        reason="Scheduled maintenance.",
        correlation_id="run-control-test",
    )

    assert result.state is CollectionRunState.PAUSED
    assert port.command.expected_revision == 4
    assert port.command.actor_id == "operator-1"
    assert port.command.requested_state is CollectionRunState.PAUSED


def test_port_conflict_is_exposed_as_owner_context_error() -> None:
    class FailingPort(Port):
        def get(self, run_id):
            raise RunControlConflict(
                code="RUN_NOT_FOUND",
                message="Missing.",
                context={"runId": str(run_id)},
                required_action="Select an existing run.",
            )

    with pytest.raises(OwnerContextError) as captured:
        RunControlService(FailingPort()).get(_RUN_ID, correlation_id="run-control-test")

    assert captured.value.envelope.owner == "RunControl"
    assert captured.value.envelope.code == "RUN_NOT_FOUND"
    assert captured.value.envelope.correlation_id == "run-control-test"
