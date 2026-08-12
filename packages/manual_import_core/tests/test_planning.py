from __future__ import annotations

import json

from manual_import_core import (
    ManualImportFormat,
    ManualImportMode,
    ManualImportPlanStatus,
    build_manual_import_plan,
)


def test_csv_json_and_jsonl_produce_the_same_record_digests() -> None:
    csv_plan = build_manual_import_plan(
        b"name,active,count\nStudio,true,2\n",
        format=ManualImportFormat.CSV,
        mode=ManualImportMode.ATOMIC,
    )
    json_plan = build_manual_import_plan(
        b'[{"count":2,"active":true,"name":"Studio"}]',
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )
    jsonl_plan = build_manual_import_plan(
        b'{"name":"Studio","active":true,"count":2}\n',
        format=ManualImportFormat.JSONL,
        mode=ManualImportMode.ATOMIC,
    )

    assert csv_plan.status is ManualImportPlanStatus.READY
    assert [item.record_digest for item in csv_plan.records] == [
        item.record_digest for item in json_plan.records
    ] == [item.record_digest for item in jsonl_plan.records]


def test_atomic_mode_blocks_every_record_when_one_record_is_invalid() -> None:
    plan = build_manual_import_plan(
        b'{"name":"ok"}\n{"name":{"nested":true}}\n',
        format=ManualImportFormat.JSONL,
        mode=ManualImportMode.ATOMIC,
    )

    assert plan.status is ManualImportPlanStatus.BLOCKED
    assert plan.records == ()
    assert [issue.code for issue in plan.issues] == ["MANUAL_IMPORT_RECORD_INVALID"]


def test_partial_mode_preserves_valid_records_and_error_ledger() -> None:
    body = b'{"name":"ok"}\nnot-json\n{"name":{"nested":true}}\n'
    first = build_manual_import_plan(
        body,
        format=ManualImportFormat.JSONL,
        mode=ManualImportMode.PARTIAL,
    )
    second = build_manual_import_plan(
        body,
        format=ManualImportFormat.JSONL,
        mode=ManualImportMode.PARTIAL,
    )

    assert first.status is ManualImportPlanStatus.READY
    assert len(first.records) == 1
    assert [issue.locator.value for issue in first.issues if issue.locator] == ["2", "3"]
    assert first.to_bytes() == second.to_bytes()
    assert first.digest == second.digest
    assert json.loads(first.to_bytes())["issueCount"] == 2


def test_duplicate_json_keys_are_rejected() -> None:
    plan = build_manual_import_plan(
        b'{"name":"one","name":"two"}',
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )

    assert plan.status is ManualImportPlanStatus.BLOCKED
    assert plan.issues[0].code == "MANUAL_IMPORT_JSON_INVALID"


def test_csv_headers_must_be_unique() -> None:
    plan = build_manual_import_plan(
        b"name,name\none,two\n",
        format=ManualImportFormat.CSV,
        mode=ManualImportMode.ATOMIC,
    )

    assert plan.status is ManualImportPlanStatus.BLOCKED
    assert plan.issues[0].code == "MANUAL_IMPORT_CSV_HEADER_INVALID"
