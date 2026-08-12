from __future__ import annotations

import json
from pathlib import Path

import pytest

from collection_contracts import (
    ManualImportDisposition,
    ManualImportFormat,
    ManualImportMode,
)
from manual_import_core import (
    ManualImportPlanIntegrityError,
    build_manual_import_plan,
    canonical_manual_import_plan_json,
    schedulable_manual_import_records,
    verify_manual_import_plan,
)

_HEADERS = "expected_entity_kind,display_name,website,osm_id,reference_urls,note,provenance"
_VALID_RECORD = {
    "expected_entity_kind": "place",
    "display_name": "Example Studio",
    "website": "https://example.test",
    "osm_id": None,
    "reference_urls": ["https://example.test/contact"],
    "note": None,
    "provenance": "operator fixture",
}


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

    first = build_manual_import_plan(
        content,
        format=ManualImportFormat.CSV,
        require_records=False,
    )
    second = build_manual_import_plan(
        content,
        format=ManualImportFormat.CSV,
        require_records=False,
    )

    assert first == second
    assert first.disposition is ManualImportDisposition.ACCEPTED
    assert canonical_manual_import_plan_json(first) == canonical_manual_import_plan_json(second)


def test_csv_uses_physical_line_as_row_number_and_locator() -> None:
    content = (
        _HEADERS
        + "\nplace,Example Studio,https://example.test,,https://example.test/contact,,fixture\n"
    ).encode()

    plan = build_manual_import_plan(content, format=ManualImportFormat.CSV)

    assert plan.disposition is ManualImportDisposition.ACCEPTED
    assert plan.records[0].locator.kind == "csv_row"
    assert plan.records[0].locator.index == 2
    assert plan.records[0].record.row_number == 2


def test_json_array_collects_complete_record_ledger() -> None:
    invalid_shape = {**_VALID_RECORD, "unknown": "value"}
    invalid_type = {**_VALID_RECORD, "reference_urls": {"nested": True}}
    content = json.dumps([_VALID_RECORD, invalid_shape, invalid_type]).encode()

    plan = build_manual_import_plan(
        content,
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.PARTIAL,
    )

    assert plan.disposition is ManualImportDisposition.PARTIAL
    assert plan.valid_record_count == 1
    assert plan.issue_count == 2
    assert [issue.locator.index for issue in plan.issues if issue.locator] == [2, 3]
    assert schedulable_manual_import_records(plan) == plan.records


def test_atomic_mode_preserves_ledger_but_schedules_nothing() -> None:
    invalid = {**_VALID_RECORD, "display_name": ""}
    content = json.dumps([_VALID_RECORD, invalid]).encode()

    plan = build_manual_import_plan(content, format=ManualImportFormat.JSON)

    assert plan.disposition is ManualImportDisposition.REJECTED
    assert plan.valid_record_count == 1
    assert plan.issue_count == 1
    assert schedulable_manual_import_records(plan) == ()


def test_json_does_not_accept_csv_reference_encoding() -> None:
    content = json.dumps({**_VALID_RECORD, "reference_urls": "https://example.test"}).encode()

    plan = build_manual_import_plan(content, format=ManualImportFormat.JSON)

    assert plan.disposition is ManualImportDisposition.REJECTED
    assert plan.issues[0].code == "MANUAL_IMPORT_RECORD_INVALID"


def test_jsonl_reports_exact_physical_lines_for_all_invalid_records() -> None:
    content = b"\n".join(
        (
            json.dumps(_VALID_RECORD).encode(),
            b"",
            b"{not-json}",
            json.dumps({**_VALID_RECORD, "display_name": ""}).encode(),
        )
    )

    plan = build_manual_import_plan(
        content,
        format=ManualImportFormat.JSONL,
        mode=ManualImportMode.PARTIAL,
    )

    assert plan.disposition is ManualImportDisposition.PARTIAL
    assert [issue.locator.index for issue in plan.issues if issue.locator] == [3, 4]


def test_duplicate_json_key_is_rejected_at_file_boundary() -> None:
    plan = build_manual_import_plan(
        b'{"display_name":"one","display_name":"two"}',
        format=ManualImportFormat.JSON,
    )

    assert plan.disposition is ManualImportDisposition.REJECTED
    assert plan.issues[0].code == "MANUAL_IMPORT_JSON_MALFORMED"


def test_non_finite_number_is_rejected() -> None:
    content = json.dumps(_VALID_RECORD).replace('"note": null', '"note": NaN').encode()

    plan = build_manual_import_plan(content, format=ManualImportFormat.JSON)

    assert plan.disposition is ManualImportDisposition.REJECTED
    assert plan.issues[0].code == "MANUAL_IMPORT_JSON_MALFORMED"


def test_file_boundary_errors_are_typed() -> None:
    empty = build_manual_import_plan(b"", format=ManualImportFormat.CSV)
    oversized = build_manual_import_plan(
        b"ab",
        format=ManualImportFormat.CSV,
        max_file_bytes=1,
    )
    invalid_utf8 = build_manual_import_plan(b"\xff", format=ManualImportFormat.JSON)

    assert empty.issues[0].code == "MANUAL_IMPORT_FILE_EMPTY"
    assert oversized.issues[0].code == "MANUAL_IMPORT_FILE_TOO_LARGE"
    assert invalid_utf8.issues[0].code == "MANUAL_IMPORT_UTF8_INVALID"


def test_record_limit_counts_valid_and_invalid_records() -> None:
    content = json.dumps([_VALID_RECORD, {**_VALID_RECORD, "display_name": ""}]).encode()

    plan = build_manual_import_plan(
        content,
        format=ManualImportFormat.JSON,
        max_records=1,
    )

    assert plan.disposition is ManualImportDisposition.REJECTED
    assert plan.issues[0].code == "MANUAL_IMPORT_RECORD_LIMIT_EXCEEDED"


def test_empty_valid_document_can_be_allowed_only_by_explicit_caller_contract() -> None:
    required = build_manual_import_plan(b"[]", format=ManualImportFormat.JSON)
    optional = build_manual_import_plan(
        b"[]",
        format=ManualImportFormat.JSON,
        require_records=False,
    )

    assert required.disposition is ManualImportDisposition.REJECTED
    assert required.issues[0].code == "MANUAL_IMPORT_NO_RECORDS"
    assert optional.disposition is ManualImportDisposition.ACCEPTED


def test_plan_digest_covers_source_records_and_issue_ledger() -> None:
    first = build_manual_import_plan(b"not-json", format=ManualImportFormat.JSON)
    second = build_manual_import_plan(b"still-not-json", format=ManualImportFormat.JSON)

    assert first.plan_digest != second.plan_digest
    verify_manual_import_plan(first)
    payload = json.loads(canonical_manual_import_plan_json(first))
    assert payload["planDigest"] == first.plan_digest

    tampered = first.model_copy(update={"source_size_bytes": first.source_size_bytes + 1})
    with pytest.raises(ManualImportPlanIntegrityError):
        verify_manual_import_plan(tampered)
