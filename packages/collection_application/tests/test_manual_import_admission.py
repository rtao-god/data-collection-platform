from __future__ import annotations

import json
from hashlib import sha256
from uuid import UUID

import pytest
from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportAdmissionResult,
    ManualImportAdmissionService,
    ManualImportChildWork,
    ManualImportPlanForAdmission,
    ManualImportPlanRejected,
    admission_result_digest,
)
from collection_contracts import ManualImportFormat, ManualImportMode
from manual_import_core import (
    build_manual_import_plan,
    canonical_manual_import_plan_json,
)

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


def test_canonical_plan_creates_one_deterministic_manual_record_child() -> None:
    store = _RecordingStore()
    service = ManualImportAdmissionService(store)

    first = service.admit(_command(_accepted_plan()))
    second = service.admit(_command(_accepted_plan()))

    assert first.status == "applied"
    assert second.status == "already_applied"
    assert first.child_work_ids == second.child_work_ids
    assert store.children is not None
    assert len(store.children) == 1
    child = store.children[0]
    assert child.position == 0
    assert child.semantic_key.startswith("sha256:")
    assert child.input_digest.startswith("sha256:")
    payload = json.loads(child.input_payload)
    assert payload["targetStage"] == "discovery"
    assert payload["targetCapability"] == "manual_record"
    assert payload["targetOutputContract"] == "manual-import-record@1"
    assert payload["planArtifactDigest"] != payload["planDigest"]
    assert payload["sourceArtifactRole"] == "manual_import_source:json:atomic"


def test_partial_plan_schedules_only_canonical_valid_records() -> None:
    store = _RecordingStore()

    result = ManualImportAdmissionService(store).admit(_command(_partial_plan()))

    assert result.status == "applied"
    assert store.children is not None
    assert len(store.children) == 1
    assert store.children[0].record.record.display_name == "Studio A"


def test_rejected_plan_never_reaches_persistence() -> None:
    store = _RecordingStore()
    plan = _rejected_plan()

    with pytest.raises(ManualImportPlanRejected) as error:
        ManualImportAdmissionService(store).admit(_command(plan))

    assert error.value.code == "MANUAL_IMPORT_PLAN_REJECTED"
    assert error.value.context["disposition"] == "rejected"
    assert store.children is None


def test_source_artifact_role_must_match_exact_plan_format_and_mode() -> None:
    plan = _accepted_plan()

    with pytest.raises(ValueError, match="role mode differs"):
        _bound_plan(plan, source_role="manual_import_source:json:partial")


def _command(plan) -> AdmitManualImportPlan:
    return AdmitManualImportPlan(
        admission_id=_ADMISSION_ID,
        parent_work_id=_PARENT_WORK_ID,
        run_id=_RUN_ID,
        correlation_id="manual-import-admission-test",
        plan=_bound_plan(plan),
    )


def _bound_plan(plan, *, source_role: str | None = None):
    if source_role is None:
        source_role = f"manual_import_source:{plan.format.value}:{plan.mode.value}"
    artifact = canonical_manual_import_plan_json(plan).encode("utf-8")
    return ManualImportPlanForAdmission(
        plan_artifact_id=_PLAN_ARTIFACT_ID,
        plan_artifact_digest=f"sha256:{sha256(artifact).hexdigest()}",
        source_artifact_id=_SOURCE_ARTIFACT_ID,
        source_artifact_role=source_role,
        plan=plan,
    )


def _accepted_plan():
    return build_manual_import_plan(
        _json_bytes([_row("Studio A")]),
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )


def _partial_plan():
    return build_manual_import_plan(
        _json_bytes([_row("Studio A"), {"display_name": "invalid"}]),
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.PARTIAL,
    )


def _rejected_plan():
    return build_manual_import_plan(
        _json_bytes([{"display_name": "invalid"}]),
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )


def _row(name: str) -> dict[str, object]:
    return {
        "expected_entity_kind": "place",
        "display_name": name,
        "website": "https://studio.example",
        "osm_id": None,
        "reference_urls": [],
        "note": None,
        "provenance": "manual import test",
    }


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
