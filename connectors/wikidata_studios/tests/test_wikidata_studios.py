"""Adapter behaviour against real Wikidata entity documents.

The fixture is the unmodified payload the pilot read on 2026-11-17 (QIDs, revision ids and
statement ids preserved). Tests use it so the adapter is measured against what Wikidata
actually publishes, not against a hand-written shape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from collection_contracts import OwnerContextError
from wikidata_studios import (
    MAX_ENTITIES_PER_REQUEST,
    WikidataClassificationMap,
    WikidataEntityRequest,
    adapt_entity,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "wbgetentities.json"
_RUN_ID = UUID("019c0000-0000-7000-8000-000000000201")
_RETRIEVED_AT = datetime(2026, 11, 17, 14, 9, 35, tzinfo=UTC)
# Q746369 is `recording studio`; Q41253/Q1060829/Q1763828 are the venue classes the real
# Meistersaal item carries next to it.
_STUDIO_CLASS = "Q746369"
_MAP = WikidataClassificationMap(
    studio_class_id=_STUDIO_CLASS,
    campaign_category="recording_studio",
)


def _payload() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _request(*entity_ids: str) -> WikidataEntityRequest:
    return WikidataEntityRequest(
        entity_ids=entity_ids or ("Q667023",),
        retrieved_at_utc=_RETRIEVED_AT,
        run_id=_RUN_ID,
    )


def _adapt(entity_id: str, *, classification: WikidataClassificationMap = _MAP):
    return adapt_entity(
        _payload(),
        entity_id=entity_id,
        classification=classification,
        request=_request(entity_id),
    )


def _code(exc: pytest.ExceptionInfo[OwnerContextError]) -> str:
    return exc.value.envelope.code


def test_a_studio_becomes_one_sealed_source_record_with_statement_evidence() -> None:
    record = _adapt("Q667023")
    document = record.document

    assert document["contract"] == "wikidata-source-record@1"
    assert document["sourceRecordId"] == "wikidata:Q667023"
    assert document["datasetRelease"] == "wikidata-entity-revision:2465661990"
    assert document["observedAtUtc"] == "2026-11-17T14:09:35Z"
    assert document["sourceUrl"] == "https://www.wikidata.org/wiki/Special:EntityData/Q667023.json"
    assert document["fields"] == [
        "category_recording_studio",
        "coordinates",
        "display_name",
        "website",
    ]

    entry = document["records"][0]
    assert entry["display_name"] == [
        {"value": "Hansa Studios", "pointer": "/entities/Q667023/labels/de", "locale": "de"}
    ]
    # The point keeps the statement it came from and the source element it belongs to.
    point = entry["coordinates"][0]
    assert point["valueKind"] == "geo_point"
    assert point["geometrySourceRecordId"] == "wikidata:Q667023"
    assert point["pointer"].startswith("/entities/Q667023/claims/P625/Q667023$")
    assert json.loads(point["value"]) == {"latitude": "52.506300", "longitude": "13.377200"}
    assert entry["website"][0]["value"] == "https://hansastudios.de/en/home/"


def test_the_record_digest_covers_the_document_and_changes_with_one_edited_value() -> None:
    record = _adapt("Q667023")
    document = json.loads(json.dumps(record.document))
    assert document["artifactDigest"] == record.digest

    document["records"][0]["website"][0]["value"] = "https://example.invalid/"
    assert _digest_without_field(document) != record.digest


def _digest_without_field(document: dict[str, Any]) -> str:
    from hashlib import sha256

    unsealed = {key: value for key, value in document.items() if key != "artifactDigest"}
    return (
        "sha256:"
        + sha256(
            json.dumps(
                unsealed, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
    )


def test_a_studio_without_a_published_point_keeps_the_field_absent() -> None:
    record = _adapt("Q7695694")

    assert "coordinates" not in record.document["fields"]
    assert "coordinates" not in record.document["records"][0]


def test_a_studio_without_a_published_address_keeps_the_field_absent() -> None:
    record = _adapt("Q667023")

    assert "address" not in record.document["fields"]


def test_a_published_street_address_is_kept_only_in_a_declared_language() -> None:
    record = _adapt("Q1372342", classification=_VENUE_MAP)

    address = record.document["records"][0]["address"][0]
    assert address["value"] == "Köthener Straße 38, 10963 Berlin"
    assert address["locale"] == "de"


_VENUE_MAP = WikidataClassificationMap(
    studio_class_id=_STUDIO_CLASS,
    campaign_category="recording_studio",
    compatible_class_ids=frozenset({"Q41253", "Q1060829", "Q1763828"}),
)


def test_a_venue_carrying_undeclared_classes_stays_blocked() -> None:
    with pytest.raises(OwnerContextError) as error:
        _adapt("Q1372342")

    assert _code(error) == "WIKIDATA_CLASSIFICATION_AMBIGUOUS"
    assert error.value.envelope.context["unexpectedClasses"] == "Q1060829,Q1763828,Q41253"


def test_an_entity_outside_the_mapped_class_stays_blocked() -> None:
    payload = _payload()
    payload["entities"]["Q667023"]["claims"]["P31"][0]["mainsnak"]["datavalue"]["value"]["id"] = (
        "Q5"
    )

    with pytest.raises(OwnerContextError) as error:
        adapt_entity(
            payload,
            entity_id="Q667023",
            classification=_MAP,
            request=_request("Q667023"),
        )

    assert _code(error) == "WIKIDATA_CLASSIFICATION_UNKNOWN"
    assert error.value.envelope.context["declaredClasses"] == "Q5"


def test_an_entity_without_any_classification_stays_blocked() -> None:
    payload = _payload()
    payload["entities"]["Q667023"]["claims"].pop("P31")

    with pytest.raises(OwnerContextError) as error:
        adapt_entity(payload, entity_id="Q667023", classification=_MAP, request=_request("Q667023"))

    assert _code(error) == "WIKIDATA_CLASSIFICATION_MISSING"


def test_two_different_published_points_are_a_source_conflict_not_an_average() -> None:
    payload = _payload()
    statements = payload["entities"]["Q667023"]["claims"]["P625"]
    duplicate = json.loads(json.dumps(statements[0]))
    duplicate["id"] = "Q667023$CONFLICTING"
    duplicate["mainsnak"]["datavalue"]["value"]["latitude"] = 52.51
    statements.append(duplicate)

    with pytest.raises(OwnerContextError) as error:
        adapt_entity(payload, entity_id="Q667023", classification=_MAP, request=_request("Q667023"))

    assert _code(error) == "WIKIDATA_COORDINATE_CONFLICT"
    assert error.value.envelope.context["distinctPoints"] == 2


def test_the_same_published_point_repeated_is_not_a_conflict() -> None:
    payload = _payload()
    statements = payload["entities"]["Q667023"]["claims"]["P625"]
    duplicate = json.loads(json.dumps(statements[0]))
    duplicate["id"] = "Q667023$REPEATED"
    statements.append(duplicate)

    record = adapt_entity(
        payload, entity_id="Q667023", classification=_MAP, request=_request("Q667023")
    )

    assert len(record.document["records"][0]["coordinates"]) == 1


def test_a_name_without_a_declared_language_tag_is_never_adapted() -> None:
    payload = _payload()
    payload["entities"]["Q667023"]["labels"] = {
        "ja": payload["entities"]["Q667023"]["labels"]["de"]
    }

    record = adapt_entity(
        payload, entity_id="Q667023", classification=_MAP, request=_request("Q667023")
    )

    assert "display_name" not in record.document["fields"]


def test_an_english_label_is_used_only_when_the_german_one_is_absent() -> None:
    payload = _payload()
    labels = payload["entities"]["Q667023"]["labels"]
    labels.pop("de")

    record = adapt_entity(
        payload, entity_id="Q667023", classification=_MAP, request=_request("Q667023")
    )

    assert record.document["records"][0]["display_name"][0]["locale"] == "en"


def test_a_missing_entity_is_refused_instead_of_becoming_an_empty_record() -> None:
    with pytest.raises(OwnerContextError) as error:
        _adapt("Q999999999")

    assert _code(error) == "WIKIDATA_ENTITY_PAYLOAD_INVALID"


def test_duplicate_entity_ids_in_one_request_are_refused() -> None:
    with pytest.raises(OwnerContextError) as error:
        WikidataEntityRequest(
            entity_ids=("Q667023", "Q667023"),
            retrieved_at_utc=_RETRIEVED_AT,
            run_id=_RUN_ID,
        )

    assert error.value.envelope.code == "WIKIDATA_REQUEST_DUPLICATE_ENTITY"


def test_the_request_is_bounded_and_pinned_to_the_wikidata_host() -> None:
    with pytest.raises(OwnerContextError) as error:
        WikidataEntityRequest(
            entity_ids=tuple(f"Q{index}" for index in range(1, MAX_ENTITIES_PER_REQUEST + 2)),
            retrieved_at_utc=_RETRIEVED_AT,
            run_id=_RUN_ID,
        )

    assert error.value.envelope.code == "WIKIDATA_REQUEST_LIMIT_EXCEEDED"
    query = _request("Q667023").entity_ids_query()
    assert query.startswith("https://www.wikidata.org/w/api.php?action=wbgetentities")
    assert "Q667023" in query


def test_a_request_without_a_utc_acquisition_time_is_refused() -> None:
    with pytest.raises(OwnerContextError) as error:
        WikidataEntityRequest(
            entity_ids=("Q667023",),
            retrieved_at_utc=datetime(2026, 11, 17, 14, 9, 35),
            run_id=_RUN_ID,
        )

    assert error.value.envelope.code == "WIKIDATA_REQUEST_TIMESTAMP_INVALID"


def test_the_adapted_record_is_exactly_what_the_mapping_owner_accepts() -> None:
    """The adapter's only contract is the existing mapping owner, so prove it there."""

    from processing_worker.source_record_mapping import materialize_source_record_observations

    record = _adapt("Q667023")
    profile = {
        "campaignKey": "berlin_recording_services",
        "mappingRevision": "wikidata-berlin-studios-v1",
        "sourceBindingKey": "wikidata_studios_berlin",
        "sourceKey": "wikidata",
        "sourcePolicyDigest": "sha256:" + "0" * 64,
    }

    batch = materialize_source_record_observations(
        record.content, json.dumps(profile).encode("utf-8"), maximum_records=1
    )

    by_key = {observation.field_key: observation for observation in batch.observations}
    assert by_key["display_name"].value.locale == "de"
    assert by_key["display_name"].value.normalized == "Hansa Studios"
    assert by_key["coordinates"].value.kind == "geo_point"
    assert by_key["coordinates"].value.latitude == Decimal("52.506300")
    assert by_key["website"].value.kind == "url"
    # The classification is a keyed claim of the record, evidenced by the exact
    # instance-of statement it was accepted from, not by this campaign's configuration.
    assert set(by_key) == {
        "category_recording_studio",
        "display_name",
        "coordinates",
        "website",
    }
    category = by_key["category_recording_studio"]
    assert category.value.kind == "text"
    assert category.value.normalized == "recording_studio"
    assert category.value.locale is None
    assert category.evidence.locator_value.startswith("/entities/Q667023/claims/P31/Q667023$")
    assert record.campaign_category == "recording_studio"
    assert record.classification_evidence["pointer"].startswith("/claims/P31/Q667023$")
    assert batch.source_key == "wikidata"
    assert batch.dataset_release == "wikidata-entity-revision:2465661990"
