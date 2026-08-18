from __future__ import annotations

import pytest
from pydantic import ValidationError

from collection_contracts import (
    ManualImportDisposition,
    ManualImportFormat,
    ManualImportMode,
    ManualImportPlan,
    ManualImportRecordDocument,
)

_DIGEST = "sha256:" + "1" * 64


def _plan(**overrides: object) -> ManualImportPlan:
    payload: dict[str, object] = {
        "sourceDigest": _DIGEST,
        "sourceSizeBytes": 10,
        "format": ManualImportFormat.CSV,
        "mode": ManualImportMode.ATOMIC,
        "disposition": ManualImportDisposition.ACCEPTED,
        "validRecordCount": 0,
        "issueCount": 0,
        "records": (),
        "issues": (),
        "planDigest": _DIGEST,
    }
    payload.update(overrides)
    return ManualImportPlan.model_validate(payload)


def _record_document(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "sourceDigest": _DIGEST,
        "sourceArtifactRole": "manual_source:csv:atomic",
        "planDigest": _DIGEST,
        "planArtifactDigest": _DIGEST,
        "planRecordPosition": 0,
        "locator": {"kind": "csv_row", "index": 2, "pointer": "line:2"},
        "recordDigest": _DIGEST,
        "record": {
            "row_number": 2,
            "expected_entity_kind": "place",
            "display_name": "Studio",
            "website": None,
            "osm_id": None,
            "reference_urls": [],
            "note": None,
            "provenance": "test",
        },
        "contentDigest": _DIGEST,
    }
    payload.update(overrides)
    return payload


def test_manual_import_plan_rejects_inconsistent_counts() -> None:
    with pytest.raises(ValidationError, match="valid_record_count"):
        _plan(validRecordCount=1)


def test_manual_import_plan_rejects_partial_disposition_without_partial_mode() -> None:
    with pytest.raises(ValidationError, match="disposition"):
        _plan(
            disposition=ManualImportDisposition.PARTIAL,
            issueCount=1,
            issues=(
                {
                    "code": "MANUAL_IMPORT_RECORD_INVALID",
                    "message": "invalid",
                },
            ),
        )


def test_manual_import_plan_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ManualImportPlan.model_validate(
            {
                **_plan().model_dump(mode="json", by_alias=True),
                "unexpected": True,
            }
        )


def test_manual_import_record_document_rejects_locator_row_mismatch() -> None:
    with pytest.raises(ValidationError, match="row number"):
        ManualImportRecordDocument.model_validate(
            _record_document(
                record={
                    **_record_document()["record"],  # type: ignore[dict-item]
                    "row_number": 3,
                }
            )
        )


def test_manual_import_record_document_rejects_noncanonical_source_role() -> None:
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        ManualImportRecordDocument.model_validate(
            _record_document(sourceArtifactRole="manual_source:csv:accept_valid")
        )


def test_manual_import_record_document_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ManualImportRecordDocument.model_validate({**_record_document(), "unexpected": True})
