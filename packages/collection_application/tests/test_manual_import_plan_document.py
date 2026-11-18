from __future__ import annotations

import json
from hashlib import sha256
from uuid import UUID

import pytest

from collection_application.canonicalization import sha256_json
from collection_application.manual_import_plan_document import (
    ManualImportPlanDocumentError,
    decode_manual_import_plan,
    verify_manual_import_record_source_provenance,
)
from collection_contracts import (
    ManualImportFormat,
    ManualImportMode,
    ManualImportPlan,
)
from manual_import_core import (
    build_manual_import_plan,
    canonical_manual_import_plan_json,
    manual_import_record_digest,
    materialize_manual_import_record,
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


def test_record_provenance_reparses_exact_source_and_accepts_exact_plan() -> None:
    source_content = _source_bytes()
    plan = build_manual_import_plan(
        source_content,
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )
    plan_content = canonical_manual_import_plan_json(plan).encode("utf-8")
    record = materialize_manual_import_record(
        plan_content,
        source_artifact_role="manual_import_source:json:atomic",
        plan_record_position=0,
    )

    verify_manual_import_record_source_provenance(
        source_content=source_content,
        plan_content=plan_content,
        plan_artifact_digest=_digest(plan_content),
        source_artifact_role="manual_import_source:json:atomic",
        record=record,
    )


def test_record_provenance_rejects_substituted_source_record_and_plan() -> None:
    source_content = _source_bytes("Studio A")
    substituted_plan = build_manual_import_plan(
        _source_bytes("Studio B"),
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )
    plan_content = canonical_manual_import_plan_json(substituted_plan).encode("utf-8")
    substituted_record = materialize_manual_import_record(
        plan_content,
        source_artifact_role="manual_import_source:json:atomic",
        plan_record_position=0,
    )

    with pytest.raises(ValueError):
        verify_manual_import_record_source_provenance(
            source_content=source_content,
            plan_content=plan_content,
            plan_artifact_digest=_digest(plan_content),
            source_artifact_role="manual_import_source:json:atomic",
            record=substituted_record,
        )


def test_record_provenance_rejects_coherent_substitution_with_exact_source_digest() -> None:
    source_content = _source_bytes("Studio A")
    exact_plan = build_manual_import_plan(
        source_content,
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )
    substituted_plan = build_manual_import_plan(
        _source_bytes("Studio B"),
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )
    substituted_source_record = substituted_plan.records[0]
    coherent_record_digest = manual_import_record_digest(
        exact_plan.source_digest,
        substituted_source_record.locator,
        substituted_source_record.record,
    )
    substituted_source_record = substituted_source_record.model_copy(
        update={"record_digest": coherent_record_digest}
    )
    substituted_payload = substituted_plan.model_dump(mode="json", by_alias=True)
    substituted_payload["sourceDigest"] = exact_plan.source_digest
    substituted_payload["records"] = [
        substituted_source_record.model_dump(mode="json", by_alias=True)
    ]
    substituted_payload.pop("planDigest")
    substituted_plan = ManualImportPlan.model_validate(
        {
            **substituted_payload,
            "planDigest": sha256_json(substituted_payload),
        }
    )
    plan_content = canonical_manual_import_plan_json(substituted_plan).encode("utf-8")
    substituted_record = materialize_manual_import_record(
        plan_content,
        source_artifact_role="manual_import_source:json:atomic",
        plan_record_position=0,
    )

    with pytest.raises(ValueError, match="does not match parsing the exact source bytes"):
        verify_manual_import_record_source_provenance(
            source_content=source_content,
            plan_content=plan_content,
            plan_artifact_digest=_digest(plan_content),
            source_artifact_role="manual_import_source:json:atomic",
            record=substituted_record,
        )


def test_record_provenance_rejects_record_substituted_under_exact_plan() -> None:
    source_content = _source_bytes()
    plan = build_manual_import_plan(
        source_content,
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )
    plan_content = canonical_manual_import_plan_json(plan).encode("utf-8")
    record = materialize_manual_import_record(
        plan_content,
        source_artifact_role="manual_import_source:json:atomic",
        plan_record_position=0,
    )
    substituted_record = record.model_copy(
        update={"record": record.record.model_copy(update={"display_name": "Substituted"})}
    )

    with pytest.raises(ValueError):
        verify_manual_import_record_source_provenance(
            source_content=source_content,
            plan_content=plan_content,
            plan_artifact_digest=_digest(plan_content),
            source_artifact_role="manual_import_source:json:atomic",
            record=substituted_record,
        )


def _source_bytes(display_name: str = "Studio A") -> bytes:
    return json.dumps(
        [
            {
                "expected_entity_kind": "place",
                "display_name": display_name,
                "category": "recording_studio",
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


def _plan():
    return build_manual_import_plan(
        _source_bytes(),
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )


def _digest(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"
