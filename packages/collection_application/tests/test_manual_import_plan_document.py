from __future__ import annotations

import json
from hashlib import sha256
from uuid import UUID

import pytest

from collection_application.manual_import_plan_document import (
    ManualImportPlanDocumentError,
    decode_manual_import_plan,
)
from collection_contracts import ManualImportFormat, ManualImportMode
from manual_import_core import (
    build_manual_import_plan,
    canonical_manual_import_plan_json,
)

_PLAN_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000301")
_SOURCE_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000302")


def test_decoder_binds_exact_canonical_artifact_and_semantic_digests() -> None:
    plan = _plan()
    payload = canonical_manual_import_plan_json(plan).encode("utf-8")

    decoded = decode_manual_import_plan(
        payload,
        plan_artifact_id=_PLAN_ARTIFACT_ID,
        plan_artifact_digest=_digest(payload),
        source_artifact_id=_SOURCE_ARTIFACT_ID,
        source_artifact_role="manual_import_source:json:atomic",
        expected_plan_digest=plan.plan_digest,
        expected_source_digest=plan.source_digest,
    )

    assert decoded.plan == plan
    assert decoded.plan_artifact_digest != decoded.plan_digest
    assert decoded.source_artifact_role == "manual_import_source:json:atomic"


def test_decoder_rejects_noncanonical_field_aliases_even_when_model_accepts_them() -> None:
    plan = _plan()
    document = json.loads(canonical_manual_import_plan_json(plan))
    document["source_digest"] = document.pop("sourceDigest")
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()

    with pytest.raises(ManualImportPlanDocumentError) as error:
        decode_manual_import_plan(
            payload,
            plan_artifact_id=_PLAN_ARTIFACT_ID,
            plan_artifact_digest=_digest(payload),
            source_artifact_id=_SOURCE_ARTIFACT_ID,
            source_artifact_role="manual_import_source:json:atomic",
            expected_plan_digest=plan.plan_digest,
            expected_source_digest=plan.source_digest,
        )

    assert error.value.code == "MANUAL_IMPORT_PLAN_CONTRACT_INVALID"
    assert error.value.context["reason"] == "non_canonical_serialization"


def test_decoder_rejects_artifact_digest_mismatch_before_contract_use() -> None:
    plan = _plan()
    payload = canonical_manual_import_plan_json(plan).encode("utf-8")

    with pytest.raises(ManualImportPlanDocumentError) as error:
        decode_manual_import_plan(
            payload,
            plan_artifact_id=_PLAN_ARTIFACT_ID,
            plan_artifact_digest="sha256:" + "9" * 64,
            source_artifact_id=_SOURCE_ARTIFACT_ID,
            source_artifact_role="manual_import_source:json:atomic",
            expected_plan_digest=plan.plan_digest,
            expected_source_digest=plan.source_digest,
        )

    assert error.value.context["reason"] == "artifact_digest_mismatch"


def test_decoder_rejects_duplicate_json_keys() -> None:
    plan = _plan()
    payload = canonical_manual_import_plan_json(plan).encode("utf-8")
    duplicate = payload.replace(
        b'"contract":"collector-manual-import-plan",',
        b'"contract":"collector-manual-import-plan","contract":"collector-manual-import-plan",',
        1,
    )

    with pytest.raises(ManualImportPlanDocumentError) as error:
        decode_manual_import_plan(
            duplicate,
            plan_artifact_id=_PLAN_ARTIFACT_ID,
            plan_artifact_digest=_digest(duplicate),
            source_artifact_id=_SOURCE_ARTIFACT_ID,
            source_artifact_role="manual_import_source:json:atomic",
            expected_plan_digest=plan.plan_digest,
            expected_source_digest=plan.source_digest,
        )

    assert error.value.context["reason"] == "contract_validation_failed"


def test_invalid_digest_identity_is_typed_contract_failure() -> None:
    plan = _plan()
    payload = canonical_manual_import_plan_json(plan).encode("utf-8")

    with pytest.raises(ManualImportPlanDocumentError) as error:
        decode_manual_import_plan(
            payload,
            plan_artifact_id=_PLAN_ARTIFACT_ID,
            plan_artifact_digest="not-a-digest",
            source_artifact_id=_SOURCE_ARTIFACT_ID,
            source_artifact_role="manual_import_source:json:atomic",
            expected_plan_digest=plan.plan_digest,
            expected_source_digest=plan.source_digest,
        )

    assert error.value.code == "MANUAL_IMPORT_PLAN_CONTRACT_INVALID"
    assert error.value.context == {
        "reason": "digest_identity_invalid",
        "field": "plan_artifact_digest",
    }


def _plan():
    source = json.dumps(
        [
            {
                "expected_entity_kind": "place",
                "display_name": "Studio A",
                "website": "https://studio.example",
                "osm_id": None,
                "reference_urls": [],
                "note": None,
                "provenance": "manual import test",
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return build_manual_import_plan(
        source,
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )


def _digest(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"
