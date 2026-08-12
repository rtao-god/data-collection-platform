from __future__ import annotations

import json

import pytest

from collection_application.manual_seed import read_manual_seed_records
from collection_contracts import OwnerContextError

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
    result = _read(json.dumps(_VALID_JSON_RECORD).encode(), format="json")

    assert result.record_count == 1
    assert result.issues == ()
    assert result.rows[0].row_number == 1
    assert result.rows[0].display_name == "Example Studio"
    assert tuple(str(value) for value in result.rows[0].reference_urls) == (
        "https://example.test/contact",
    )


def test_json_array_preserves_record_ordinal() -> None:
    second = {**_VALID_JSON_RECORD, "display_name": "Second Studio"}

    result = _read(json.dumps([_VALID_JSON_RECORD, second]).encode(), format="json")

    assert [row.row_number for row in result.rows] == [1, 2]
    assert [row.display_name for row in result.rows] == ["Example Studio", "Second Studio"]


def test_json_lines_collects_every_invalid_record_with_physical_line_context() -> None:
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
    assert [issue["lineNumber"] for issue in issues] == [2, 3]
    assert [issue["recordNumber"] for issue in issues] == [2, 3]


def test_partial_mode_is_explicit_and_policy_gated() -> None:
    invalid = {**_VALID_JSON_RECORD, "display_name": ""}
    raw = json.dumps([_VALID_JSON_RECORD, invalid]).encode()

    with pytest.raises(OwnerContextError) as captured:
        _read(
            raw,
            format="json",
            partial_mode=True,
            partial_mode_allowed=False,
        )
    assert captured.value.envelope.code == "MANUAL_SEED_PARTIAL_MODE_FORBIDDEN"

    result = _read(
        raw,
        format="json",
        partial_mode=True,
        partial_mode_allowed=True,
    )
    assert len(result.rows) == 1
    assert len(result.issues) == 1
    assert result.issues[0].record_number == 2


def test_duplicate_json_key_is_rejected_at_file_boundary() -> None:
    raw = (
        b'{"expected_entity_kind":"place","expected_entity_kind":"provider",'
        b'"display_name":"Duplicate","website":null,"osm_id":null,'
        b'"reference_urls":[],"note":null,"provenance":"fixture"}'
    )

    with pytest.raises(OwnerContextError) as captured:
        _read(raw, format="json")

    assert captured.value.envelope.code == "MANUAL_SEED_JSON_INVALID"
    assert "duplicate JSON key" in str(captured.value.envelope.context["detail"])


def test_non_finite_json_number_is_rejected() -> None:
    raw = json.dumps(_VALID_JSON_RECORD).replace('"note": null', '"note": NaN').encode()

    with pytest.raises(OwnerContextError) as captured:
        _read(raw, format="json")

    assert captured.value.envelope.code == "MANUAL_SEED_JSON_INVALID"


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
    assert [issue["lineNumber"] for issue in envelope.context["issues"]] == [2, 3]


def test_file_size_is_checked_before_parsing() -> None:
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
