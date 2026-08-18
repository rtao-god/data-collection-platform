from __future__ import annotations

import json
from hashlib import sha256

import pytest

from collection_contracts import ManualImportFormat, ManualImportMode
from manual_import_core import (
    ManualImportPlanDocumentError,
    ManualImportRecordDocumentError,
    build_manual_import_plan,
    canonical_manual_import_plan_json,
    canonical_manual_import_record_json,
    decode_canonical_manual_import_plan,
    decode_canonical_manual_import_record,
    materialize_manual_import_record,
    parse_manual_import_plan_record_role,
    verify_manual_import_record_document,
)

_SOURCE = (
    b"expected_entity_kind,display_name,website,osm_id,"
    b"reference_urls,note,provenance\n"
    b"place,Studio A,https://studio.example,,,,manual-test\n"
)


def _plan_bytes(*, mode: ManualImportMode = ManualImportMode.ATOMIC) -> bytes:
    plan = build_manual_import_plan(
        _SOURCE,
        format=ManualImportFormat.CSV,
        mode=mode,
    )
    return canonical_manual_import_plan_json(plan).encode("utf-8")


def test_canonical_plan_decoder_separates_artifact_and_semantic_digests() -> None:
    payload = _plan_bytes()
    plan = decode_canonical_manual_import_plan(
        payload,
        expected_artifact_digest=_digest(payload),
    )

    assert plan.plan_digest != _digest(payload)
    assert plan.source_digest == _digest(_SOURCE)


def test_plan_decoder_rejects_noncanonical_serialization_and_duplicate_keys() -> None:
    payload = _plan_bytes()
    document = json.loads(payload)
    noncanonical = json.dumps(document).encode("utf-8")
    with pytest.raises(ManualImportPlanDocumentError) as noncanonical_error:
        decode_canonical_manual_import_plan(noncanonical)
    assert noncanonical_error.value.context["reason"] == "non_canonical_serialization"

    duplicate = payload.replace(
        b'"contract":"collector-manual-import-plan",',
        b'"contract":"collector-manual-import-plan","contract":"collector-manual-import-plan",',
        1,
    )
    with pytest.raises(ManualImportPlanDocumentError) as duplicate_error:
        decode_canonical_manual_import_plan(duplicate)
    assert duplicate_error.value.context["reason"] == "contract_validation_failed"


def test_record_materializer_binds_exact_plan_record_and_lineage() -> None:
    plan_payload = _plan_bytes()

    document = materialize_manual_import_record(
        plan_payload,
        source_artifact_role="manual_import_source:csv:atomic",
        plan_record_position=0,
    )

    verify_manual_import_record_document(document)
    assert document.plan_artifact_digest == _digest(plan_payload)
    assert document.source_digest == _digest(_SOURCE)
    assert document.plan_record_position == 0
    assert document.locator.pointer == "line:2"
    assert document.record.row_number == 2
    assert document.record.display_name == "Studio A"
    assert document.content_digest.startswith("sha256:")

    payload = canonical_manual_import_record_json(document).encode("utf-8")
    assert (
        decode_canonical_manual_import_record(
            payload,
            expected_content_digest=document.content_digest,
        )
        == document
    )


def test_record_document_rejects_content_and_record_identity_tampering() -> None:
    document = materialize_manual_import_record(
        _plan_bytes(),
        source_artifact_role="manual_source:csv:atomic",
        plan_record_position=0,
    )

    changed_content = document.model_copy(update={"plan_record_position": 1})
    with pytest.raises(ManualImportRecordDocumentError) as content_error:
        verify_manual_import_record_document(changed_content)
    assert content_error.value.context["reason"] == "content_digest_mismatch"

    changed_record = document.model_copy(update={"record_digest": "sha256:" + "9" * 64})
    changed_payload = changed_record.model_dump(mode="json", by_alias=True)
    changed_payload.pop("contentDigest")
    changed_record = changed_record.model_copy(
        update={
            "content_digest": "sha256:"
            + sha256(
                json.dumps(
                    changed_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        }
    )
    with pytest.raises(ManualImportRecordDocumentError) as record_error:
        verify_manual_import_record_document(changed_record)
    assert record_error.value.context["reason"] == "record_digest_mismatch"


def test_record_materializer_rejects_binding_mismatch_and_invalid_position() -> None:
    with pytest.raises(ManualImportRecordDocumentError) as role_error:
        materialize_manual_import_record(
            _plan_bytes(),
            source_artifact_role="manual_source:json:atomic",
            plan_record_position=0,
        )
    assert role_error.value.context["reason"] == "source_format_mismatch"

    with pytest.raises(ManualImportRecordDocumentError) as position_error:
        materialize_manual_import_record(
            _plan_bytes(),
            source_artifact_role="manual_source:csv:atomic",
            plan_record_position=1,
        )
    assert position_error.value.context["reason"] == "plan_record_position_invalid"


def test_plan_record_role_is_canonical_and_zero_based() -> None:
    assert parse_manual_import_plan_record_role("manual_import_plan_record:0") == 0
    assert parse_manual_import_plan_record_role("manual_import_plan_record:12") == 12

    for value in (
        "manual_import_plan_record:-1",
        "manual_import_plan_record:01",
        "manual_import_plan_record:",
    ):
        with pytest.raises(ManualImportRecordDocumentError):
            parse_manual_import_plan_record_role(value)


def _digest(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"
