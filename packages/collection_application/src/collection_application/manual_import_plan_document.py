from __future__ import annotations

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from uuid import UUID

from collection_application.manual_import_admission import ManualImportPlanForAdmission
from collection_contracts import ManualImportPlan
from manual_import_core import (
    MAX_MANUAL_IMPORT_BYTES,
    ManualImportPlanIntegrityError,
    canonical_manual_import_plan_json,
    verify_manual_import_plan,
)
from pydantic import ValidationError

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class ManualImportPlanDocumentError(ValueError):
    def __init__(
        self,
        *,
        message: str,
        context: Mapping[str, object],
    ) -> None:
        self.code = "MANUAL_IMPORT_PLAN_CONTRACT_INVALID"
        self.context = dict(context)
        super().__init__(message)


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
    _require_digest("plan_artifact_digest", plan_artifact_digest)
    _require_digest("expected_plan_digest", expected_plan_digest)
    _require_digest("expected_source_digest", expected_source_digest)
    if not 1 <= len(payload) <= MAX_MANUAL_IMPORT_BYTES:
        raise _error(
            "The manual import plan artifact violates its byte boundary.",
            reason="artifact_size_invalid",
            actualBytes=len(payload),
            maximumBytes=MAX_MANUAL_IMPORT_BYTES,
        )
    actual_artifact_digest = f"sha256:{sha256(payload).hexdigest()}"
    if actual_artifact_digest != plan_artifact_digest:
        raise _error(
            "The manual import plan artifact digest does not match its exact bytes.",
            reason="artifact_digest_mismatch",
            expectedDigest=plan_artifact_digest,
            actualDigest=actual_artifact_digest,
        )

    try:
        text = payload.decode("utf-8", errors="strict")
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
        plan = ManualImportPlan.model_validate(document)
        verify_manual_import_plan(plan)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ManualImportPlanIntegrityError,
        ValueError,
    ) as exc:
        if isinstance(exc, ManualImportPlanDocumentError):
            raise
        raise _error(
            "The manual import plan artifact violates the canonical contract.",
            reason="contract_validation_failed",
            causeType=type(exc).__name__,
        ) from exc

    canonical = canonical_manual_import_plan_json(plan).encode("utf-8")
    if payload != canonical:
        raise _error(
            "The manual import plan artifact is not the canonical serialization.",
            reason="non_canonical_serialization",
        )
    if plan.plan_digest != expected_plan_digest:
        raise _error(
            "The plan semantic digest differs from successful work output.",
            reason="plan_digest_mismatch",
            expectedDigest=expected_plan_digest,
            actualDigest=plan.plan_digest,
        )
    if plan.source_digest != expected_source_digest:
        raise _error(
            "The manual import plan source digest differs from its source artifact.",
            reason="source_digest_mismatch",
            expectedDigest=expected_source_digest,
            actualDigest=plan.source_digest,
        )
    try:
        return ManualImportPlanForAdmission(
            plan_artifact_id=plan_artifact_id,
            plan_artifact_digest=plan_artifact_digest,
            source_artifact_id=source_artifact_id,
            source_artifact_role=source_artifact_role,
            plan=plan,
        )
    except ValueError as exc:
        raise _error(
            "The manual import plan artifact bindings violate the canonical contract.",
            reason="artifact_binding_invalid",
            causeType=type(exc).__name__,
        ) from exc


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _error(message: str, *, reason: str, **context: object) -> ManualImportPlanDocumentError:
    return ManualImportPlanDocumentError(
        message=message,
        context={"reason": reason, **context},
    )


def _require_digest(name: str, value: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise _error(
            "The manual import plan digest identity is invalid.",
            reason="digest_identity_invalid",
            field=name,
        )
