from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write(relative: str, content: str) -> None:
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def replace_once(relative: str, old: str, new: str) -> None:
    text = read(relative)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"{relative}: expected one occurrence, found {count}: {old[:120]!r}"
        )
    write(relative, text.replace(old, new, 1))


def add_workspace_member(member: str) -> None:
    relative = "pyproject.toml"
    text = read(relative)
    if f'"{member}"' in text:
        return
    pattern = re.compile(
        r"(?P<head>\[tool\.uv\.workspace\]\s*\nmembers\s*=\s*\[\n)"
        r"(?P<body>.*?)"
        r"(?P<tail>\n\])",
        re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        raise RuntimeError("root uv workspace member list was not found")
    body = match.group("body").rstrip()
    if body and not body.endswith(","):
        body += ","
    body += f'\n  "{member}",'
    text = text[: match.start()] + match.group("head") + body + match.group("tail") + text[match.end() :]
    write(relative, text)


def add_mypy_path(path: str) -> None:
    relative = "pyproject.toml"
    text = read(relative)
    if f'"{path}"' in text:
        return
    section_start = text.find("[tool.mypy]")
    if section_start < 0:
        return
    files_start = text.find("files = [", section_start)
    if files_start < 0:
        return
    open_bracket = text.find("[", files_start)
    close_bracket = text.find("]", open_bracket)
    if close_bracket < 0:
        raise RuntimeError("tool.mypy files list is malformed")
    insertion = f'  "{path}",\n'
    text = text[:close_bracket] + insertion + text[close_bracket:]
    write(relative, text)


def create_contracts() -> None:
    write(
        "packages/collection_contracts/src/collection_contracts/manual_import.py",
        '''from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from collection_contracts.campaign_config import ManualSeedRow

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")


class ManualImportFormat(StrEnum):
    CSV = "csv"
    JSON = "json"
    JSONL = "jsonl"


class ManualImportMode(StrEnum):
    ATOMIC = "atomic"
    PARTIAL = "partial"


class ManualImportDisposition(StrEnum):
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    REJECTED = "rejected"


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


class ManualImportLocator(_ContractModel):
    kind: Literal["csv_row", "json_index", "jsonl_line"]
    index: int = Field(ge=1)
    pointer: str = Field(min_length=1, max_length=200)


class ManualImportIssue(_ContractModel):
    locator: ManualImportLocator | None = None
    code: str
    message: str = Field(min_length=1, max_length=500)
    context: dict[str, str | int | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_code(self) -> Self:
        if _CODE_PATTERN.fullmatch(self.code) is None:
            raise ValueError("manual import issue code has an invalid format")
        return self


class ManualImportRecord(_ContractModel):
    locator: ManualImportLocator
    record: ManualSeedRow
    record_digest: str = Field(alias="recordDigest")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        _require_digest("record_digest", self.record_digest)
        return self


class ManualImportPlan(_ContractModel):
    contract: Literal["collector-manual-import-plan"] = "collector-manual-import-plan"
    contract_revision: Literal["manual-import-plan-v1"] = Field(
        default="manual-import-plan-v1",
        alias="contractRevision",
    )
    source_digest: str = Field(alias="sourceDigest")
    source_size_bytes: int = Field(alias="sourceSizeBytes", ge=0, le=16_777_216)
    format: ManualImportFormat
    mode: ManualImportMode
    disposition: ManualImportDisposition
    valid_record_count: int = Field(alias="validRecordCount", ge=0, le=100_000)
    issue_count: int = Field(alias="issueCount", ge=0, le=100_000)
    records: tuple[ManualImportRecord, ...] = ()
    issues: tuple[ManualImportIssue, ...] = ()
    plan_digest: str = Field(alias="planDigest")

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        _require_digest("source_digest", self.source_digest)
        _require_digest("plan_digest", self.plan_digest)
        if self.valid_record_count != len(self.records):
            raise ValueError("valid_record_count must equal the record collection size")
        if self.issue_count != len(self.issues):
            raise ValueError("issue_count must equal the issue collection size")
        if self.disposition is ManualImportDisposition.ACCEPTED and self.issues:
            raise ValueError("accepted manual import plan cannot contain issues")
        if self.disposition is ManualImportDisposition.PARTIAL:
            if self.mode is not ManualImportMode.PARTIAL or not self.records or not self.issues:
                raise ValueError("partial disposition requires partial mode, records, and issues")
        if self.disposition is ManualImportDisposition.REJECTED and not self.issues:
            raise ValueError("rejected manual import plan requires at least one issue")
        return self


def _require_digest(name: str, value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be canonical SHA-256")
''',
    )

    relative = "packages/collection_contracts/src/collection_contracts/__init__.py"
    text = read(relative)
    if "from collection_contracts.manual_import import" not in text:
        import_block = '''from collection_contracts.manual_import import (
    ManualImportDisposition,
    ManualImportFormat,
    ManualImportIssue,
    ManualImportLocator,
    ManualImportMode,
    ManualImportPlan,
    ManualImportRecord,
)
'''
        marker = "__all__ = ("
        index = text.find(marker)
        if index < 0:
            raise RuntimeError("collection_contracts __all__ marker was not found")
        text = text[:index] + import_block + "\n" + text[index:]

    names = (
        "ManualImportDisposition",
        "ManualImportFormat",
        "ManualImportIssue",
        "ManualImportLocator",
        "ManualImportMode",
        "ManualImportPlan",
        "ManualImportRecord",
    )
    all_start = text.find("__all__ = (")
    all_end = text.find("\n)", all_start)
    if all_start < 0 or all_end < 0:
        raise RuntimeError("collection_contracts __all__ tuple is malformed")
    block = text[all_start:all_end]
    additions = "".join(f'    "{name}",\n' for name in names if f'"{name}"' not in block)
    if additions:
        text = text[:all_end] + "\n" + additions.rstrip("\n") + text[all_end:]
    write(relative, text)


def create_core_package() -> None:
    write(
        "packages/manual_import_core/pyproject.toml",
        '''[project]
name = "manual-import-core"
version = "0.1.0"
description = "Deterministic manual import planning for the Data Collection Platform"
requires-python = ">=3.13,<3.14"
dependencies = [
  "collection-contracts",
  "pydantic==2.13.4",
]

[build-system]
requires = ["hatchling==1.31.0"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/manual_import_core"]

[tool.uv.sources]
collection-contracts = { workspace = true }
''',
    )
    write(
        "packages/manual_import_core/src/manual_import_core/__init__.py",
        '''from manual_import_core.planner import (
    MAX_MANUAL_IMPORT_BYTES,
    MAX_MANUAL_IMPORT_RECORDS,
    build_manual_import_plan,
    canonical_manual_import_plan_json,
    schedulable_manual_import_records,
)

__all__ = (
    "MAX_MANUAL_IMPORT_BYTES",
    "MAX_MANUAL_IMPORT_RECORDS",
    "build_manual_import_plan",
    "canonical_manual_import_plan_json",
    "schedulable_manual_import_records",
)
''',
    )
    write(
        "packages/manual_import_core/src/manual_import_core/planner.py",
        '''from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import NoReturn

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


class _DuplicateJsonKey(ValueError):
    pass


class _UnsupportedJsonValue(ValueError):
    pass


def build_manual_import_plan(
    content: bytes,
    *,
    format: ManualImportFormat,
    mode: ManualImportMode = ManualImportMode.ATOMIC,
) -> ManualImportPlan:
    source_digest = _digest_bytes(content)
    if len(content) > MAX_MANUAL_IMPORT_BYTES:
        return _assemble_plan(
            source_digest=source_digest,
            source_size_bytes=len(content),
            format=format,
            mode=mode,
            records=(),
            issues=(
                _issue(
                    code="MANUAL_IMPORT_FILE_TOO_LARGE",
                    message="The manual import file exceeds the supported byte limit.",
                    context={"maximumBytes": MAX_MANUAL_IMPORT_BYTES, "actualBytes": len(content)},
                ),
            ),
        )
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        return _assemble_plan(
            source_digest=source_digest,
            source_size_bytes=len(content),
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
    if parse_issues:
        return _assemble_plan(
            source_digest=source_digest,
            source_size_bytes=len(content),
            format=format,
            mode=mode,
            records=(),
            issues=parse_issues,
        )
    if len(raw_records) > MAX_MANUAL_IMPORT_RECORDS:
        return _assemble_plan(
            source_digest=source_digest,
            source_size_bytes=len(content),
            format=format,
            mode=mode,
            records=(),
            issues=(
                _issue(
                    code="MANUAL_IMPORT_RECORD_LIMIT_EXCEEDED",
                    message="The manual import file contains too many records.",
                    context={
                        "maximumRecords": MAX_MANUAL_IMPORT_RECORDS,
                        "actualRecords": len(raw_records),
                    },
                ),
            ),
        )

    records: list[ManualImportRecord] = []
    issues: list[ManualImportIssue] = []
    for locator, raw_record in raw_records:
        try:
            normalized = _normalize_record(raw_record)
            row = ManualSeedRow.model_validate(normalized)
        except (_UnsupportedJsonValue, ValidationError, ValueError) as exc:
            issues.append(
                _issue(
                    locator=locator,
                    code="MANUAL_IMPORT_RECORD_INVALID",
                    message="The manual import record does not satisfy the canonical row contract.",
                    context={"detail": _validation_detail(exc)},
                )
            )
            continue
        canonical_row = row.model_dump(mode="json", by_alias=True)
        record_digest = _digest_json(
            {
                "sourceDigest": source_digest,
                "locator": locator.model_dump(mode="json", by_alias=True),
                "record": canonical_row,
            }
        )
        records.append(
            ManualImportRecord(
                locator=locator,
                record=row,
                record_digest=record_digest,
            )
        )

    return _assemble_plan(
        source_digest=source_digest,
        source_size_bytes=len(content),
        format=format,
        mode=mode,
        records=tuple(records),
        issues=tuple(issues),
    )


def schedulable_manual_import_records(
    plan: ManualImportPlan,
) -> tuple[ManualImportRecord, ...]:
    if plan.disposition is ManualImportDisposition.REJECTED:
        return ()
    return plan.records


def canonical_manual_import_plan_json(plan: ManualImportPlan) -> str:
    return _canonical_json(plan.model_dump(mode="json", by_alias=True)) + "\n"


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
    expected_headers = tuple(
        field.alias or name for name, field in ManualSeedRow.model_fields.items()
    )
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = tuple(reader.fieldnames or ())
        if not headers or len(set(headers)) != len(headers) or any(not value for value in headers):
            return (), (
                _issue(
                    code="MANUAL_IMPORT_CSV_HEADER_INVALID",
                    message="The CSV header is missing, empty, or contains duplicates.",
                    context={},
                ),
            )
        if headers != expected_headers:
            return (), (
                _issue(
                    code="MANUAL_IMPORT_CSV_HEADER_MISMATCH",
                    message="The CSV header does not match the canonical manual row contract.",
                    context={
                        "expected": ",".join(expected_headers),
                        "actual": ",".join(headers),
                    },
                ),
            )
        records: list[tuple[ManualImportLocator, Mapping[str, object]]] = []
        for row in reader:
            if None in row:
                return (), (
                    _issue(
                        code="MANUAL_IMPORT_CSV_COLUMN_OVERFLOW",
                        message="A CSV row contains columns not declared by the header.",
                        context={"line": reader.line_num},
                    ),
                )
            locator = ManualImportLocator(
                kind="csv_row",
                index=max(1, reader.line_num),
                pointer=f"line:{reader.line_num}",
            )
            records.append((locator, row))
        return tuple(records), ()
    except csv.Error as exc:
        return (), (
            _issue(
                code="MANUAL_IMPORT_CSV_MALFORMED",
                message="The CSV document is malformed.",
                context={"detail": str(exc)[:300]},
            ),
        )


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
        return ((locator, document),), ()
    if not isinstance(document, list):
        return (), (
            _issue(
                code="MANUAL_IMPORT_JSON_ROOT_INVALID",
                message="The JSON root must be an object or an array of objects.",
                context={"actualType": type(document).__name__},
            ),
        )
    records: list[tuple[ManualImportLocator, Mapping[str, object]]] = []
    for offset, value in enumerate(document):
        if not isinstance(value, Mapping):
            return (), (
                _issue(
                    locator=ManualImportLocator(
                        kind="json_index",
                        index=offset + 1,
                        pointer=f"$[{offset}]",
                    ),
                    code="MANUAL_IMPORT_JSON_RECORD_INVALID",
                    message="Every JSON array item must be an object.",
                    context={"actualType": type(value).__name__},
                ),
            )
        records.append(
            (
                ManualImportLocator(
                    kind="json_index",
                    index=offset + 1,
                    pointer=f"$[{offset}]",
                ),
                value,
            )
        )
    return tuple(records), ()


def _parse_jsonl(
    text: str,
) -> tuple[
    tuple[tuple[ManualImportLocator, Mapping[str, object]], ...],
    tuple[ManualImportIssue, ...],
]:
    records: list[tuple[ManualImportLocator, Mapping[str, object]]] = []
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
            return (), (
                _issue(
                    locator=locator,
                    code="MANUAL_IMPORT_JSONL_MALFORMED",
                    message="A JSON Lines record is malformed or contains duplicate keys.",
                    context={"detail": str(exc)[:300]},
                ),
            )
        if not isinstance(value, Mapping):
            return (), (
                _issue(
                    locator=locator,
                    code="MANUAL_IMPORT_JSONL_RECORD_INVALID",
                    message="Every non-empty JSON Lines record must be an object.",
                    context={"actualType": type(value).__name__},
                ),
            )
        records.append((locator, value))
    return tuple(records), ()


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


def _reject_non_finite_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number is not supported: {value}")


def _normalize_record(record: Mapping[str, object]) -> dict[str, str | None]:
    normalized: dict[str, str | None] = {}
    for key, value in record.items():
        if not isinstance(key, str):
            raise _UnsupportedJsonValue("manual import record keys must be strings")
        normalized[key] = _normalize_scalar(value)
    return normalized


def _normalize_scalar(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _UnsupportedJsonValue("non-finite numeric values are not supported")
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    raise _UnsupportedJsonValue(
        f"nested or unsupported value type: {type(value).__name__}"
    )


def _assemble_plan(
    *,
    source_digest: str,
    source_size_bytes: int,
    format: ManualImportFormat,
    mode: ManualImportMode,
    records: tuple[ManualImportRecord, ...],
    issues: tuple[ManualImportIssue, ...],
) -> ManualImportPlan:
    if not issues:
        disposition = ManualImportDisposition.ACCEPTED
    elif mode is ManualImportMode.PARTIAL and records:
        disposition = ManualImportDisposition.PARTIAL
    else:
        disposition = ManualImportDisposition.REJECTED
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
        "records": [item.model_dump(mode="json", by_alias=True) for item in records],
        "issues": [item.model_dump(mode="json", by_alias=True) for item in issues],
    }
    return ManualImportPlan.model_validate(
        {
            **payload,
            "planDigest": _digest_json(payload),
        }
    )


def _issue(
    *,
    code: str,
    message: str,
    context: dict[str, str | int | bool | None],
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
        return _canonical_json(exc.errors(include_url=False))[:1_000]
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
''',
    )


def create_worker_app() -> None:
    write(
        "apps/manual_import_worker/pyproject.toml",
        '''[project]
name = "manual-import-worker"
version = "0.1.0"
description = "Isolated manual import planning process for the Data Collection Platform"
requires-python = ">=3.13,<3.14"
dependencies = [
  "collection-contracts",
  "manual-import-core",
]

[project.scripts]
manual-import-worker = "manual_import_worker.app:main"

[build-system]
requires = ["hatchling==1.31.0"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/manual_import_worker"]

[tool.uv.sources]
collection-contracts = { workspace = true }
manual-import-core = { workspace = true }
''',
    )
    write(
        "apps/manual_import_worker/src/manual_import_worker/__init__.py",
        '''from manual_import_worker.app import main

__all__ = ("main",)
''',
    )
    write(
        "apps/manual_import_worker/src/manual_import_worker/__main__.py",
        '''from manual_import_worker.app import main

raise SystemExit(main())
''',
    )
    write(
        "apps/manual_import_worker/src/manual_import_worker/app.py",
        '''from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from collection_contracts import ErrorEnvelope, ManualImportDisposition, ManualImportFormat, ManualImportMode
from manual_import_core import build_manual_import_plan, canonical_manual_import_plan_json

_REJECTED_EXIT_CODE = 2
_RUNTIME_FAILURE_EXIT_CODE = 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    if arguments.command != "plan":
        parser.error("a command is required")
    try:
        content = Path(arguments.file).read_bytes()
        plan = build_manual_import_plan(
            content,
            format=ManualImportFormat(arguments.format),
            mode=ManualImportMode(arguments.mode),
        )
        _write_output(arguments.output, canonical_manual_import_plan_json(plan))
    except (OSError, ValueError) as exc:
        _write_error(exc)
        return _RUNTIME_FAILURE_EXIT_CODE
    if plan.disposition is ManualImportDisposition.REJECTED:
        return _REJECTED_EXIT_CODE
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manual-import-worker")
    subcommands = parser.add_subparsers(dest="command", required=True)
    plan = subcommands.add_parser(
        "plan",
        help="Create a deterministic immutable plan and error ledger for one manual import file.",
    )
    plan.add_argument("file")
    plan.add_argument(
        "--format",
        required=True,
        choices=tuple(value.value for value in ManualImportFormat),
    )
    plan.add_argument(
        "--mode",
        default=ManualImportMode.ATOMIC.value,
        choices=tuple(value.value for value in ManualImportMode),
    )
    plan.add_argument("--output", default="-")
    return parser


def _write_output(destination: str, content: str) -> None:
    if destination == "-":
        sys.stdout.write(content)
        return
    target = Path(destination)
    parent = target.parent
    if not parent.is_dir():
        raise OSError(f"output directory does not exist: {parent}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _write_error(exc: Exception) -> None:
    envelope = ErrorEnvelope(
        type="collection/manual-import-worker-failed",
        owner="ManualImportWorker",
        code="MANUAL_IMPORT_WORKER_FAILED",
        message="The manual import worker could not create the requested plan.",
        context={"causeType": type(exc).__name__, "detail": str(exc)[:500]},
        required_action="Correct the file path or output destination and retry the exact command.",
        correlation_id="manual-import-worker-cli",
    )
    sys.stderr.write(
        json.dumps(
            envelope.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
''',
    )
    write(
        "deploy/docker/manual-import-worker.Dockerfile",
        '''FROM ghcr.io/astral-sh/uv:0.10.0 AS uv

FROM python:3.13.14-slim AS builder
COPY --from=uv /uv /uvx /bin/
WORKDIR /workspace
COPY pyproject.toml uv.lock .python-version ./
COPY apps ./apps
COPY packages ./packages
RUN uv sync --frozen --package manual-import-worker --no-dev --no-editable

FROM python:3.13.14-slim AS runtime
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN addgroup --system --gid 10001 collector \
    && adduser --system --uid 10001 --ingroup collector --home /nonexistent collector
COPY --from=builder /workspace/.venv /opt/venv
USER 10001:10001
ENTRYPOINT ["manual-import-worker"]
''',
    )


def create_tests() -> None:
    write(
        "packages/collection_contracts/tests/test_manual_import.py",
        '''from __future__ import annotations

import pytest
from pydantic import ValidationError

from collection_contracts import (
    ManualImportDisposition,
    ManualImportFormat,
    ManualImportIssue,
    ManualImportMode,
    ManualImportPlan,
)


def test_manual_import_plan_rejects_inconsistent_summary() -> None:
    with pytest.raises(ValidationError):
        ManualImportPlan(
            source_digest="sha256:" + "1" * 64,
            source_size_bytes=1,
            format=ManualImportFormat.JSON,
            mode=ManualImportMode.ATOMIC,
            disposition=ManualImportDisposition.ACCEPTED,
            valid_record_count=0,
            issue_count=1,
            issues=(
                ManualImportIssue(
                    code="MANUAL_IMPORT_INVALID",
                    message="invalid",
                ),
            ),
            plan_digest="sha256:" + "2" * 64,
        )
''',
    )
    write(
        "packages/manual_import_core/tests/test_planner.py",
        '''from __future__ import annotations

import json
from pathlib import Path

from collection_contracts import ManualImportDisposition, ManualImportFormat, ManualImportMode
from manual_import_core import (
    build_manual_import_plan,
    canonical_manual_import_plan_json,
    schedulable_manual_import_records,
)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_campaign_csv_fixture_has_deterministic_plan() -> None:
    content = (
        _repository_root()
        / "campaigns"
        / "berlin_recording_services"
        / "discovery"
        / "manual_seeds.csv"
    ).read_bytes()
    first = build_manual_import_plan(content, format=ManualImportFormat.CSV)
    second = build_manual_import_plan(content, format=ManualImportFormat.CSV)

    assert first == second
    assert first.disposition is ManualImportDisposition.ACCEPTED
    assert canonical_manual_import_plan_json(first) == canonical_manual_import_plan_json(second)


def test_duplicate_json_key_is_rejected_with_file_ledger() -> None:
    plan = build_manual_import_plan(
        b'{"entityKey":"one","entityKey":"two"}',
        format=ManualImportFormat.JSON,
    )

    assert plan.disposition is ManualImportDisposition.REJECTED
    assert plan.issues[0].code == "MANUAL_IMPORT_JSON_MALFORMED"
    assert schedulable_manual_import_records(plan) == ()


def test_jsonl_reports_exact_physical_line() -> None:
    plan = build_manual_import_plan(
        b'\n{"nested":{"value":1}}\n',
        format=ManualImportFormat.JSONL,
        mode=ManualImportMode.PARTIAL,
    )

    assert plan.disposition is ManualImportDisposition.REJECTED
    assert plan.issues[0].locator is not None
    assert plan.issues[0].locator.kind == "jsonl_line"
    assert plan.issues[0].locator.index == 2


def test_plan_digest_covers_error_ledger() -> None:
    first = build_manual_import_plan(b"not-json", format=ManualImportFormat.JSON)
    second = build_manual_import_plan(b"still-not-json", format=ManualImportFormat.JSON)

    assert first.plan_digest != second.plan_digest
    payload = json.loads(canonical_manual_import_plan_json(first))
    assert payload["planDigest"] == first.plan_digest
''',
    )
    write(
        "apps/manual_import_worker/tests/test_cli.py",
        '''from __future__ import annotations

import json
from pathlib import Path

from manual_import_worker.app import main


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_plan_command_writes_canonical_ledger(tmp_path: Path) -> None:
    source = (
        _repository_root()
        / "campaigns"
        / "berlin_recording_services"
        / "discovery"
        / "manual_seeds.csv"
    )
    output = tmp_path / "plan.json"

    result = main(
        [
            "plan",
            str(source),
            "--format",
            "csv",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["contract"] == "collector-manual-import-plan"
    assert payload["disposition"] == "accepted"


def test_rejected_plan_is_written_and_returns_distinct_exit(tmp_path: Path) -> None:
    source = tmp_path / "invalid.json"
    source.write_text("not-json", encoding="utf-8")
    output = tmp_path / "plan.json"

    result = main(
        [
            "plan",
            str(source),
            "--format",
            "json",
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert json.loads(output.read_text(encoding="utf-8"))["disposition"] == "rejected"
''',
    )


def patch_contract_generation() -> None:
    relative = "tools/contract_generation/generate.py"
    text = read(relative)
    if "from collection_contracts.manual_import import ManualImportPlan" not in text:
        import_anchor = "from collection_contracts import ("
        index = text.find(import_anchor)
        if index < 0:
            raise RuntimeError("contract generation import block was not found")
        text = text[:index] + "from collection_contracts.manual_import import ManualImportPlan\n" + text[index:]
    if '"manual-import-plan.schema.json"' not in text:
        candidates = (
            '"manual-seed-row.schema.json": ManualSeedRow,',
            '"campaign-snapshot.schema.json": CampaignSnapshot,',
        )
        for anchor in candidates:
            if anchor in text:
                text = text.replace(
                    anchor,
                    anchor + '\n    "manual-import-plan.schema.json": ManualImportPlan,',
                    1,
                )
                break
        else:
            raise RuntimeError("contract generation schema registry anchor was not found")
    write(relative, text)


def patch_architecture() -> None:
    relative = "tools/architecture_checks/check_dependencies.py"
    text = read(relative)
    prefix_match = re.search(r"_INTERNAL_MODULE_PREFIXES\s*=\s*\((?P<body>[^)]*)\)", text)
    if prefix_match is None:
        raise RuntimeError("architecture internal module prefix registry was not found")
    prefix_body = prefix_match.group("body")
    if '"manual_import_"' not in prefix_body:
        replacement = prefix_match.group(0)[:-1].rstrip()
        if not replacement.endswith(","):
            replacement += ","
        replacement += ' "manual_import_")'
        text = text[: prefix_match.start()] + replacement + text[prefix_match.end() :]

    if 'distribution="manual-import-core"' not in text:
        marker = '    OwnerRule(\n        distribution="collection-infrastructure"'
        index = text.find(marker)
        if index < 0:
            raise RuntimeError("architecture owner insertion marker was not found")
        rules = '''    OwnerRule(
        distribution="manual-import-core",
        member="packages/manual_import_core",
        module="manual_import_core",
        allowed_internal_dependencies=frozenset({"collection-contracts"}),
        allowed_external_dependencies=frozenset({"pydantic"}),
    ),
    OwnerRule(
        distribution="manual-import-worker",
        member="apps/manual_import_worker",
        module="manual_import_worker",
        allowed_internal_dependencies=frozenset(
            {"collection-contracts", "manual-import-core"}
        ),
        allowed_external_dependencies=frozenset(),
    ),
'''
        text = text[:index] + rules + text[index:]
    write(relative, text)


def patch_ci() -> None:
    relative = ".github/workflows/ci.yml"
    text = read(relative)
    if "manual-import-worker.Dockerfile" in text:
        return
    anchors = (
        "docker build --file deploy/docker/worker-gateway.Dockerfile .",
        "docker build --file deploy/docker/migration.Dockerfile .",
    )
    for anchor in anchors:
        if anchor in text:
            text = text.replace(
                anchor,
                anchor + "\n          docker build --file deploy/docker/manual-import-worker.Dockerfile .",
                1,
            )
            write(relative, text)
            return
    raise RuntimeError("CI Docker build anchor was not found")


def patch_docs() -> None:
    write(
        ".codex/modules/manual-import.md",
        '''# Manual import module

## Owners

- `collection_contracts.manual_import` owns the immutable plan, locator, record, issue, mode, and disposition wire contracts.
- `manual_import_core` owns byte decoding, CSV/JSON/JSONL parsing, canonical row validation, record identity, plan identity, and deterministic error-ledger assembly.
- `manual_import_worker` is the isolated process that reads one mounted input file and emits one immutable canonical plan artifact. It has no SQLAlchemy, PostgreSQL, object-store credential, or publication dependency.

## Invariants

- UTF-8, byte, and record limits fail closed.
- CSV headers are exact and ordered; JSON duplicate keys and non-finite numbers are rejected.
- Every issue preserves an exact file-level or record-level locator.
- Atomic mode is the default. Partial disposition is possible only when partial mode was selected explicitly and at least one valid record and one issue both exist.
- The source digest, record digests, complete issue ledger, and plan digest are deterministic.
- A rejected plan schedules no records. The future Gateway expansion owner must consume only `schedulable_manual_import_records` and must create one durable work unit per returned record.

## Current runtime boundary

The worker currently produces the immutable plan and error ledger from a local mounted file. Uploading the original file through the Worker Gateway, atomically expanding valid records into one work unit per row/object, and processing those child units remain the next owner batch. No temporary direct database path is permitted.
''',
    )

    relative = "docs/architecture/dependency-rules.md"
    text = read(relative)
    if "`manual-import-core`" not in text:
        marker = "| `collection-infrastructure`"
        index = text.find(marker)
        if index < 0:
            raise RuntimeError("dependency rules table insertion marker was not found")
        rows = (
            "| `manual-import-core` | `collection-contracts` | `pydantic` |\n"
            "| `manual-import-worker` | `collection-contracts`, `manual-import-core` | none |\n"
        )
        text = text[:index] + rows + text[index:]
        write(relative, text)

    relative = "docs/architecture/owner-map.md"
    text = read(relative)
    if "`manual-import-core`" not in text:
        marker = "| `collection-infrastructure`"
        index = text.find(marker)
        if index < 0:
            raise RuntimeError("owner map table insertion marker was not found")
        rows = (
            "| `manual-import-core` | `packages/manual_import_core` | `manual_import_core` | Deterministic CSV/JSON/JSONL planning and error-ledger ownership |\n"
            "| `manual-import-worker` | `apps/manual_import_worker` | `manual_import_worker` | Isolated operator/connector process producing immutable manual-import plans |\n"
        )
        text = text[:index] + rows + text[index:]
        write(relative, text)

    relative = "docs/implementation-status.md"
    text = read(relative)
    if "Manual import planning" not in text:
        table_anchor = "| Architecture proof |"
        index = text.find(table_anchor)
        if index < 0:
            raise RuntimeError("implementation status table anchor was not found")
        line_end = text.find("\n", index)
        row = (
            "\n| Manual import planning | Strict UTF-8 CSV/JSON/JSONL planner, exact locators, atomic/partial semantics, deterministic record/plan digests, canonical error ledger, isolated worker image |"
        )
        text = text[:line_end] + row + text[line_end:]
    incomplete = "- no runtime manual-import intake, Gateway plan expansion, or one-record-per-work scheduling;"
    if incomplete not in text:
        heading = "## Explicitly incomplete"
        index = text.find(heading)
        if index < 0:
            raise RuntimeError("implementation status incomplete section was not found")
        line_end = text.find("\n", index) + 1
        text = text[:line_end] + "\n" + incomplete + "\n" + text[line_end:]
    write(relative, text)

    relative = "README.md"
    text = read(relative)
    if "manual-import-worker plan" not in text:
        text += '''

## Manual import planning

Create a deterministic immutable plan and error ledger without database credentials:

```text
uv run manual-import-worker plan input.csv --format csv --mode atomic --output plan.json
```

`atomic` is the default. `partial` must be selected explicitly; a rejected plan returns exit code `2` after writing its complete ledger.
'''
        write(relative, text)


def main() -> None:
    create_contracts()
    create_core_package()
    create_worker_app()
    create_tests()
    add_workspace_member("packages/manual_import_core")
    add_workspace_member("apps/manual_import_worker")
    add_mypy_path("packages/manual_import_core/src")
    add_mypy_path("apps/manual_import_worker/src")
    patch_contract_generation()
    patch_architecture()
    patch_ci()
    patch_docs()


if __name__ == "__main__":
    main()
