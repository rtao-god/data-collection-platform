from __future__ import annotations

import hashlib
from uuid import UUID

from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportPlanForAdmission,
    manual_import_admission_id,
)
from collection_contracts import ManualImportRecordDocument
from manual_import_core import (
    ManualImportPlanDocumentError,
    build_manual_import_plan,
    canonical_manual_import_plan_json,
    decode_canonical_manual_import_plan,
    materialize_manual_import_record,
)


def decode_manual_import_plan(
    payload: bytes,
    *,
    plan_artifact_id: UUID,
    plan_artifact_digest: str,
    source_artifact_id: UUID,
    source_artifact_role: str,
    expected_plan_digest: str,
    expected_source_digest: str,
) -> ManualImportPlanForAdmission:
    try:
        plan = decode_canonical_manual_import_plan(
            payload,
            expected_artifact_digest=plan_artifact_digest,
            expected_plan_digest=expected_plan_digest,
            expected_source_digest=expected_source_digest,
        )
    except ManualImportPlanDocumentError as exc:
        if exc.context.get("field") != "expected_artifact_digest":
            raise
        raise ManualImportPlanDocumentError(
            message=str(exc),
            context={**exc.context, "field": "plan_artifact_digest"},
        ) from exc
    try:
        return ManualImportPlanForAdmission(
            plan_artifact_id=plan_artifact_id,
            plan_artifact_digest=plan_artifact_digest,
            source_artifact_id=source_artifact_id,
            source_artifact_role=source_artifact_role,
            plan=plan,
        )
    except ValueError as exc:
        raise ManualImportPlanDocumentError(
            message=("The manual import plan artifact bindings violate the canonical contract."),
            context={
                "reason": "artifact_binding_invalid",
                "causeType": type(exc).__name__,
            },
        ) from exc


def manual_import_admission_command(
    payload: bytes,
    *,
    parent_work_id: UUID,
    run_id: UUID,
    correlation_id: str,
    plan_artifact_id: UUID,
    plan_artifact_digest: str,
    plan_digest: str,
    source_artifact_id: UUID,
    source_artifact_role: str,
    source_digest: str,
) -> AdmitManualImportPlan:
    """Bind one succeeded manual-import plan output to its exact admission command.

    The identity and the command are derived only from the exact artifacts the work unit
    published, so the transition planner and the transition applier cannot disagree about
    which admission the same output produces.
    """

    return AdmitManualImportPlan(
        admission_id=manual_import_admission_id(
            parent_work_id=parent_work_id,
            plan_artifact_id=plan_artifact_id,
        ),
        parent_work_id=parent_work_id,
        run_id=run_id,
        correlation_id=correlation_id,
        plan=decode_manual_import_plan(
            payload,
            plan_artifact_id=plan_artifact_id,
            plan_artifact_digest=plan_artifact_digest,
            source_artifact_id=source_artifact_id,
            source_artifact_role=source_artifact_role,
            expected_plan_digest=plan_digest,
            expected_source_digest=source_digest,
        ),
    )


def verify_manual_import_record_source_provenance(
    *,
    source_content: bytes,
    plan_content: bytes,
    plan_artifact_digest: str,
    source_artifact_role: str,
    record: ManualImportRecordDocument,
) -> None:
    if record.plan_artifact_digest != plan_artifact_digest:
        raise ValueError("manual import record plan artifact digest differs from its exact binding")
    source_digest = f"sha256:{hashlib.sha256(source_content).hexdigest()}"
    plan = decode_canonical_manual_import_plan(
        plan_content,
        expected_artifact_digest=plan_artifact_digest,
        expected_plan_digest=record.plan_digest,
        expected_source_digest=source_digest,
    )
    if plan.source_size_bytes != len(source_content):
        raise ValueError("manual import plan source size differs from the exact source bytes")

    rebuilt_plan = build_manual_import_plan(
        source_content,
        format=plan.format,
        mode=plan.mode,
        max_file_bytes=max(1, len(source_content)),
    )
    if (
        rebuilt_plan != plan
        or canonical_manual_import_plan_json(rebuilt_plan).encode("utf-8") != plan_content
    ):
        raise ValueError("manual import plan does not match parsing the exact source bytes")

    rebuilt_record = materialize_manual_import_record(
        plan_content,
        source_artifact_role=source_artifact_role,
        plan_record_position=record.plan_record_position,
    )
    if rebuilt_record != record:
        raise ValueError("manual import record does not match its exact plan record")
