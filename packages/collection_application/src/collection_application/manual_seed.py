from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Never, cast

from pydantic import ValidationError

from collection_application.ports import RawCampaignBundle
from collection_contracts import ManualSeedRow, SourceBindingsDocument, SourcePolicy, owner_error

ManualSeedFormat = Literal["csv", "json", "jsonl"]

MANUAL_SEED_HEADERS = (
    "expected_entity_kind",
    "display_name",
    "website",
    "osm_id",
    "reference_urls",
    "note",
    "provenance",
)
_MAX_MANUAL_SEED_RECORDS = 100_000


@dataclass(frozen=True, slots=True)
class ManualSeedIssue:
    code: str
    record_number: int
    line_number: int | None
    errors: tuple[Mapping[str, object], ...]

    def as_context(self) -> dict[str, object]:
        context: dict[str, object] = {
            "code": self.code,
            "recordNumber": self.record_number,
            "errors": [dict(error) for error in self.errors],
        }
        if self.line_number is not None:
            context["lineNumber"] = self.line_number
        return context


@dataclass(frozen=True, slots=True)
class ManualSeedReadResult:
    rows: tuple[ManualSeedRow, ...]
    issues: tuple[ManualSeedIssue, ...]

    @property
    def record_count(self) -> int:
        return len(self.rows) + len(self.issues)


def load_manual_seed_rows(
    bundle: RawCampaignBundle,
    bindings: SourceBindingsDocument,
    policies: Mapping[str, SourcePolicy],
    correlation_id: str,
) -> dict[str, tuple[ManualSeedRow, ...]]:
    result: dict[str, tuple[ManualSeedRow, ...]] = {}
    for binding in bindings.items:
        if binding.capability != "manual_import":
            continue
        if binding.seed_provider.kind != "file":
            raise owner_error(
                error_type="collection/manual-seed-binding-invalid",
                owner="ManualSeedImport",
                code="MANUAL_SEED_BINDING_INVALID",
                message="Manual source binding has no file seed provider.",
                context={"bindingKey": binding.key},
                required_action="Correct the typed source binding before reading campaign seeds.",
                correlation_id=correlation_id,
            )
        policy = policies.get(binding.source_policy_key)
        if policy is None:
            raise owner_error(
                error_type="collection/manual-seed-policy-missing",
                owner="ManualSeedImport",
                code="MANUAL_SEED_POLICY_MISSING",
                message="Manual source binding references a missing source policy.",
                context={
                    "bindingKey": binding.key,
                    "policyKey": binding.source_policy_key,
                },
                required_action="Add the exact manual source policy before reading campaign seeds.",
                correlation_id=correlation_id,
            )
        access = policy.access
        if access.kind != "manual":
            raise owner_error(
                error_type="collection/manual-seed-policy-invalid",
                owner="ManualSeedImport",
                code="MANUAL_SEED_POLICY_INVALID",
                message="Manual source binding does not use a manual access policy.",
                context={
                    "bindingKey": binding.key,
                    "policyKey": policy.policy_key,
                    "accessKind": access.kind,
                },
                required_action="Publish a manual access policy for the manual source binding.",
                correlation_id=correlation_id,
            )
        declared_format: ManualSeedFormat = binding.seed_provider.format
        if declared_format not in access.accepted_formats:
            raise owner_error(
                error_type="collection/manual-seed-format-forbidden",
                owner="ManualSeedImport",
                code="MANUAL_SEED_FORMAT_FORBIDDEN",
                message="Manual seed format is not allowed by the source policy.",
                context={
                    "bindingKey": binding.key,
                    "policyKey": policy.policy_key,
                    "format": declared_format,
                    "acceptedFormats": list(access.accepted_formats),
                },
                required_action=(
                    "Use a policy-approved format or publish a reviewed policy revision."
                ),
                correlation_id=correlation_id,
            )

        path = binding.seed_provider.path
        raw = bundle.files.get(path)
        if raw is None:
            raise owner_error(
                error_type="collection/manual-seed-missing",
                owner="ManualSeedImport",
                code="MANUAL_SEED_MISSING",
                message="Manual source binding references a missing seed file.",
                context={"bindingKey": binding.key, "path": path},
                required_action="Add the referenced seed file inside the campaign bundle.",
                correlation_id=correlation_id,
            )
        read_result = read_manual_seed_records(
            raw,
            path=path,
            format=declared_format,
            max_file_bytes=access.max_file_bytes,
            partial_mode=False,
            partial_mode_allowed=access.partial_mode_allowed,
            require_records=False,
            correlation_id=correlation_id,
        )
        result[path] = read_result.rows
    return result


def read_manual_seed_records(
    raw: bytes,
    *,
    path: str,
    format: ManualSeedFormat,
    max_file_bytes: int,
    partial_mode: bool,
    partial_mode_allowed: bool,
    require_records: bool,
    correlation_id: str,
) -> ManualSeedReadResult:
    if partial_mode and not partial_mode_allowed:
        raise owner_error(
            error_type="collection/manual-seed-partial-mode-forbidden",
            owner="ManualSeedImport",
            code="MANUAL_SEED_PARTIAL_MODE_FORBIDDEN",
            message="Partial manual import is not allowed by the source policy.",
            context={"path": path},
            required_action=(
                "Correct the complete file or publish a policy that explicitly allows partial mode."
            ),
            correlation_id=correlation_id,
        )
    if not 1 <= len(raw) <= max_file_bytes:
        raise owner_error(
            error_type="collection/manual-seed-size-invalid",
            owner="ManualSeedImport",
            code="MANUAL_SEED_SIZE_INVALID",
            message="Manual seed file size is outside the source-policy limit.",
            context={
                "path": path,
                "actualBytes": len(raw),
                "maximumBytes": max_file_bytes,
            },
            required_action=(
                "Provide a non-empty file within the reviewed source-policy size limit."
            ),
            correlation_id=correlation_id,
        )
    text = _decode_utf8(raw, path, correlation_id)
    result = _parse_manual_seed_text(text, path, format, correlation_id)
    if result.record_count > _MAX_MANUAL_SEED_RECORDS:
        raise owner_error(
            error_type="collection/manual-seed-record-limit-exceeded",
            owner="ManualSeedImport",
            code="MANUAL_SEED_RECORD_LIMIT_EXCEEDED",
            message="Manual seed file contains more records than the platform limit.",
            context={
                "path": path,
                "actualRecords": result.record_count,
                "maximumRecords": _MAX_MANUAL_SEED_RECORDS,
            },
            required_action="Split the source into independently reviewable import files.",
            correlation_id=correlation_id,
        )
    if require_records and result.record_count == 0:
        raise owner_error(
            error_type="collection/manual-seed-empty",
            owner="ManualSeedImport",
            code="MANUAL_SEED_EMPTY",
            message="Manual seed file contains no records.",
            context={"path": path, "format": format},
            required_action="Provide at least one complete manual seed record.",
            correlation_id=correlation_id,
        )
    if result.issues and not partial_mode:
        raise owner_error(
            error_type="collection/manual-seed-file-invalid",
            owner="ManualSeedImport",
            code="MANUAL_SEED_FILE_INVALID",
            message="Manual seed file contains invalid records and cannot be partially accepted.",
            context={
                "path": path,
                "format": format,
                "recordCount": result.record_count,
                "validRecordCount": len(result.rows),
                "invalidRecordCount": len(result.issues),
                "issues": [issue.as_context() for issue in result.issues],
            },
            required_action=(
                "Correct every reported record or explicitly use policy-approved partial mode."
            ),
            correlation_id=correlation_id,
        )
    return result


def parse_seed_csv(raw: bytes, path: str, correlation_id: str) -> tuple[ManualSeedRow, ...]:
    """Compatibility-free owner entry point for the canonical CSV format."""
    result = read_manual_seed_records(
        raw,
        path=path,
        format="csv",
        max_file_bytes=max(1, len(raw)),
        partial_mode=False,
        partial_mode_allowed=False,
        require_records=False,
        correlation_id=correlation_id,
    )
    return result.rows


def _decode_utf8(raw: bytes, path: str, correlation_id: str) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise owner_error(
            error_type="collection/manual-seed-encoding-invalid",
            owner="ManualSeedImport",
            code="MANUAL_SEED_ENCODING_INVALID",
            message="Manual seed file is not valid UTF-8.",
            context={"path": path, "byteOffset": exc.start},
            required_action="Encode the complete seed file as UTF-8 and import it again.",
            correlation_id=correlation_id,
        ) from exc


def _parse_manual_seed_text(
    text: str,
    path: str,
    format: ManualSeedFormat,
    correlation_id: str,
) -> ManualSeedReadResult:
    if format == "csv":
        return _parse_csv_text(text, path, correlation_id)
    if format == "json":
        return _parse_json_text(text, path, correlation_id)
    if format == "jsonl":
        return _parse_jsonl_text(text)
    raise AssertionError(f"unreachable manual seed format: {format}")


def _parse_csv_text(
    text: str,
    path: str,
    correlation_id: str,
) -> ManualSeedReadResult:
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = tuple(reader.fieldnames or ())
        if headers != MANUAL_SEED_HEADERS:
            raise owner_error(
                error_type="collection/manual-seed-header-invalid",
                owner="ManualSeedImport",
                code="MANUAL_SEED_HEADER_INVALID",
                message="Manual seed CSV header does not match the owned contract.",
                context={
                    "path": path,
                    "expectedHeaders": list(MANUAL_SEED_HEADERS),
                    "actualHeaders": list(headers),
                },
                required_action="Use the exact documented header and preserve its column order.",
                correlation_id=correlation_id,
            )
        rows: list[ManualSeedRow] = []
        issues: list[ManualSeedIssue] = []
        for line_number, raw_row in enumerate(reader, start=2):
            record, issue = _parse_csv_row(raw_row, line_number)
            if issue is not None:
                issues.append(issue)
            elif record is not None:
                rows.append(record)
    except csv.Error as exc:
        raise owner_error(
            error_type="collection/manual-seed-csv-invalid",
            owner="ManualSeedImport",
            code="MANUAL_SEED_CSV_INVALID",
            message="Manual seed CSV cannot be parsed safely.",
            context={"path": path, "detail": str(exc)},
            required_action="Correct the CSV quoting and row structure before importing it again.",
            correlation_id=correlation_id,
        ) from exc
    return ManualSeedReadResult(rows=tuple(rows), issues=tuple(issues))


def _parse_csv_row(
    raw_row: Mapping[str | None, str | list[str] | None],
    line_number: int,
) -> tuple[ManualSeedRow | None, ManualSeedIssue | None]:
    errors: list[Mapping[str, object]] = []
    extra_values = raw_row.get(None)
    if extra_values is not None:
        errors.append({"type": "extra_columns", "values": list(extra_values)})

    values: dict[str, str] = {}
    for header in MANUAL_SEED_HEADERS:
        value = raw_row.get(header)
        if not isinstance(value, str):
            errors.append({"type": "missing_column_value", "column": header})
        else:
            values[header] = value
    if errors:
        return None, _issue("MANUAL_SEED_ROW_INVALID", line_number, line_number, errors)

    payload = {
        "row_number": line_number,
        "expected_entity_kind": values["expected_entity_kind"],
        "display_name": values["display_name"],
        "website": _empty_to_none(values["website"]),
        "osm_id": _empty_to_none(values["osm_id"]),
        "reference_urls": _split_reference_urls(values["reference_urls"]),
        "note": _empty_to_none(values["note"]),
        "provenance": values["provenance"],
    }
    return _validate_seed_payload(payload, line_number, line_number)


def _parse_json_text(
    text: str,
    path: str,
    correlation_id: str,
) -> ManualSeedReadResult:
    try:
        payload = _load_json(text)
    except (json.JSONDecodeError, _DuplicateJsonKey, _NonFiniteJsonNumber) as exc:
        raise owner_error(
            error_type="collection/manual-seed-json-invalid",
            owner="ManualSeedImport",
            code="MANUAL_SEED_JSON_INVALID",
            message="Manual seed JSON cannot be parsed safely.",
            context={"path": path, "detail": str(exc)},
            required_action="Correct the JSON syntax, duplicate keys, or non-finite numbers.",
            correlation_id=correlation_id,
        ) from exc

    raw_records = payload if isinstance(payload, list) else [payload]
    rows: list[ManualSeedRow] = []
    issues: list[ManualSeedIssue] = []
    for record_number, raw_record in enumerate(raw_records, start=1):
        record, issue = _parse_json_record(raw_record, record_number, None)
        if issue is not None:
            issues.append(issue)
        elif record is not None:
            rows.append(record)
    return ManualSeedReadResult(rows=tuple(rows), issues=tuple(issues))


def _parse_jsonl_text(text: str) -> ManualSeedReadResult:
    rows: list[ManualSeedRow] = []
    issues: list[ManualSeedIssue] = []
    record_number = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        record_number += 1
        try:
            raw_record = _load_json(line)
        except (json.JSONDecodeError, _DuplicateJsonKey, _NonFiniteJsonNumber) as exc:
            issues.append(
                _issue(
                    "MANUAL_SEED_JSONL_RECORD_INVALID",
                    record_number,
                    line_number,
                    ({"type": type(exc).__name__, "detail": str(exc)},),
                )
            )
            continue
        record, issue = _parse_json_record(raw_record, record_number, line_number)
        if issue is not None:
            issues.append(issue)
        elif record is not None:
            rows.append(record)
    return ManualSeedReadResult(rows=tuple(rows), issues=tuple(issues))


def _parse_json_record(
    raw_record: object,
    record_number: int,
    line_number: int | None,
) -> tuple[ManualSeedRow | None, ManualSeedIssue | None]:
    if not isinstance(raw_record, dict):
        return None, _issue(
            "MANUAL_SEED_RECORD_SHAPE_INVALID",
            record_number,
            line_number,
            ({"type": "object_required", "actualType": type(raw_record).__name__},),
        )
    typed_record = cast(dict[str, object], raw_record)
    actual_keys = set(typed_record)
    expected_keys = set(MANUAL_SEED_HEADERS)
    if actual_keys != expected_keys:
        return None, _issue(
            "MANUAL_SEED_RECORD_SHAPE_INVALID",
            record_number,
            line_number,
            (
                {
                    "type": "field_set_mismatch",
                    "missingFields": sorted(expected_keys.difference(actual_keys)),
                    "unknownFields": sorted(actual_keys.difference(expected_keys)),
                },
            ),
        )

    errors: list[Mapping[str, object]] = []
    required_strings: dict[str, str] = {}
    for field in ("expected_entity_kind", "display_name", "provenance"):
        value = typed_record[field]
        if not isinstance(value, str):
            errors.append(
                {"type": "string_required", "field": field, "actualType": type(value).__name__}
            )
        else:
            required_strings[field] = value

    optional_strings: dict[str, str | None] = {}
    for field in ("website", "osm_id", "note"):
        value = typed_record[field]
        if value is not None and not isinstance(value, str):
            errors.append(
                {
                    "type": "string_or_null_required",
                    "field": field,
                    "actualType": type(value).__name__,
                }
            )
        else:
            optional_strings[field] = value

    raw_references = typed_record["reference_urls"]
    references: tuple[str, ...] = ()
    if not isinstance(raw_references, list) or not all(
        isinstance(value, str) for value in raw_references
    ):
        errors.append(
            {
                "type": "string_array_required",
                "field": "reference_urls",
                "actualType": type(raw_references).__name__,
            }
        )
    else:
        references = tuple(cast(list[str], raw_references))

    if errors:
        return None, _issue(
            "MANUAL_SEED_ROW_INVALID",
            record_number,
            line_number,
            errors,
        )

    payload = {
        "row_number": line_number if line_number is not None else record_number,
        "expected_entity_kind": required_strings["expected_entity_kind"],
        "display_name": required_strings["display_name"],
        "website": optional_strings["website"],
        "osm_id": optional_strings["osm_id"],
        "reference_urls": references,
        "note": optional_strings["note"],
        "provenance": required_strings["provenance"],
    }
    return _validate_seed_payload(payload, record_number, line_number)


def _validate_seed_payload(
    payload: Mapping[str, object],
    record_number: int,
    line_number: int | None,
) -> tuple[ManualSeedRow | None, ManualSeedIssue | None]:
    try:
        return ManualSeedRow.model_validate(payload), None
    except ValidationError as exc:
        return None, _issue(
            "MANUAL_SEED_ROW_INVALID",
            record_number,
            line_number,
            cast(
                Sequence[Mapping[str, object]],
                exc.errors(include_input=False, include_url=False),
            ),
        )


def _issue(
    code: str,
    record_number: int,
    line_number: int | None,
    errors: Sequence[Mapping[str, object]],
) -> ManualSeedIssue:
    return ManualSeedIssue(
        code=code,
        record_number=record_number,
        line_number=line_number,
        errors=tuple(dict(error) for error in errors),
    )


class _DuplicateJsonKey(ValueError):
    pass


class _NonFiniteJsonNumber(ValueError):
    pass


def _load_json(text: str) -> object:
    return cast(
        object,
        json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        ),
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Never:
    raise _NonFiniteJsonNumber(f"non-finite JSON number is forbidden: {value}")


def _empty_to_none(value: str) -> str | None:
    stripped = value.strip()
    return stripped if stripped else None


def _split_reference_urls(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    return tuple(part.strip() for part in value.split("|") if part.strip())
