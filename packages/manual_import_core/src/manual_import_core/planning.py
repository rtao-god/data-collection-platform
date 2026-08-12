from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import TypeAlias, cast

MANUAL_IMPORT_PLAN_CONTRACT = "collector-manual-import-plan"
MANUAL_IMPORT_PLAN_REVISION = "manual-import-plan-v1"
MAX_MANUAL_IMPORT_BYTES = 16 * 1024 * 1024
MAX_MANUAL_IMPORT_RECORDS = 100_000

Scalar: TypeAlias = str | None
CanonicalRecord: TypeAlias = dict[str, Scalar]
ParsedRecord: TypeAlias = tuple["ManualImportLocator", object]


class ManualImportFormat(StrEnum):
    CSV = "csv"
    JSON = "json"
    JSONL = "jsonl"


class ManualImportMode(StrEnum):
    ATOMIC = "atomic"
    PARTIAL = "partial"


class ManualImportPlanStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ManualImportLocator:
    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind not in {"csv-record", "json-pointer", "jsonl-line"}:
            raise ValueError("manual import locator kind is unsupported")
        if not self.value or len(self.value) > 100:
            raise ValueError("manual import locator value is invalid")

    def to_wire(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True, slots=True)
class ManualImportIssue:
    code: str
    message: str
    locator: ManualImportLocator | None

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("manual import issue requires code and message")

    def to_wire(self) -> dict[str, object]:
        return {
            "code": self.code,
            "locator": self.locator.to_wire() if self.locator is not None else None,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ManualImportRecord:
    position: int
    locator: ManualImportLocator
    fields: Mapping[str, Scalar]
    record_digest: str
    semantic_key: str

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("manual import record position cannot be negative")
        _require_digest(self.record_digest)
        _require_digest(self.semantic_key)
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def to_wire(self) -> dict[str, object]:
        return {
            "fields": dict(self.fields),
            "locator": self.locator.to_wire(),
            "position": self.position,
            "recordDigest": self.record_digest,
            "semanticKey": self.semantic_key,
        }


@dataclass(frozen=True, slots=True)
class ManualImportPlan:
    source_digest: str
    format: ManualImportFormat
    mode: ManualImportMode
    status: ManualImportPlanStatus
    records: tuple[ManualImportRecord, ...]
    issues: tuple[ManualImportIssue, ...]

    def __post_init__(self) -> None:
        _require_digest(self.source_digest)
        if (
            self.status is ManualImportPlanStatus.READY
            and self.mode is ManualImportMode.ATOMIC
            and self.issues
        ):
            raise ValueError("ready atomic plan cannot contain issues")
        if self.status is ManualImportPlanStatus.BLOCKED and not self.issues:
            raise ValueError("blocked manual import plan requires at least one issue")

    @property
    def accepted_record_count(self) -> int:
        return len(self.records)

    def to_wire(self) -> dict[str, object]:
        return {
            "acceptedRecordCount": self.accepted_record_count,
            "contract": MANUAL_IMPORT_PLAN_CONTRACT,
            "contractRevision": MANUAL_IMPORT_PLAN_REVISION,
            "format": self.format.value,
            "issueCount": len(self.issues),
            "issues": [issue.to_wire() for issue in self.issues],
            "mode": self.mode.value,
            "records": [record.to_wire() for record in self.records],
            "sourceDigest": self.source_digest,
            "status": self.status.value,
        }

    def to_bytes(self) -> bytes:
        return _canonical_json(self.to_wire())

    @property
    def digest(self) -> str:
        return _digest(self.to_bytes())


def build_manual_import_plan(
    body: bytes,
    *,
    format: ManualImportFormat,
    mode: ManualImportMode,
) -> ManualImportPlan:
    source_digest = _digest(body)
    if len(body) > MAX_MANUAL_IMPORT_BYTES:
        return _blocked_plan(
            source_digest,
            format,
            mode,
            ManualImportIssue(
                code="MANUAL_IMPORT_INPUT_TOO_LARGE",
                message="The manual import exceeds the 16 MiB input limit.",
                locator=None,
            ),
        )
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _blocked_plan(
            source_digest,
            format,
            mode,
            ManualImportIssue(
                code="MANUAL_IMPORT_ENCODING_INVALID",
                message="The manual import must be valid UTF-8.",
                locator=None,
            ),
        )

    parsed, issues = _parse(text, format)
    records: list[ManualImportRecord] = []
    processed_count = 0
    for locator, value in parsed:
        if processed_count >= MAX_MANUAL_IMPORT_RECORDS:
            issues.append(
                ManualImportIssue(
                    code="MANUAL_IMPORT_RECORD_LIMIT_EXCEEDED",
                    message="The manual import exceeds the 100000 record limit.",
                    locator=locator,
                )
            )
            break
        processed_count += 1
        try:
            normalized = _normalize_record(value)
        except ValueError as exc:
            issues.append(
                ManualImportIssue(
                    code="MANUAL_IMPORT_RECORD_INVALID",
                    message=str(exc),
                    locator=locator,
                )
            )
            continue
        canonical = _canonical_json(normalized)
        record_digest = _digest(canonical)
        semantic_key = _digest(
            _canonical_json(
                {
                    "locator": locator.to_wire(),
                    "recordDigest": record_digest,
                    "sourceDigest": source_digest,
                }
            )
        )
        records.append(
            ManualImportRecord(
                position=len(records),
                locator=locator,
                fields=normalized,
                record_digest=record_digest,
                semantic_key=semantic_key,
            )
        )

    if not parsed and not issues:
        issues.append(
            ManualImportIssue(
                code="MANUAL_IMPORT_EMPTY",
                message="The manual import does not contain any records.",
                locator=None,
            )
        )

    if issues and mode is ManualImportMode.ATOMIC:
        records = []
    status = (
        ManualImportPlanStatus.BLOCKED
        if issues and (mode is ManualImportMode.ATOMIC or not records)
        else ManualImportPlanStatus.READY
    )
    return ManualImportPlan(
        source_digest=source_digest,
        format=format,
        mode=mode,
        status=status,
        records=tuple(records),
        issues=tuple(issues),
    )


def _parse(
    text: str,
    format: ManualImportFormat,
) -> tuple[list[ParsedRecord], list[ManualImportIssue]]:
    if format is ManualImportFormat.CSV:
        return _parse_csv(text)
    if format is ManualImportFormat.JSON:
        return _parse_json(text)
    if format is ManualImportFormat.JSONL:
        return _parse_jsonl(text)
    raise ValueError(f"unsupported manual import format: {format}")


def _parse_csv(text: str) -> tuple[list[ParsedRecord], list[ManualImportIssue]]:
    issues: list[ManualImportIssue] = []
    stream = io.StringIO(text, newline="")
    try:
        reader = csv.DictReader(stream, strict=True)
        headers = reader.fieldnames
        if headers is None:
            return [], [
                ManualImportIssue(
                    code="MANUAL_IMPORT_CSV_HEADER_MISSING",
                    message="The CSV input must contain a header row.",
                    locator=None,
                )
            ]
        if any(not header.strip() for header in headers) or len(headers) != len(set(headers)):
            return [], [
                ManualImportIssue(
                    code="MANUAL_IMPORT_CSV_HEADER_INVALID",
                    message="CSV headers must be non-empty and unique.",
                    locator=None,
                )
            ]
        records: list[ParsedRecord] = []
        for row in reader:
            locator = ManualImportLocator(kind="csv-record", value=str(reader.line_num))
            if None in row:
                issues.append(
                    ManualImportIssue(
                        code="MANUAL_IMPORT_CSV_COLUMN_COUNT_INVALID",
                        message="The CSV record contains more values than the header.",
                        locator=locator,
                    )
                )
                continue
            records.append((locator, row))
        return records, issues
    except csv.Error as exc:
        return [], [
            ManualImportIssue(
                code="MANUAL_IMPORT_CSV_INVALID",
                message=f"The CSV input is malformed: {exc}.",
                locator=None,
            )
        ]


def _parse_json(text: str) -> tuple[list[ParsedRecord], list[ManualImportIssue]]:
    try:
        value = _json_loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return [], [
            ManualImportIssue(
                code="MANUAL_IMPORT_JSON_INVALID",
                message=f"The JSON input is malformed: {exc}.",
                locator=None,
            )
        ]
    if isinstance(value, dict):
        return [(ManualImportLocator(kind="json-pointer", value="/"), value)], []
    if isinstance(value, list):
        return [
            (ManualImportLocator(kind="json-pointer", value=f"/{index}"), record)
            for index, record in enumerate(value)
        ], []
    return [], [
        ManualImportIssue(
            code="MANUAL_IMPORT_JSON_ROOT_INVALID",
            message="The JSON root must be an object or an array of objects.",
            locator=None,
        )
    ]


def _parse_jsonl(text: str) -> tuple[list[ParsedRecord], list[ManualImportIssue]]:
    records: list[ParsedRecord] = []
    issues: list[ManualImportIssue] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        locator = ManualImportLocator(kind="jsonl-line", value=str(line_number))
        try:
            value = _json_loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            issues.append(
                ManualImportIssue(
                    code="MANUAL_IMPORT_JSONL_RECORD_INVALID",
                    message=f"The JSON Lines record is malformed: {exc}.",
                    locator=locator,
                )
            )
            continue
        records.append((locator, value))
    return records, issues


def _json_loads(text: str) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number: {value}")

    return cast(
        object,
        json.loads(text, object_pairs_hook=object_pairs, parse_constant=reject_constant),
    )


def _normalize_record(value: object) -> CanonicalRecord:
    if not isinstance(value, dict):
        raise ValueError("Each manual import record must be an object.")
    normalized: CanonicalRecord = {}
    items = cast(dict[object, object], value).items()
    for raw_key, item in sorted(items, key=lambda pair: str(pair[0])):
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError("Manual import field names must be non-empty strings.")
        if item is None or isinstance(item, str):
            normalized[raw_key] = item
        elif isinstance(item, bool):
            normalized[raw_key] = "true" if item else "false"
        elif isinstance(item, int):
            normalized[raw_key] = str(item)
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("Manual import numbers must be finite.")
            normalized[raw_key] = json.dumps(item, allow_nan=False, separators=(",", ":"))
        else:
            raise ValueError("Manual import records may contain only scalar values.")
    return normalized


def _blocked_plan(
    source_digest: str,
    format: ManualImportFormat,
    mode: ManualImportMode,
    issue: ManualImportIssue,
) -> ManualImportPlan:
    return ManualImportPlan(
        source_digest=source_digest,
        format=format,
        mode=mode,
        status=ManualImportPlanStatus.BLOCKED,
        records=(),
        issues=(issue,),
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return f"sha256:{sha256(value).hexdigest()}"


def _require_digest(value: str) -> None:
    if len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError("value must be a canonical SHA-256 digest")
    if any(character not in "0123456789abcdef" for character in value[7:]):
        raise ValueError("value must be a canonical SHA-256 digest")
