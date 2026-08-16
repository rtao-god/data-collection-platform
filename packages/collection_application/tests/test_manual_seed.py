from __future__ import annotations

import json

import pytest

from collection_application.manual_seed import read_manual_seed_records
from collection_contracts import ManualImportDisposition, OwnerContextError

_VALID_JSON_RECORD = {
    "expected_entity_kind": "place",
    "display_name": "Example Studio",
    "website": "https://example.test",
    "osm_id": None,
    "reference_urls": ["https://example.test/contact"],
    "note": None,
    "provenance": "operator fixture",
}


def _read(
    raw: bytes,
    *,
    format: str,
    partial_mode: bool = False,
    partial_mode_allowed: bool = False,
):
    return read_manual_seed_records(
        raw,
        path=f"manual.{format}",
        format=format,  # type: ignore[arg-type]
        max_file_bytes=1_048_576,
        partial_mode=partial_mode,
        partial_mode_allowed=partial_mode_allowed,
        require_records=True,
        correlation_id="manual-seed-test",
    )


def test_json_object_is_parsed_as_one_typed_record() -> None:
    plan = _read(json.dumps(_VALID_JSON_RECORD).encode(), format="json")

    assert plan.disposition is ManualImportDisposition.ACCEPTED
    assert plan.valid_record_count == 1
    assert plan.records[0].record.row_number == 1
    assert plan.records[0].record.display_name == "Example Studio"


def test_json_lines_rejection_preserves_every_row_context() -> None:
    invalid_shape = {**_VALID_JSON_RECORD, "unknown": "value"}
    raw = b"\n".join(
        (
            json.dumps(_VALID_JSON_RECORD).encode(),
            b"{not-json}",
            json.dumps(invalid_shape).encode(),
        )
    )

    with pytest.raises(OwnerContextError) as captured:
        _read(raw, format="jsonl")

    envelope = captured.value.envelope
    assert envelope.code == "MANUAL_SEED_FILE_INVALID"
    assert envelope.context["validRecordCount"] == 1
    assert envelope.context["invalidRecordCount"] == 2
    issues = envelope.context["issues"]
    assert [issue["locator"]["index"] for issue in issues] == [2, 3]


def test_partial_mode_is_explicit_and_policy_gated() -> None:
    invalid = {**_VALID_JSON_RECORD, "display_name": ""}
    raw = json.dumps([_VALID_JSON_RECORD, invalid]).encode()

    with pytest.raises(OwnerContextError) as captured:
        _read(raw, format="json", partial_mode=True, partial_mode_allowed=False)
    assert captured.value.envelope.code == "MANUAL_SEED_PARTIAL_MODE_FORBIDDEN"

    plan = _read(
        raw,
        format="json",
        partial_mode=True,
        partial_mode_allowed=True,
    )
    assert plan.disposition is ManualImportDisposition.PARTIAL
    assert plan.valid_record_count == 1
    assert plan.issue_count == 1


def test_duplicate_json_key_maps_to_existing_owner_error_contract() -> None:
    raw = (
        b'{"expected_entity_kind":"place","expected_entity_kind":"provider",'
        b'"display_name":"Duplicate","website":null,"osm_id":null,'
        b'"reference_urls":[],"note":null,"provenance":"fixture"}'
    )

    with pytest.raises(OwnerContextError) as captured:
        _read(raw, format="json")

    assert captured.value.envelope.code == "MANUAL_SEED_JSON_INVALID"
    assert captured.value.envelope.context["issues"][0]["code"] == ("MANUAL_IMPORT_JSON_MALFORMED")


def test_csv_reports_all_invalid_rows_in_one_error_ledger() -> None:
    raw = b"\n".join(
        (
            b"expected_entity_kind,display_name,website,osm_id,reference_urls,note,provenance",
            b"place,,https://example.test,,,,",
            b"provider,Valid Provider,not-a-url,,,,operator fixture",
        )
    )

    with pytest.raises(OwnerContextError) as captured:
        _read(raw, format="csv")

    envelope = captured.value.envelope
    assert envelope.code == "MANUAL_SEED_FILE_INVALID"
    assert envelope.context["invalidRecordCount"] == 2
    issues = envelope.context["issues"]
    assert [issue["locator"]["index"] for issue in issues] == [2, 3]


def test_file_size_is_checked_by_canonical_planner() -> None:
    with pytest.raises(OwnerContextError) as captured:
        read_manual_seed_records(
            b"{}",
            path="manual.json",
            format="json",
            max_file_bytes=1,
            partial_mode=False,
            partial_mode_allowed=False,
            require_records=True,
            correlation_id="manual-seed-test",
        )

    assert captured.value.envelope.code == "MANUAL_SEED_SIZE_INVALID"
