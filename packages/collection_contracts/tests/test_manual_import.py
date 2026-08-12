from __future__ import annotations

import pytest
from pydantic import ValidationError

from collection_contracts import (
    ManualImportDisposition,
    ManualImportFormat,
    ManualImportMode,
    ManualImportPlan,
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
