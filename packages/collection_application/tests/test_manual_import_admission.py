from __future__ import annotations

from uuid import UUID

import pytest

from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportAdmissionResult,
    ManualImportAdmissionService,
    ManualImportChildWork,
    ManualImportPlanBlocked,
    ManualImportPlanForAdmission,
    ManualImportRecord,
    admission_result_digest,
)

_PLAN_DIGEST = "sha256:" + "1" * 64
_SOURCE_DIGEST = "sha256:" + "2" * 64
_RECORD_DIGEST = "sha256:" + "3" * 64
_ADMISSION_ID = UUID("00000000-0000-0000-0000-000000000101")
_PARENT_WORK_ID = UUID("00000000-0000-0000-0000-000000000102")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000103")
_PLAN_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000104")
_SOURCE_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000105")


class _RecordingStore:
    def __init__(self) -> None:
        self.children: tuple[ManualImportChildWork, ...] | None = None
        self._result: ManualImportAdmissionResult | None = None

    def admit(
        self,
        command: AdmitManualImportPlan,
        children: tuple[ManualImportChildWork, ...],
    ) -> ManualImportAdmissionResult:
        if self._result is not None:
            if self._result.plan_digest != command.plan.plan_digest:
                raise RuntimeError("conflicting plan digest")
            return ManualImportAdmissionResult(
                admission_id=self._result.admission_id,
                plan_digest=self._result.plan_digest,
                child_work_ids=self._result.child_work_ids,
                status="already_applied",
                result_digest=self._result.result_digest,
            )
        self.children = children
        child_ids = tuple(child.work_id for child in children)
        self._result = ManualImportAdmissionResult(
            admission_id=command.admission_id,
            plan_digest=command.plan.plan_digest,
            child_work_ids=child_ids,
            status="applied",
            result_digest=admission_result_digest(
                command.admission_id,
                command.plan.plan_digest,
                child_ids,
            ),
        )
        return self._result


def _record(position: int = 0) -> ManualImportRecord:
    return ManualImportRecord(
        position=position,
        locator_kind="line",
        locator_value=str(position + 1),
        record_digest=_RECORD_DIGEST,
        values={"name": "Studio", "website": None},
    )


def _command(*, status: str = "ready", records: tuple[ManualImportRecord, ...] | None = None) -> AdmitManualImportPlan:
    selected = (_record(),) if records is None and status == "ready" else (records or ())
    plan = ManualImportPlanForAdmission(
        plan_artifact_id=_PLAN_ARTIFACT_ID,
        source_artifact_id=_SOURCE_ARTIFACT_ID,
        plan_digest=_PLAN_DIGEST,
        source_digest=_SOURCE_DIGEST,
        mode="partial",
        status=status,
        total_record_count=2 if status == "ready" else 1,
        accepted_record_count=len(selected),
        rejected_record_count=(2 - len(selected)) if status == "ready" else 1,
        records=selected,
    )
    return AdmitManualImportPlan(
        admission_id=_ADMISSION_ID,
        parent_work_id=_PARENT_WORK_ID,
        run_id=_RUN_ID,
        stage_name="manual_import_admission",
        target_stage="normalization",
        target_capability="normalization",
        target_output_contract="normalized-observation@1",
        correlation_id="manual-import-admission-test",
        plan=plan,
    )


def test_ready_plan_creates_one_deterministic_child_per_accepted_record() -> None:
    store = _RecordingStore()
    service = ManualImportAdmissionService(store)

    first = service.admit(_command())
    second = service.admit(_command())

    assert first.status == "applied"
    assert second.status == "already_applied"
    assert first.child_work_ids == second.child_work_ids
    assert store.children is not None
    assert len(store.children) == 1
    child = store.children[0]
    assert child.semantic_key.startswith(f"manual-import:{_PLAN_DIGEST}:0:")
    assert child.input_digest.startswith("sha256:")
    assert b'"planArtifactId":"00000000-0000-0000-0000-000000000104"' in child.input_payload
    assert b'"sourceArtifactId":"00000000-0000-0000-0000-000000000105"' in child.input_payload


def test_blocked_plan_never_reaches_persistence() -> None:
    store = _RecordingStore()

    with pytest.raises(ManualImportPlanBlocked) as error:
        ManualImportAdmissionService(store).admit(_command(status="blocked"))

    assert error.value.code == "MANUAL_IMPORT_PLAN_BLOCKED"
    assert store.children is None


def test_record_positions_and_digests_are_unique() -> None:
    duplicate = _record()

    with pytest.raises(ValueError, match="positions must be unique"):
        _command(records=(duplicate, duplicate))
