from __future__ import annotations

from uuid import UUID

from collection_application.manual_import_admission import ManualImportPlanForAdmission
from manual_import_core import (
    ManualImportPlanDocumentError,
    decode_canonical_manual_import_plan,
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
