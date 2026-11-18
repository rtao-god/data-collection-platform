"""The acquired-result envelope: what it promises and what it refuses.

Every consumer of a Wikidata acquisition — the collector's succeeded-work catalog and the
pipeline planner — decides whether a record may advance from this envelope alone. These tests
hold the envelope to that promise: one entry per requested entity, each entry bound to its own
bytes, its own classification statement, and the response it was read from.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest

from collection_contracts import (
    build_wikidata_entity_result,
    canonical_wikidata_source_record_document,
    decode_wikidata_entity_result,
    wikidata_record_digest,
)

_RUN_ID = UUID("77777777-7777-5777-8777-777777777777")
_RETRIEVED_AT = datetime(2026, 11, 30, 22, 3, 11, tzinfo=UTC)
_STATEMENT_ID = "Q667023$30d3e507-46e6-72b2-a4b6-5f3398a53f13"
_RESPONSE_DIGEST = f"sha256:{'1' * 64}"


def _document(entity_id: str = "Q667023", *, category: str = "recording_studio") -> dict:
    """One adapted record, sealed exactly as the connector seals it."""

    document: dict[str, Any] = {
        "contract": "wikidata-source-record@1",
        "sourceRecordId": f"wikidata:{entity_id}",
        "entityRevisionId": 2465661990,
        "observedAtUtc": _RETRIEVED_AT.isoformat().replace("+00:00", "Z"),
        "datasetRelease": "wikidata-entity-revision:2465661990",
        "runId": str(_RUN_ID),
        "sourceUrl": f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json",
        "attribution": "Wikidata (CC0 1.0)",
        "fields": ["display_name", f"category_{category}"],
        "records": [
            {
                "sourceRecordId": f"wikidata:{entity_id}",
                "display_name": [
                    {
                        "value": "Hansa Studios",
                        "pointer": f"/entities/{entity_id}/labels/de",
                    }
                ],
                f"category_{category}": [
                    {
                        "value": category,
                        "pointer": f"/entities/{entity_id}/claims/P31/{_STATEMENT_ID}",
                    }
                ],
            }
        ],
    }
    document["artifactRecordId"] = str(
        UUID(bytes=sha256(entity_id.encode()).digest()[:16], version=5)
    )
    unsealed = {key: value for key, value in document.items() if key != "artifactDigest"}
    document["artifactDigest"] = (
        "sha256:" + sha256(canonical_wikidata_source_record_document(unsealed)).hexdigest()
    )
    return document


def _envelope(*, records: list[dict] | None = None, rejected=None) -> bytes:
    return build_wikidata_entity_result(
        requested_entity_ids=("Q667023",),
        retrieved_at_utc=_RETRIEVED_AT,
        response_digest=_RESPONSE_DIGEST,
        records=records if records is not None else [_document()],
        rejected=rejected or [],
    )


def test_a_record_is_read_back_with_the_identity_its_producer_sealed() -> None:
    envelope = decode_wikidata_entity_result(_envelope())
    record = envelope.records[0]

    assert envelope.response_digest == _RESPONSE_DIGEST
    assert envelope.retrieved_at_utc == _RETRIEVED_AT
    assert envelope.requested_entity_ids == ("Q667023",)
    assert envelope.accepted_entity_ids == ("Q667023",)
    assert envelope.rejected_entities == ()
    assert record.source_record_id == "wikidata:Q667023"
    assert record.campaign_category == "recording_studio"
    assert record.classification_pointer.endswith(_STATEMENT_ID)
    assert record.dataset_release == "wikidata-entity-revision:2465661990"
    assert record.artifact_record_id == UUID(_document()["artifactRecordId"])
    # The content digest covers the exact bytes the artifact will publish.
    assert record.record_digest == wikidata_record_digest(record.document_bytes)
    assert record.document_bytes == canonical_wikidata_source_record_document(
        json.loads(record.document_bytes)
    )


def test_an_altered_record_is_refused_by_its_own_digest() -> None:
    envelope = json.loads(_envelope())
    envelope["records"][0]["document"]["records"][0]["display_name"][0]["value"] = "Substituted"

    with pytest.raises(ValueError, match="declared digest"):
        decode_wikidata_entity_result(_canonical(envelope))


def test_a_record_that_lost_its_own_seal_is_refused() -> None:
    document = _document()
    document["artifactDigest"] = f"sha256:{'0' * 64}"
    envelope = json.loads(_envelope(records=[document]))

    with pytest.raises(ValueError, match="self-verifying"):
        decode_wikidata_entity_result(_canonical(envelope))


def test_a_record_may_not_carry_two_categories() -> None:
    document = _document()
    document["records"][0]["category_band_recording"] = [
        {
            "value": "band_recording",
            "pointer": f"/entities/Q667023/claims/P31/{_STATEMENT_ID}",
        }
    ]
    document["fields"] = ["display_name", "category_recording_studio", "category_band_recording"]
    resealed = _reseal(document)

    with pytest.raises(ValueError, match="single category"):
        build_wikidata_entity_result(
            requested_entity_ids=("Q667023",),
            retrieved_at_utc=_RETRIEVED_AT,
            response_digest=_RESPONSE_DIGEST,
            records=[resealed],
            rejected=[],
        )


def test_a_category_claim_must_point_at_the_instance_of_statement() -> None:
    document = _document()
    document["records"][0]["category_recording_studio"][0]["pointer"] = (
        "/entities/Q667023/claims/P625/Q667023$30d3e507-46e6-72b2-a4b6-5f3398a53f13"
    )

    with pytest.raises(ValueError, match="instance-of statement"):
        decode_wikidata_entity_result(
            _canonical(json.loads(_envelope(records=[_reseal(document)])))
        )


def test_a_category_claim_citing_another_entity_statement_is_refused() -> None:
    document = _document()
    pointer = "/entities/Q667023/claims/P31/Q7695694$30d3e507-46e6-72b2-a4b6-5f3398a53f13"
    document["records"][0]["category_recording_studio"][0]["pointer"] = pointer

    # Refused where the envelope is built: a producer may not publish a record whose
    # classification cites a statement of a different item.
    with pytest.raises(ValueError, match="foreign instance-of statement"):
        build_wikidata_entity_result(
            requested_entity_ids=("Q667023",),
            retrieved_at_utc=_RETRIEVED_AT,
            response_digest=_RESPONSE_DIGEST,
            records=[_reseal(document)],
            rejected=[],
        )


def test_every_requested_entity_is_accounted_for() -> None:
    with pytest.raises(ValueError, match="exactly the requested entities"):
        build_wikidata_entity_result(
            requested_entity_ids=("Q667023", "Q7695694"),
            retrieved_at_utc=_RETRIEVED_AT,
            response_digest=_RESPONSE_DIGEST,
            records=[_document()],
            rejected=[],
        )


def test_an_entity_cannot_be_both_accepted_and_refused() -> None:
    with pytest.raises(ValueError, match="both accepts and refuses"):
        build_wikidata_entity_result(
            requested_entity_ids=("Q667023",),
            retrieved_at_utc=_RETRIEVED_AT,
            response_digest=_RESPONSE_DIGEST,
            records=[_document()],
            rejected=[("Q667023", "WIKIDATA_CLASSIFICATION_AMBIGUOUS", "Two classes.")],
        )


def test_a_refusal_is_published_with_its_reason() -> None:
    envelope = decode_wikidata_entity_result(
        build_wikidata_entity_result(
            requested_entity_ids=("Q1372342", "Q667023"),
            retrieved_at_utc=_RETRIEVED_AT,
            response_digest=_RESPONSE_DIGEST,
            records=[_document()],
            rejected=[
                (
                    "Q1372342",
                    "WIKIDATA_CLASSIFICATION_AMBIGUOUS",
                    "The entity carries more than one accepted class.",
                )
            ],
        )
    )

    assert envelope.accepted_entity_ids == ("Q667023",)
    assert envelope.rejected_entity_ids == ("Q1372342",)
    assert envelope.rejected_entities[0].code == "WIKIDATA_CLASSIFICATION_AMBIGUOUS"


def test_the_envelope_declares_its_own_contract_and_revision() -> None:
    envelope = json.loads(_envelope())

    assert envelope["contract"] == "wikidata-entity-result@2"
    assert envelope["schemaRevision"] == "wikidata-entity-result-v2"

    envelope["contract"] = "wikidata-entity-result@1"
    with pytest.raises(ValueError, match="unsupported contract"):
        decode_wikidata_entity_result(_canonical(envelope))


def test_bytes_that_are_not_canonical_are_not_the_published_result() -> None:
    indented = json.dumps(json.loads(_envelope()), indent=2).encode("utf-8")

    # The decoder reads the document, but the content digest names the published bytes, so a
    # consumer comparing digests can always tell re-serialized content from the original.
    envelope = decode_wikidata_entity_result(indented)
    assert envelope.content_digest == "sha256:" + sha256(indented).hexdigest()
    assert envelope.content_digest != "sha256:" + sha256(_envelope()).hexdigest()


def _reseal(document: dict) -> dict:
    resealed = {key: value for key, value in document.items() if key != "artifactDigest"}
    resealed["artifactDigest"] = (
        "sha256:" + sha256(canonical_wikidata_source_record_document(resealed)).hexdigest()
    )
    return resealed


def _canonical(document: object) -> bytes:
    return json.dumps(
        document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
