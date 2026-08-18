from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from collection_application.pipeline_advancement import (
    ApplyPipelineAdvancement,
    ArtifactIdentity,
    ClaimPipelineAdvancement,
    PipelineAdvancementConflict,
    PipelineAdvancementLease,
    PipelineAdvancementService,
    PipelineAdvancementState,
    PipelineAdvancementStatus,
    PipelineBlocker,
    PipelineTransitionDisposition,
    PipelineTransitionRegistry,
    SucceededWorkOutput,
)
from collection_contracts import OwnerContextError
from collection_domain import WorkStage

_RUN_ID = UUID("00000000-0000-0000-0000-000000000201")
_STAGE_RUN_ID = UUID("00000000-0000-0000-0000-000000000202")
_WORK_ID = UUID("00000000-0000-0000-0000-000000000203")
_OUTPUT_ID = UUID("00000000-0000-0000-0000-000000000204")
_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000205")
_OTHER_ID = UUID("00000000-0000-0000-0000-000000000206")
_ADVANCEMENT_ID = UUID("00000000-0000-0000-0000-000000000207")
_LEASE_ID = UUID("00000000-0000-0000-0000-000000000208")
_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64


def _artifact(
    artifact_id: UUID,
    role: str,
    digest: str = _DIGEST_A,
) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_id=artifact_id,
        role=role,
        content_digest=digest,
        size_bytes=123,
        content_type="application/json",
    )


def _manual_plan_source(
    *,
    output_role: str = "manual_import_plan",
    output_digest: str = _DIGEST_B,
    input_artifacts: tuple[ArtifactIdentity, ...] | None = None,
) -> SucceededWorkOutput:
    return SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_STAGE_RUN_ID,
        stage=WorkStage.DISCOVERY,
        capability="manual_import",
        output_contract="manual-import-plan@1",
        output_digest=output_digest,
        output_artifact=_artifact(_OUTPUT_ID, output_role, output_digest),
        input_artifacts=(
            (_artifact(_SOURCE_ID, "manual_source:csv:partial"),)
            if input_artifacts is None
            else input_artifacts
        ),
    )


def _status() -> PipelineAdvancementStatus:
    return PipelineAdvancementStatus(
        advancement_id=_ADVANCEMENT_ID,
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        state=PipelineAdvancementState.PENDING,
        transition_key="manual-import-plan-admission",
        transition_plan_digest=_DIGEST_C,
        revision=0,
        attempt_count=0,
        result_digest=None,
        blocker=None,
        created_at_utc=_NOW,
        updated_at_utc=_NOW,
    )


def test_manual_import_plan_routes_to_exact_admission_owner() -> None:
    plan = PipelineTransitionRegistry().plan(_manual_plan_source())

    assert plan.transition_key == "manual-import-plan-admission"
    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert plan.blocker is None
    assert plan.plan_digest.startswith("sha256:")


def test_transition_digest_is_order_independent_but_evidence_bound() -> None:
    source = _artifact(_SOURCE_ID, "manual_source:csv:partial")
    other = _artifact(_OTHER_ID, "manual_import_schema", _DIGEST_C)
    registry = PipelineTransitionRegistry()

    first = registry.plan(_manual_plan_source(input_artifacts=(source, other)))
    reordered = registry.plan(_manual_plan_source(input_artifacts=(other, source)))
    changed = registry.plan(
        _manual_plan_source(
            input_artifacts=(
                _artifact(_SOURCE_ID, "manual_source:csv:partial", _DIGEST_B),
                other,
            )
        )
    )

    assert first.plan_digest == reordered.plan_digest
    assert first.plan_digest != changed.plan_digest


def test_manual_import_plan_requires_one_canonical_source_binding() -> None:
    registry = PipelineTransitionRegistry()

    missing = registry.plan(_manual_plan_source(input_artifacts=()))
    conflicting = registry.plan(
        _manual_plan_source(
            input_artifacts=(
                _artifact(_SOURCE_ID, "manual_source:csv:partial"),
                _artifact(_OTHER_ID, "manual_import_source:json:partial"),
            )
        )
    )

    assert missing.disposition is PipelineTransitionDisposition.BLOCK
    assert missing.blocker is not None
    assert missing.blocker.code == "PIPELINE_INPUT_ARTIFACT_MISSING"
    assert conflicting.blocker is not None
    assert conflicting.blocker.code == "PIPELINE_INPUT_ARTIFACT_CONFLICT"


def test_manual_record_remains_explicitly_blocked_without_source_owner() -> None:
    source = SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_STAGE_RUN_ID,
        stage=WorkStage.DISCOVERY,
        capability="manual_record",
        output_contract="manual-import-record@1",
        output_digest=_DIGEST_B,
        output_artifact=_artifact(_OUTPUT_ID, "manual_import_record", _DIGEST_B),
        input_artifacts=(_artifact(_SOURCE_ID, "manual_source:csv:partial"),),
    )

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "MANUAL_RECORD_DOWNSTREAM_SOURCE_UNAVAILABLE"


def test_unknown_transition_is_never_silently_accepted() -> None:
    source = SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_STAGE_RUN_ID,
        stage=WorkStage.ACQUISITION,
        capability="future_capability",
        output_contract="future-output@1",
        output_digest=_DIGEST_B,
        output_artifact=_artifact(_OUTPUT_ID, "future_output", _DIGEST_B),
        input_artifacts=(),
    )

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_TRANSITION_UNSUPPORTED"


def test_invalid_terminal_status_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="applied pipeline advancement"):
        PipelineAdvancementStatus(
            advancement_id=_ADVANCEMENT_ID,
            source_work_unit_id=_WORK_ID,
            run_id=_RUN_ID,
            state=PipelineAdvancementState.APPLIED,
            transition_key="manual-import-plan-admission",
            transition_plan_digest=_DIGEST_C,
            revision=1,
            attempt_count=1,
            result_digest=None,
            blocker=None,
            created_at_utc=_NOW,
            updated_at_utc=_NOW,
        )


def test_lease_contract_binds_exact_dagster_execution_and_expiry() -> None:
    lease = PipelineAdvancementLease(
        advancement_id=_ADVANCEMENT_ID,
        source_work_unit_id=_WORK_ID,
        lease_id=_LEASE_ID,
        lease_token="lease-token-1",
        worker_id="pipeline-supervisor-1",
        dagster_execution_id="dagster-run-1",
        dagster_build_id="build-1",
        transition_key="manual-import-plan-admission",
        transition_plan_digest=_DIGEST_C,
        revision=1,
        attempt_number=1,
        issued_at_utc=_NOW,
        expires_at_utc=_NOW + timedelta(minutes=5),
    )

    assert lease.dagster_execution_id == "dagster-run-1"
    assert lease.expires_at_utc > lease.issued_at_utc

    with pytest.raises(ValueError, match="expiry"):
        PipelineAdvancementLease(
            advancement_id=_ADVANCEMENT_ID,
            source_work_unit_id=_WORK_ID,
            lease_id=_LEASE_ID,
            lease_token="lease-token-1",
            worker_id="pipeline-supervisor-1",
            dagster_execution_id="dagster-run-1",
            dagster_build_id="build-1",
            transition_key="manual-import-plan-admission",
            transition_plan_digest=_DIGEST_C,
            revision=1,
            attempt_number=1,
            issued_at_utc=_NOW,
            expires_at_utc=_NOW,
        )


class Port:
    def __init__(self) -> None:
        self.source = None
        self.plan = None

    def register(self, source, plan, *, correlation_id):
        assert correlation_id == "pipeline-test"
        self.source = source
        self.plan = plan
        return _status()

    def claim(self, command):
        assert isinstance(command, ClaimPipelineAdvancement)
        return None

    def apply(self, command):
        assert isinstance(command, ApplyPipelineAdvancement)
        return _status()

    def block(self, command):
        raise AssertionError(command)


def test_service_registers_registry_plan_through_port() -> None:
    port = Port()

    result = PipelineAdvancementService(port).register(
        _manual_plan_source(),
        correlation_id="pipeline-test",
    )

    assert result == _status()
    assert port.source is not None
    assert port.plan is not None
    assert port.plan.disposition is PipelineTransitionDisposition.APPLY


def test_port_conflict_is_exposed_as_owner_context_error() -> None:
    class FailingPort(Port):
        def register(self, source, plan, *, correlation_id):
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_OUTPUT_CONFLICT",
                message="Source output changed.",
                context={"sourceWorkUnitId": str(source.source_work_unit_id)},
                required_action="Reload the exact successful work output.",
            )

    with pytest.raises(OwnerContextError) as captured:
        PipelineAdvancementService(FailingPort()).register(
            _manual_plan_source(),
            correlation_id="pipeline-test",
        )

    assert captured.value.envelope.owner == "PipelineAdvancement"
    assert captured.value.envelope.code == "PIPELINE_SOURCE_OUTPUT_CONFLICT"
    assert captured.value.envelope.correlation_id == "pipeline-test"


def test_blocker_context_is_immutable() -> None:
    blocker = PipelineBlocker(
        owner="PipelineAdvancement",
        code="PIPELINE_TRANSITION_UNSUPPORTED",
        message="Unsupported.",
        required_action="Add an owner.",
        context={"key": "value"},
    )

    with pytest.raises(TypeError):
        blocker.context["key"] = "changed"  # type: ignore[index]
