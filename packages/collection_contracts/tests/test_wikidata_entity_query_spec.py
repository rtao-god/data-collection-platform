"""The declared entity query: bounded, sorted, canonical and re-readable.

Bootstrap publishes this document once per run and the acquisition worker replays it, so the
contract has to refuse anything that would make a run read an unintended set: duplicate or
unsorted identities, a set past the bounded ceiling, a category the campaign never declared.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from collection_contracts import (
    WikidataClassificationSpec,
    WikidataEntityQuerySpec,
    canonical_wikidata_query_spec_document,
    decode_wikidata_query_spec,
    wikidata_query_spec_digest,
)

_RUN_ID = UUID("00000000-0000-0000-0000-000000000411")
_POLICY_DIGEST = f"sha256:{'4' * 64}"


def _spec(**overrides: object) -> WikidataEntityQuerySpec:
    payload: dict[str, object] = {
        "run_id": _RUN_ID,
        "binding_key": "wikidata_studios_berlin",
        "entity_ids": ("Q105762301", "Q1372342", "Q56298345", "Q667023", "Q7695694"),
        "label_languages": ("de", "en"),
        "classification": WikidataClassificationSpec(
            studio_class_id="Q746369",
            campaign_category="recording_studio",
        ),
        "source_policy_digest": _POLICY_DIGEST,
    }
    payload.update(overrides)
    return WikidataEntityQuerySpec(**payload)  # type: ignore[arg-type]


def test_the_published_bytes_are_the_canonical_contract() -> None:
    spec = _spec()
    content = canonical_wikidata_query_spec_document(spec)

    document = json.loads(content)
    assert document["schemaRevision"] == "wikidata-entity-query-spec-v2"
    assert document["entityIds"] == list(spec.entity_ids)
    assert document["classification"] == {
        "campaignCategory": "recording_studio",
        "compatibleClassIds": [],
        "studioClassId": "Q746369",
    }
    assert decode_wikidata_query_spec(content) == spec
    assert wikidata_query_spec_digest(spec) == wikidata_query_spec_digest(_spec())


def test_a_duplicated_entity_id_is_refused() -> None:
    with pytest.raises(ValidationError, match="unique and sorted"):
        _spec(entity_ids=("Q667023", "Q667023"))


def test_an_unsorted_entity_set_is_refused() -> None:
    with pytest.raises(ValidationError, match="unique and sorted"):
        _spec(entity_ids=("Q7695694", "Q667023"))


def test_the_entity_set_is_bounded() -> None:
    too_many = tuple(f"Q{index}" for index in range(1, 52))

    with pytest.raises(ValidationError, match="between 1 and 50"):
        _spec(entity_ids=too_many)


def test_a_property_or_foreign_identity_is_not_an_item() -> None:
    with pytest.raises(ValidationError, match="non-item entity ids"):
        _spec(entity_ids=("P31",))


def test_an_unknown_language_tag_is_refused() -> None:
    with pytest.raises(ValidationError, match="invalid language tags"):
        _spec(label_languages=("de", "not a tag"))


def test_the_classification_must_name_an_item_and_a_declared_category() -> None:
    with pytest.raises(ValidationError, match="not an item id"):
        WikidataClassificationSpec(studio_class_id="studio", campaign_category="recording_studio")

    with pytest.raises(ValidationError, match="not a valid key"):
        WikidataClassificationSpec(
            studio_class_id="Q746369",
            campaign_category="Recording Studio",
        )


def test_the_policy_digest_must_be_a_sha256_digest() -> None:
    with pytest.raises(ValidationError, match="source policy digest is invalid"):
        _spec(source_policy_digest="sha256:short")


def test_bytes_that_are_not_canonical_are_not_the_published_specification() -> None:
    indented = json.dumps(json.loads(canonical_wikidata_query_spec_document(_spec())), indent=2)

    with pytest.raises(ValueError, match="not canonical"):
        decode_wikidata_query_spec(indented.encode("utf-8"))
