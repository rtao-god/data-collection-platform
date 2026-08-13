from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import cast
from uuid import UUID

from collection_application.manual_import_admission import (
    ManualImportPlanForAdmission,
    ManualImportRecord,
)

_MAX_PLAN_BYTES = 16 * 1024 * 1024
_MISSING = object()


class ManualImportPlanDocumentError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.context = dict(context or {})
        super().__init__(message)


def decode_manual_import_plan(
    payload: bytes,
    *,
    plan_artifact_id: UUID,
    source_artifact_id: UUID,
    expected_plan_digest: str,
    expected_source_digest: str,
) -> ManualImportPlanForAdmission:
    if len(payload) > _MAX_PLAN_BYTES:
        raise _error(
            "MANUAL_IMPORT_PLAN_TOO_LARGE",
            "The manual import plan artifact exceeds the byte limit.",
            actualSizeBytes=len(payload),
            maximumSizeBytes=_MAX_PLAN_BYTES,
        )
    actual_digest = f"sha256:{sha256(payload).hexdigest()}"
    if actual_digest != expected_plan_digest:
        raise _error(
            "MANUAL_IMPORT_PLAN_DIGEST_MISMATCH",
            "The manual import plan bytes do not match artifact metadata.",
            expectedDigest=expected_plan_digest,
            actualDigest=actual_digest,
        )
    try:
        decoded = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _error(
            "MANUAL_IMPORT_PLAN_ENCODING_INVALID",
            "The manual import plan is not valid UTF-8.",
            byteOffset=exc.start,
        ) from exc
    try:
        document = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ManualImportPlanDocumentError):
            raise
        raise _error(
            "MANUAL_IMPORT_PLAN_JSON_INVALID",
            "The manual import plan is not valid canonical JSON.",
            causeType=type(exc).__name__,
        ) from exc
    root = _object(document, "document")
    embedded_digest = _optional_string(root, "planDigest", "plan_digest", "digest")
    if embedded_digest is not None and embedded_digest != expected_plan_digest:
        raise _error(
            "MANUAL_IMPORT_PLAN_EMBEDDED_DIGEST_MISMATCH",
            "The embedded plan digest differs from artifact metadata.",
            embeddedDigest=embedded_digest,
            expectedDigest=expected_plan_digest,
        )
    embedded_source = _optional_string(
        root,
        "sourceDigest",
        "source_digest",
        "sourceContentDigest",
        "source_content_digest",
    )
    if embedded_source is not None and embedded_source != expected_source_digest:
        raise _error(
            "MANUAL_IMPORT_SOURCE_DIGEST_MISMATCH",
            "The plan names a different source artifact digest.",
            embeddedDigest=embedded_source,
            expectedDigest=expected_source_digest,
        )
    embedded_source_id = _optional_string(
        root,
        "sourceArtifactId",
        "source_artifact_id",
    )
    if embedded_source_id is not None:
        try:
            parsed_source_id = UUID(embedded_source_id)
        except ValueError as exc:
            raise _error(
                "MANUAL_IMPORT_SOURCE_ID_INVALID",
                "The embedded source artifact ID is invalid.",
                actualValue=embedded_source_id,
            ) from exc
        if parsed_source_id != source_artifact_id:
            raise _error(
                "MANUAL_IMPORT_SOURCE_ID_MISMATCH",
                "The plan names a different source artifact.",
                embeddedArtifactId=embedded_source_id,
                expectedArtifactId=str(source_artifact_id),
            )
    mode = _required_string(root, "mode", "importMode", "import_mode")
    status = _required_string(root, "status", "planStatus", "plan_status")
    records_value = _required(root, "records", "acceptedRecords", "accepted_records")
    if not isinstance(records_value, list):
        raise _error(
            "MANUAL_IMPORT_PLAN_RECORDS_INVALID",
            "The plan records field must be an array.",
        )
    records = tuple(
        _decode_record(value, fallback_position=index) for index, value in enumerate(records_value)
    )
    accepted_count = _optional_integer(
        root,
        "acceptedRecordCount",
        "accepted_record_count",
        "acceptedCount",
        "accepted_count",
    )
    if accepted_count is None:
        accepted_count = len(records)
    rejected_count = _optional_integer(
        root,
        "rejectedRecordCount",
        "rejected_record_count",
        "rejectedCount",
        "rejected_count",
    )
    issue_count = _optional_integer(root, "issueCount", "issue_count")
    if rejected_count is None:
        rejected_count = issue_count or 0
    total_count = _optional_integer(
        root,
        "totalRecordCount",
        "total_record_count",
        "recordCount",
        "record_count",
    )
    if total_count is None:
        total_count = accepted_count + rejected_count
    if accepted_count != len(records):
        raise _error(
            "MANUAL_IMPORT_PLAN_ACCEPTED_COUNT_MISMATCH",
            "The accepted record count differs from the records array.",
            acceptedRecordCount=accepted_count,
            actualRecordCount=len(records),
        )
    return ManualImportPlanForAdmission(
        plan_artifact_id=plan_artifact_id,
        source_artifact_id=source_artifact_id,
        plan_digest=expected_plan_digest,
        source_digest=expected_source_digest,
        mode=mode,
        status=status,
        total_record_count=total_count,
        accepted_record_count=accepted_count,
        rejected_record_count=rejected_count,
        records=records,
    )


def _decode_record(value: object, *, fallback_position: int) -> ManualImportRecord:
    record = _object(value, "record")
    position = _optional_integer(record, "position", "index", "ordinal")
    if position is None:
        position = fallback_position
    digest = _required_string(record, "recordDigest", "record_digest", "digest")
    locator_value = _required(record, "locator", "sourceLocator", "source_locator")
    if isinstance(locator_value, Mapping):
        locator = cast(Mapping[str, object], locator_value)
        locator_kind = _required_string(locator, "kind", "type")
        locator_text = _required_string(locator, "value", "locator")
    elif isinstance(locator_value, (str, int)) and not isinstance(locator_value, bool):
        locator_kind = "record"
        locator_text = str(locator_value)
    else:
        raise _error(
            "MANUAL_IMPORT_RECORD_LOCATOR_INVALID",
            "A record locator must be an object or scalar identity.",
            position=position,
        )
    values_value = _required(record, "values", "record", "data")
    values_object = _object(values_value, "record values")
    values: dict[str, str | None] = {}
    for key, field_value in values_object.items():
        if field_value is None:
            values[key] = None
        elif isinstance(field_value, str):
            values[key] = field_value
        else:
            raise _error(
                "MANUAL_IMPORT_RECORD_VALUE_INVALID",
                "Canonical plan record values must be strings or null.",
                position=position,
                field=key,
                actualType=type(field_value).__name__,
            )
    return ManualImportRecord(
        position=position,
        locator_kind=locator_kind,
        locator_value=locator_text,
        record_digest=digest,
        values=values,
    )


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _error(
                "MANUAL_IMPORT_PLAN_DUPLICATE_KEY",
                "The plan contains a duplicate JSON object key.",
                key=key,
            )
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise _error(
        "MANUAL_IMPORT_PLAN_NON_FINITE_NUMBER",
        "The plan contains a non-finite JSON number.",
        actualValue=value,
    )


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise _error(
            "MANUAL_IMPORT_PLAN_OBJECT_INVALID",
            f"The {name} must be a JSON object with string keys.",
        )
    return cast(Mapping[str, object], value)


def _required(mapping: Mapping[str, object], *names: str) -> object:
    value = _lookup(mapping, names)
    if value is _MISSING:
        raise _error(
            "MANUAL_IMPORT_PLAN_FIELD_MISSING",
            "The manual import plan is missing a required field.",
            acceptedNames=list(names),
        )
    return value


def _required_string(mapping: Mapping[str, object], *names: str) -> str:
    value = _required(mapping, *names)
    if not isinstance(value, str) or not value:
        raise _error(
            "MANUAL_IMPORT_PLAN_STRING_INVALID",
            "The manual import plan field must be a non-empty string.",
            acceptedNames=list(names),
        )
    return value


def _optional_string(mapping: Mapping[str, object], *names: str) -> str | None:
    value = _lookup(mapping, names)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _error(
            "MANUAL_IMPORT_PLAN_STRING_INVALID",
            "The manual import plan field must be a non-empty string.",
            acceptedNames=list(names),
        )
    return value


def _optional_integer(mapping: Mapping[str, object], *names: str) -> int | None:
    value = _lookup(mapping, names)
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error(
            "MANUAL_IMPORT_PLAN_INTEGER_INVALID",
            "The manual import plan count must be a non-negative integer.",
            acceptedNames=list(names),
        )
    return value


def _lookup(mapping: Mapping[str, object], names: Sequence[str]) -> object:
    found = [(name, mapping[name]) for name in names if name in mapping]
    if not found:
        return _MISSING
    first_name, first_value = found[0]
    conflicts = [name for name, value in found[1:] if value != first_value]
    if conflicts:
        raise _error(
            "MANUAL_IMPORT_PLAN_ALIAS_CONFLICT",
            "Equivalent plan field aliases contain different values.",
            firstAlias=first_name,
            conflictingAliases=conflicts,
        )
    return first_value


def _error(code: str, message: str, **context: object) -> ManualImportPlanDocumentError:
    return ManualImportPlanDocumentError(code=code, message=message, context=context)
