from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineAdvancementLease,
    PipelineAdvancementService,
    PipelineAdvancementState,
    PipelineAdvancementStatus,
    PipelineBlocker,
    SucceededWorkOutput,
)
from collection_application.pipeline_supervision import (
    PipelinePreviewBlocked,
    PipelineSupervisorService,
)
from collection_domain import WorkStage

_RUN_ID = UUID("00000000-0000-0000-0000-000000000401")
_STAGE_RUN_ID = UUID("00000000-0000-0000-0000-000000000402")
_WORK_ID = UUID("00000000-0000-0000-0000-000000000403")
_OUTPUT_ID = UUID("00000000-0000-0000-0000-000000000404")
_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000405")
_ADVANCEMENT_ID = UUID("00000000-0000-0000-0000-000000000406")
_LEASE_ID = UUID("00000000-0000-0000-0000-000000000407")
_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
_OUTPUT_DIGEST = "sha256:" + "a" * 64
_PLAN_DIGEST = "sha256:" + "b" * 64
_RESULT_DIGEST = "sha256:" + "c" * 64


def _source() -> SucceededWorkOutput:
    return SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_STAGE_RUN_ID,
        stage=WorkStage.DISCOVERY,
        capability="manual_import",
        output_contract="manual-import-plan@1",
        output_digest=_OUTPUT_DIGEST,
        output_artifact=ArtifactIdentity(
            artifact_id=_OUTPUT_ID,
            role="manual_import_plan",
            content_digest=_OUTPUT_DIGEST,
            size_bytes=10,
            content_type="application/json",
        ),
        input_artifacts=(
            ArtifactIdentity(
                artifact_id=_SOURCE_ID,
                role="manual_source:csv:accept_valid",
                content_digest=_OUTPUT_DIGEST,
                size_bytes=10,
                content_type="text/csv",
            ),
        ),
    )


def _lease() -> PipelineAdvancementLease:
    return PipelineAdvancementLease(
        advancement_id=_ADVANCEMENT_ID,
        source_work_unit_id=_WORK_ID,
        lease_id=_LEASE_ID,
        lease_token="lease-token-1",
        worker_id="pipeline-supervisor-1",
        dagster_execution_id="dagster-run-1",
        dagster_build_id="build-1",
        transition_key="manual-import-plan-admission",
        transition_plan_digest=_PLAN_DIGEST,
        revision=1,
        attempt_number=1,
        issued_at_utc=_NOW,
        expires_at_utc=_NOW + timedelta(minutes=5),
    )


def _status(
    state: PipelineAdvancementState,
    *,
    result_digest: str | None = None,
    blocker: PipelineBlocker | None = None,
) -> PipelineAdvancementStatus:
    terminal = state in {
        PipelineAdvancementState.APPLIED,
        PipelineAdvancementState.BLOCKED,
    }
    return PipelineAdvancementStatus(
        advancement_id=_ADVANCEMENT_ID,
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        state=state,
        transition_key="manual-import-plan-admission",
        transition_plan_digest=_PLAN_DIGEST,
        revision=2 if terminal else 0,
        attempt_count=1 if terminal else 0,
        result_digest=result_digest,
        blocker=blocker,
        created_at_utc=_NOW,
        updated_at_utc=_NOW,
    )


_DEFAULT_SOURCES = (_source(),)
_DEFAULT_LEASE = _lease()


class Discovery:
    def __init__(self, sources=_DEFAULT_SOURCES) -> None:
        self.sources = sources

    def list_unregistered_succeeded(self, *, limit, correlation_id):
        assert limit == 25
        assert correlation_id == "pipeline-test"
        return self.sources


class Port:
    def __init__(self, *, lease=_DEFAULT_LEASE) -> None:
        self.lease = lease
        self.applied = None
        self.blocked = None

    def register(self, source, plan, *, correlation_id):
        assert source == _source()
        assert correlation_id == "pipeline-test"
        return _status(PipelineAdvancementState.PENDING)

    def claim(self, command):
        return self.lease

    def apply(self, command):
        self.applied = command
        return _status(PipelineAdvancementState.APPLIED, result_digest=command.result_digest)

    def block(self, command):
        self.blocked = command
        return _status(PipelineAdvancementState.BLOCKED, blocker=command.blocker)


class Previewer:
    def preview_result_digest(
        self,
        source_work_unit_id,
        transition_plan_digest,
        *,
        correlation_id,
    ):
        assert source_work_unit_id == _WORK_ID
        assert transition_plan_digest == _PLAN_DIGEST
        assert correlation_id == "pipeline-test"
        return _RESULT_DIGEST


def _run(service: PipelineSupervisorService):
    return service.run_once(
        registration_limit=25,
        worker_id="pipeline-supervisor-1",
        dagster_execution_id="dagster-run-1",
        dagster_build_id="build-1",
        lease_duration=timedelta(minutes=5),
        correlation_id="pipeline-test",
    )


def test_supervisor_registers_then_applies_exact_previewed_digest() -> None:
    port = Port()
    service = PipelineSupervisorService(
        PipelineAdvancementService(port),
        Discovery(),
        {"manual-import-plan-admission": Previewer()},
    )

    tick = _run(service)

    assert tick.registered_count == 1
    assert tick.claimed_advancement_id == _ADVANCEMENT_ID
    assert tick.terminal_status is not None
    assert tick.terminal_status.state is PipelineAdvancementState.APPLIED
    assert port.applied.result_digest == _RESULT_DIGEST


def test_missing_previewer_is_terminally_visible_not_silently_skipped() -> None:
    port = Port()
    service = PipelineSupervisorService(
        PipelineAdvancementService(port),
        Discovery(),
        {},
    )

    tick = _run(service)

    assert tick.terminal_status is not None
    assert tick.terminal_status.state is PipelineAdvancementState.BLOCKED
    assert port.blocked.blocker.code == "PIPELINE_PREVIEWER_UNAVAILABLE"


def test_previewer_can_return_an_explicit_owner_blocker() -> None:
    blocker = PipelineBlocker(
        owner="ManualImportPlanAdmission",
        code="MANUAL_IMPORT_PLAN_INVALID",
        message="The plan is invalid.",
        required_action="Correct the source file and create new work.",
        context={"sourceWorkUnitId": str(_WORK_ID)},
    )

    class BlockingPreviewer(Previewer):
        def preview_result_digest(self, *args, **kwargs):
            raise PipelinePreviewBlocked(blocker)

    port = Port()
    service = PipelineSupervisorService(
        PipelineAdvancementService(port),
        Discovery(),
        {"manual-import-plan-admission": BlockingPreviewer()},
    )

    tick = _run(service)

    assert tick.terminal_status is not None
    assert tick.terminal_status.state is PipelineAdvancementState.BLOCKED
    assert port.blocked.blocker == blocker


def test_supervisor_rejects_duplicate_discovery_identities() -> None:
    service = PipelineSupervisorService(
        PipelineAdvancementService(Port(lease=None)),
        Discovery((_source(), _source())),
        {},
    )

    with pytest.raises(ValueError, match="duplicate work identities"):
        service.synchronize(limit=25, correlation_id="pipeline-test")
