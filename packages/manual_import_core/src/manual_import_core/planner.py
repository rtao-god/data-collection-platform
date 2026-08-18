from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Never, cast

from pydantic import ValidationError

from collection_contracts import (
    ManualImportDisposition,
    ManualImportFormat,
    ManualImportIssue,
    ManualImportLocator,
    ManualImportMode,
    ManualImportPlan,
    ManualImportRecord,
    ManualSeedRow,
)

MAX_MANUAL_IMPORT_BYTES = 16 * 1024 * 1024
MAX_MANUAL_IMPORT_RECORDS = 100_000
_MANUAL_INPUT_FIELDS = tuple(name for name in ManualSeedRow.model_fields if name != "row_number")
_REQUIRED_TEXT_FIELDS = ("expected_entity_kind", "display_name", "provenance")
_OPTIONAL_TEXT_FIELDS = ("website", "osm_id", "note")


class _DuplicateJsonKey(ValueError):
    pass


class ManualImportPlanIntegrityError(ValueError):
    pass


def build_manual_import_plan(
    content: bytes,
    *,
    format: ManualImportFormat,
    mode: ManualImportMode = ManualImportMode.ATOMIC,
    max_file_bytes: int = MAX_MANUAL_IMPORT_BYTES,
    max_records: int = MAX_MANUAL_IMPORT_RECORDS,
    require_records: bool = True,
) -> ManualImportPlan:
    if not 1 <= max_file_bytes <= 5 * 1024 * 1024 * 1024:
        raise ValueError("manual import byte limit is outside the supported range")
    if not 1 <= max_records <= MAX_MANUAL_IMPORT_RECORDS:
        raise ValueError("manual import record limit is outside the supported range")

    source_digest = _digest_bytes(content)
    source_size_bytes = len(content)
    boundary_issue = _validate_file_boundary(content, max_file_bytes)
    if boundary_issue is not None:
        return _assemble_plan(
            source_digest=source_digest,
            source_size_bytes=source_size_bytes,
            format=format,
            mode=mode,
            records=(),
            issues=(boundary_issue,),
        )

    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        return _assemble_plan(
            source_digest=source_digest,
            source_size_bytes=source_size_bytes,
            format=format,
            mode=mode,
            records=(),
            issues=(
                _issue(
                    code="MANUAL_IMPORT_UTF8_INVALID",
                    message="The manual import file is not valid UTF-8.",
                    context={"byteOffset": exc.start},
                ),
            ),
        )

    raw_records, parse_issues = _parse_records(text, format)
    total_records = len(raw_records) + len(parse_issues)
    if total_records > max_records:
        return _assemble_plan(
            source_digest=source_digest,
            source_size_bytes=source_size_bytes,
            format=format,
            mode=mode,
            records=(),
            issues=(
                _issue(
                    code="MANUAL_IMPORT_RECORD_LIMIT_EXCEEDED",
                    message="The manual import file contains more records than allowed.",
                    context={
                        "actualRecords": total_records,
                        "maximumRecords": max_records,
                    },
                ),
            ),
        )

    records: list[ManualImportRecord] = []
    issues = list(parse_issues)
    for locator, raw_record in raw_records:
        try:
            payload = _normalize_record(raw_record, locator, format)
            row = ManualSeedRow.model_validate(payload)
        except (ValidationError, ValueError) as exc:
            issues.append(
                _issue(
                    locator=locator,
                    code="MANUAL_IMPORT_RECORD_INVALID",
                    message="The manual import record violates the canonical row contract.",
                    context={"detail": _validation_detail(exc)},
                )
            )
            continue
        record_digest = manual_import_record_digest(source_digest, locator, row)
        records.append(
            ManualImportRecord(
                locator=locator,
                record=row,
                record_digest=record_digest,
            )
        )

    if require_records and not records and not issues:
        issues.append(
            _issue(
                code="MANUAL_IMPORT_NO_RECORDS",
                message="The manual import file contains no records.",
                context={},
            )
        )

    return _assemble_plan(
        source_digest=source_digest,
        source_size_bytes=source_size_bytes,
        format=format,
        mode=mode,
        records=tuple(records),
        issues=tuple(issues),
    )


def schedulable_manual_import_records(
    plan: ManualImportPlan,
) -> tuple[ManualImportRecord, ...]:
    verify_manual_import_plan(plan)
    if plan.disposition is ManualImportDisposition.REJECTED:
        return ()
    return plan.records


def canonical_manual_import_plan_json(plan: ManualImportPlan) -> str:
    verify_manual_import_plan(plan)
    return _canonical_json(plan.model_dump(mode="json", by_alias=True)) + "\n"


def verify_manual_import_plan(plan: ManualImportPlan) -> None:
    expected = _digest_json(_plan_payload(plan))
    if plan.plan_digest != expected:
        raise ManualImportPlanIntegrityError(
            "manual import plan digest does not match its canonical content"
        )
    locators: set[tuple[str, int, str]] = set()
    for record in plan.records:
        identity = (record.locator.kind, record.locator.index, record.locator.pointer)
        if identity in locators:
            raise ManualImportPlanIntegrityError(
                "manual import plan contains duplicate record locators"
            )
        locators.add(identity)
        expected_record_digest = manual_import_record_digest(
            plan.source_digest,
            record.locator,
            record.record,
        )
        if record.record_digest != expected_record_digest:
            raise ManualImportPlanIntegrityError(
                "manual import plan contains an invalid record digest"
            )


def manual_import_record_digest(
    source_digest: str,
    locator: ManualImportLocator,
    record: ManualSeedRow,
) -> str:
    return _digest_json(
        {
            "sourceDigest": source_digest,
            "locator": locator.model_dump(mode="json", by_alias=True),
            "record": record.model_dump(mode="json", by_alias=True),
        }
    )


def _validate_file_boundary(content: bytes, max_file_bytes: int) -> ManualImportIssue | None:
    if not content:
        return _issue(
            code="MANUAL_IMPORT_FILE_EMPTY",
            message="The manual import file is empty.",
            context={},
        )
    if len(content) > max_file_bytes:
        return _issue(
            code="MANUAL_IMPORT_FILE_TOO_LARGE",
            message="The manual import file exceeds the allowed byte limit.",
            context={"actualBytes": len(content), "maximumBytes": max_file_bytes},
        )
    return None


def _parse_records(
    text: str,
    format: ManualImportFormat,
) -> tuple[
    tuple[tuple[ManualImportLocator, Mapping[str, object]], ...],
    tuple[ManualImportIssue, ...],
]:
    if format is ManualImportFormat.CSV:
        return _parse_csv(text)
    if format is ManualImportFormat.JSON:
        return _parse_json(text)
    if format is ManualImportFormat.JSONL:
        return _parse_jsonl(text)
    raise ValueError(f"unsupported manual import format: {format}")


def _parse_csv(
    text: str,
) -> tuple[
    tuple[tuple[ManualImportLocator, Mapping[str, object]], ...],
    tuple[ManualImportIssue, ...],
]:
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = tuple(reader.fieldnames or ())
        header_issue = _validate_csv_headers(headers)
        if header_issue is not None:
            return (), (header_issue,)

        records: list[tuple[ManualImportLocator, Mapping[str, object]]] = []
        issues: list[ManualImportIssue] = []
        for raw_row in reader:
            line_number = max(2, reader.line_num)
            locator = ManualImportLocator(
                kind="csv_row",
                index=line_number,
                pointer=f"line:{line_number}",
            )
            overflow = raw_row.get(None)
            missing = tuple(
                header for header in _MANUAL_INPUT_FIELDS if raw_row.get(header) is None
            )
            if overflow is not None or missing:
                issues.append(
                    _issue(
                        locator=locator,
                        code="MANUAL_IMPORT_CSV_ROW_SHAPE_INVALID",
                        message="The CSV row does not match the declared header.",
                        context={
                            "missingFields": missing,
                            "extraColumnCount": len(overflow or ()),
                        },
                    )
                )
                continue
            records.append((locator, cast(Mapping[str, object], raw_row)))
        return tuple(records), tuple(issues)
    except csv.Error as exc:
        return (), (
            _issue(
                code="MANUAL_IMPORT_CSV_MALFORMED",
                message="The CSV document is malformed.",
                context={"detail": str(exc)[:300]},
            ),
        )


def _validate_csv_headers(headers: tuple[str, ...]) -> ManualImportIssue | None:
    if not headers or any(not header for header in headers) or len(set(headers)) != len(headers):
        return _issue(
            code="MANUAL_IMPORT_CSV_HEADER_INVALID",
            message="The CSV header is missing, empty, or contains duplicates.",
            context={"actual": ",".join(headers)},
        )
    if headers != _MANUAL_INPUT_FIELDS:
        return _issue(
            code="MANUAL_IMPORT_CSV_HEADER_MISMATCH",
            message="The CSV header does not match the canonical manual import contract.",
            context={
                "actual": ",".join(headers),
                "expected": ",".join(_MANUAL_INPUT_FIELDS),
            },
        )
    return None


def _parse_json(
    text: str,
) -> tuple[
    tuple[tuple[ManualImportLocator, Mapping[str, object]], ...],
    tuple[ManualImportIssue, ...],
]:
    try:
        document = _load_json(text)
    except (json.JSONDecodeError, _DuplicateJsonKey, ValueError) as exc:
        return (), (
            _issue(
                code="MANUAL_IMPORT_JSON_MALFORMED",
                message="The JSON document is malformed or contains duplicate keys.",
                context={"detail": str(exc)[:300]},
            ),
        )

    if isinstance(document, Mapping):
        locator = ManualImportLocator(kind="json_index", index=1, pointer="$")
        return ((locator, cast(Mapping[str, object], document)),), ()
    if not isinstance(document, list):
        return (), (
            _issue(
                code="MANUAL_IMPORT_JSON_ROOT_INVALID",
                message="The JSON root must be an object or an array of objects.",
                context={"actualType": type(document).__name__},
            ),
        )

    records: list[tuple[ManualImportLocator, Mapping[str, object]]] = []
    issues: list[ManualImportIssue] = []
    for offset, value in enumerate(document):
        locator = ManualImportLocator(
            kind="json_index",
            index=offset + 1,
            pointer=f"$[{offset}]",
        )
        if not isinstance(value, Mapping):
            issues.append(
                _issue(
                    locator=locator,
                    code="MANUAL_IMPORT_JSON_RECORD_INVALID",
                    message="Every JSON array item must be an object.",
                    context={"actualType": type(value).__name__},
                )
            )
            continue
        records.append((locator, cast(Mapping[str, object], value)))
    return tuple(records), tuple(issues)


def _parse_jsonl(
    text: str,
) -> tuple[
    tuple[tuple[ManualImportLocator, Mapping[str, object]], ...],
    tuple[ManualImportIssue, ...],
]:
    records: list[tuple[ManualImportLocator, Mapping[str, object]]] = []
    issues: list[ManualImportIssue] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        locator = ManualImportLocator(
            kind="jsonl_line",
            index=line_number,
            pointer=f"line:{line_number}",
        )
        try:
            value = _load_json(line)
        except (json.JSONDecodeError, _DuplicateJsonKey, ValueError) as exc:
            issues.append(
                _issue(
                    locator=locator,
                    code="MANUAL_IMPORT_JSONL_MALFORMED",
                    message="The JSON Lines record is malformed or contains duplicate keys.",
                    context={"detail": str(exc)[:300]},
                )
            )
            continue
        if not isinstance(value, Mapping):
            issues.append(
                _issue(
                    locator=locator,
                    code="MANUAL_IMPORT_JSONL_RECORD_INVALID",
                    message="Every non-empty JSON Lines record must be an object.",
                    context={"actualType": type(value).__name__},
                )
            )
            continue
        records.append((locator, cast(Mapping[str, object], value)))
    return tuple(records), tuple(issues)


def _load_json(text: str) -> object:
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_non_finite_constant,
    )


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> Never:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _normalize_record(
    record: Mapping[str, object],
    locator: ManualImportLocator,
    format: ManualImportFormat,
) -> dict[str, object]:
    actual_fields = frozenset(record)
    expected_fields = frozenset(_MANUAL_INPUT_FIELDS)
    if actual_fields != expected_fields:
        missing = tuple(sorted(expected_fields.difference(actual_fields)))
        unknown = tuple(sorted(actual_fields.difference(expected_fields)))
        raise ValueError(
            f"manual import field set mismatch; missing={missing!r}; unknown={unknown!r}"
        )

    payload: dict[str, object] = {"row_number": locator.index}
    for field in _REQUIRED_TEXT_FIELDS:
        value = record[field]
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        payload[field] = value
    for field in _OPTIONAL_TEXT_FIELDS:
        value = record[field]
        if format is ManualImportFormat.CSV:
            if not isinstance(value, str):
                raise ValueError(f"CSV field {field} must be a string")
            payload[field] = _empty_to_none(value)
            continue
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{field} must be a string or null")
        payload[field] = value

    references = record["reference_urls"]
    if format is ManualImportFormat.CSV:
        if not isinstance(references, str):
            raise ValueError("CSV reference_urls must be a pipe-delimited string")
        payload["reference_urls"] = tuple(
            part.strip() for part in references.split("|") if part.strip()
        )
    elif isinstance(references, list) and all(isinstance(value, str) for value in references):
        payload["reference_urls"] = tuple(cast(list[str], references))
    else:
        raise ValueError("JSON reference_urls must be an array of strings")
    return payload


def _empty_to_none(value: str) -> str | None:
    stripped = value.strip()
    return stripped if stripped else None


def _assemble_plan(
    *,
    source_digest: str,
    source_size_bytes: int,
    format: ManualImportFormat,
    mode: ManualImportMode,
    records: tuple[ManualImportRecord, ...],
    issues: tuple[ManualImportIssue, ...],
) -> ManualImportPlan:
    disposition = ManualImportDisposition.ACCEPTED
    if issues:
        disposition = (
            ManualImportDisposition.PARTIAL
            if mode is ManualImportMode.PARTIAL and records
            else ManualImportDisposition.REJECTED
        )
    payload = {
        "contract": "collector-manual-import-plan",
        "contractRevision": "manual-import-plan-v1",
        "sourceDigest": source_digest,
        "sourceSizeBytes": source_size_bytes,
        "format": format.value,
        "mode": mode.value,
        "disposition": disposition.value,
        "validRecordCount": len(records),
        "issueCount": len(issues),
        "records": [record.model_dump(mode="json", by_alias=True) for record in records],
        "issues": [issue.model_dump(mode="json", by_alias=True) for issue in issues],
    }
    return ManualImportPlan.model_validate({**payload, "planDigest": _digest_json(payload)})


def _plan_payload(plan: ManualImportPlan) -> dict[str, object]:
    payload = plan.model_dump(mode="json", by_alias=True)
    del payload["planDigest"]
    return payload


def _issue(
    *,
    code: str,
    message: str,
    context: dict[str, str | int | bool | tuple[str, ...] | None],
    locator: ManualImportLocator | None = None,
) -> ManualImportIssue:
    return ManualImportIssue(
        locator=locator,
        code=code,
        message=message,
        context=context,
    )


def _validation_detail(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return json.dumps(
            exc.errors(include_input=False, include_url=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )[:1_000]
    return str(exc)[:1_000]


def _digest_bytes(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


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
