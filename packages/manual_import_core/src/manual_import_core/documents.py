from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Never

from pydantic import ValidationError

from collection_contracts import (
    ManualImportDisposition,
    ManualImportPlan,
    ManualImportRecordDocument,
)
from manual_import_core.planner import (
    ManualImportPlanIntegrityError,
    canonical_manual_import_plan_json,
    manual_import_record_digest,
    schedulable_manual_import_records,
    verify_manual_import_plan,
)

MAX_MANUAL_IMPORT_PLAN_BYTES = 64 * 1024 * 1024
MAX_MANUAL_IMPORT_RECORD_DOCUMENT_BYTES = 4 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_ROLE_PATTERN = re.compile(
    r"^(?:manual_source|manual_import_source):"
    r"(?P<format>csv|json|jsonl):(?P<mode>atomic|partial)$"
)
_PLAN_RECORD_ROLE_PATTERN = re.compile(
    r"^manual_import_plan_record:(?P<position>0|[1-9][0-9]{0,4})$"
)


class ManualImportPlanDocumentError(ValueError):
    def __init__(self, *, message: str, context: Mapping[str, object]) -> None:
        self.code = "MANUAL_IMPORT_PLAN_CONTRACT_INVALID"
        self.context = dict(context)
        super().__init__(message)


class ManualImportRecordDocumentError(ValueError):
    def __init__(self, *, message: str, context: Mapping[str, object]) -> None:
        self.code = "MANUAL_IMPORT_RECORD_INPUT_MISMATCH"
        self.context = dict(context)
        super().__init__(message)


def decode_canonical_manual_import_plan(
    payload: bytes,
    *,
    expected_artifact_digest: str | None = None,
    expected_plan_digest: str | None = None,
    expected_source_digest: str | None = None,
    maximum_bytes: int = MAX_MANUAL_IMPORT_PLAN_BYTES,
) -> ManualImportPlan:
    _require_optional_digest(
        "expected_artifact_digest",
        expected_artifact_digest,
        error_type=ManualImportPlanDocumentError,
    )
    _require_optional_digest(
        "expected_plan_digest",
        expected_plan_digest,
        error_type=ManualImportPlanDocumentError,
    )
    _require_optional_digest(
        "expected_source_digest",
        expected_source_digest,
        error_type=ManualImportPlanDocumentError,
    )
    _require_byte_boundary(
        payload,
        maximum_bytes=maximum_bytes,
        document_name="manual import plan",
        error_type=ManualImportPlanDocumentError,
    )

    actual_artifact_digest = _digest_bytes(payload)
    if expected_artifact_digest is not None and actual_artifact_digest != expected_artifact_digest:
        raise _plan_error(
            "The manual import plan artifact digest does not match its exact bytes.",
            reason="artifact_digest_mismatch",
            expectedDigest=expected_artifact_digest,
            actualDigest=actual_artifact_digest,
        )

    try:
        plan = ManualImportPlan.model_validate(_load_json_bytes(payload))
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
        raise _plan_error(
            "The manual import plan artifact violates the canonical contract.",
            reason="contract_validation_failed",
            causeType=type(exc).__name__,
        ) from exc

    canonical = canonical_manual_import_plan_json(plan).encode("utf-8")
    if payload != canonical:
        raise _plan_error(
            "The manual import plan artifact is not the canonical serialization.",
            reason="non_canonical_serialization",
        )
    if expected_plan_digest is not None and plan.plan_digest != expected_plan_digest:
        raise _plan_error(
            "The plan semantic digest differs from the expected work output.",
            reason="plan_digest_mismatch",
            expectedDigest=expected_plan_digest,
            actualDigest=plan.plan_digest,
        )
    if expected_source_digest is not None and plan.source_digest != expected_source_digest:
        raise _plan_error(
            "The manual import plan source digest differs from its source artifact.",
            reason="source_digest_mismatch",
            expectedDigest=expected_source_digest,
            actualDigest=plan.source_digest,
        )
    return plan


def parse_manual_import_plan_record_role(role: str) -> int:
    match = _PLAN_RECORD_ROLE_PATTERN.fullmatch(role)
    if match is None:
        raise _record_error(
            "The manual-record plan binding role is invalid.",
            reason="plan_record_role_invalid",
            actualRole=role,
        )
    return int(match.group("position"))


def materialize_manual_import_record(
    plan_payload: bytes,
    *,
    source_artifact_role: str,
    plan_record_position: int,
) -> ManualImportRecordDocument:
    plan = decode_canonical_manual_import_plan(plan_payload)
    source_match = _SOURCE_ROLE_PATTERN.fullmatch(source_artifact_role)
    if source_match is None:
        raise _record_error(
            "The manual-record source binding role is invalid.",
            reason="source_role_invalid",
            actualRole=source_artifact_role,
        )
    if source_match.group("format") != plan.format.value:
        raise _record_error(
            "The source binding format differs from the canonical plan.",
            reason="source_format_mismatch",
            expectedFormat=plan.format.value,
            actualFormat=source_match.group("format"),
        )
    if source_match.group("mode") != plan.mode.value:
        raise _record_error(
            "The source binding mode differs from the canonical plan.",
            reason="source_mode_mismatch",
            expectedMode=plan.mode.value,
            actualMode=source_match.group("mode"),
        )
    if plan.disposition is ManualImportDisposition.REJECTED:
        raise _record_error(
            "A rejected manual import plan cannot materialize record work.",
            reason="plan_rejected",
            planDigest=plan.plan_digest,
        )

    records = schedulable_manual_import_records(plan)
    if not 0 <= plan_record_position < len(records):
        raise _record_error(
            "The requested plan record position is outside the schedulable record set.",
            reason="plan_record_position_invalid",
            requestedPosition=plan_record_position,
            recordCount=len(records),
        )
    record = records[plan_record_position]
    expected_record_digest = manual_import_record_digest(
        plan.source_digest,
        record.locator,
        record.record,
    )
    if record.record_digest != expected_record_digest:
        raise _record_error(
            "The selected plan record digest differs from its canonical content.",
            reason="record_digest_mismatch",
            expectedDigest=expected_record_digest,
            actualDigest=record.record_digest,
        )

    payload = {
        "contract": "collector-manual-import-record",
        "contractRevision": "manual-import-record-v1",
        "sourceDigest": plan.source_digest,
        "sourceArtifactRole": source_artifact_role,
        "planDigest": plan.plan_digest,
        "planArtifactDigest": _digest_bytes(plan_payload),
        "planRecordPosition": plan_record_position,
        "locator": record.locator.model_dump(mode="json", by_alias=True),
        "recordDigest": record.record_digest,
        "record": record.record.model_dump(mode="json", by_alias=True),
        "materializerRevision": "manual-import-record-materializer-v1",
    }
    document = ManualImportRecordDocument.model_validate(
        {**payload, "contentDigest": _digest_json(payload)}
    )
    verify_manual_import_record_document(document)
    return document


def verify_manual_import_record_document(document: ManualImportRecordDocument) -> None:
    payload = document.model_dump(mode="json", by_alias=True)
    del payload["contentDigest"]
    expected_content_digest = _digest_json(payload)
    if document.content_digest != expected_content_digest:
        raise _record_error(
            "The manual import record digest does not match its canonical content.",
            reason="content_digest_mismatch",
            expectedDigest=expected_content_digest,
            actualDigest=document.content_digest,
        )

    expected_record_digest = manual_import_record_digest(
        document.source_digest,
        document.locator,
        document.record,
    )
    if document.record_digest != expected_record_digest:
        raise _record_error(
            "The selected manual import record digest differs from its exact source identity.",
            reason="record_digest_mismatch",
            expectedDigest=expected_record_digest,
            actualDigest=document.record_digest,
        )


def canonical_manual_import_record_json(document: ManualImportRecordDocument) -> str:
    verify_manual_import_record_document(document)
    return _canonical_json(document.model_dump(mode="json", by_alias=True)) + "\n"


def decode_canonical_manual_import_record(
    payload: bytes,
    *,
    expected_content_digest: str | None = None,
    maximum_bytes: int = MAX_MANUAL_IMPORT_RECORD_DOCUMENT_BYTES,
) -> ManualImportRecordDocument:
    _require_optional_digest(
        "expected_content_digest",
        expected_content_digest,
        error_type=ManualImportRecordDocumentError,
    )
    _require_byte_boundary(
        payload,
        maximum_bytes=maximum_bytes,
        document_name="manual import record",
        error_type=ManualImportRecordDocumentError,
    )
    try:
        document = ManualImportRecordDocument.model_validate(_load_json_bytes(payload))
        verify_manual_import_record_document(document)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ManualImportRecordDocumentError,
        ValueError,
    ) as exc:
        if isinstance(exc, ManualImportRecordDocumentError):
            raise
        raise _record_error(
            "The manual import record artifact violates the canonical contract.",
            reason="contract_validation_failed",
            causeType=type(exc).__name__,
        ) from exc

    canonical = canonical_manual_import_record_json(document).encode("utf-8")
    if payload != canonical:
        raise _record_error(
            "The manual import record artifact is not the canonical serialization.",
            reason="non_canonical_serialization",
        )
    if expected_content_digest is not None and document.content_digest != expected_content_digest:
        raise _record_error(
            "The manual import record semantic digest differs from the expected work output.",
            reason="content_digest_mismatch",
            expectedDigest=expected_content_digest,
            actualDigest=document.content_digest,
        )
    return document


def _load_json_bytes(payload: bytes) -> object:
    return json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_non_finite,
    )


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> Never:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _require_byte_boundary(
    payload: bytes,
    *,
    maximum_bytes: int,
    document_name: str,
    error_type: type[ManualImportPlanDocumentError] | type[ManualImportRecordDocumentError],
) -> None:
    if not 1 <= maximum_bytes <= 5 * 1024 * 1024 * 1024:
        raise ValueError(f"{document_name} byte limit is outside the supported range")
    if not 1 <= len(payload) <= maximum_bytes:
        raise error_type(
            message=f"The {document_name} artifact violates its byte boundary.",
            context={
                "reason": "artifact_size_invalid",
                "actualBytes": len(payload),
                "maximumBytes": maximum_bytes,
            },
        )


def _require_optional_digest(
    name: str,
    value: str | None,
    *,
    error_type: type[ManualImportPlanDocumentError] | type[ManualImportRecordDocumentError],
) -> None:
    if value is not None and _SHA256_PATTERN.fullmatch(value) is None:
        raise error_type(
            message="The manual import digest identity is invalid.",
            context={"reason": "digest_identity_invalid", "field": name},
        )


def _digest_bytes(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"


def _digest_json(value: object) -> str:
    return _digest_bytes(_canonical_json(value).encode("utf-8"))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _plan_error(
    message: str,
    *,
    reason: str,
    **context: object,
) -> ManualImportPlanDocumentError:
    return ManualImportPlanDocumentError(
        message=message,
        context={"reason": reason, **context},
    )


def _record_error(
    message: str,
    *,
    reason: str,
    **context: object,
) -> ManualImportRecordDocumentError:
    return ManualImportRecordDocumentError(
        message=message,
        context={"reason": reason, **context},
    )
