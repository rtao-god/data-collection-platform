"""One real Wikidata classification claim, followed from the entity to the Catalog proposal.

The fixture is the unmodified pilot payload, so every identity here is the one Wikidata
published: entity revision, statement id, coordinates with their own precision. The chain
under test is

    entity document -> sealed source record -> typed observation -> candidate revision
    -> export field evidence (`categories/recording_studio`) -> backend item document
    (`categories/recording-studio`)

and it proves that the campaign category the backend is asked to propose comes from the
same instance-of statement the adapter accepted, not from campaign configuration or a
synthetic field path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from collection_contracts.export_package import ExportCatalogBinding
from collection_infrastructure.postgres.export_readers import ExportRow, _export_source
from collector_cli.backend_ingestion import IngestionAdapterError, _item_document
from processing_worker.source_record_mapping import materialize_source_record_observations
from resolution_contracts import CandidateField, EntityKind
from wikidata_studios import WikidataClassificationMap, WikidataEntityRequest, adapt_entity

_FIXTURE = Path(__file__).parent / "fixtures" / "wbgetentities.json"
_RUN_ID = UUID("019c0000-0000-7000-8000-000000000301")
_RETRIEVED_AT = datetime(2026, 11, 17, 14, 9, 35, tzinfo=UTC)
_STUDIO_CLASS = "Q746369"
_POLICY_DIGEST = "sha256:" + "b" * 64
_DECISION_ID = UUID("019c0000-0000-7000-8000-0000000003a1")
_DECISION_DIGEST = "sha256:" + "c" * 64
_CONFIGURATION_REVISION_ID = UUID("019fdeab-5c00-7000-8000-000000000002")
_PROFILE = {
    "campaignKey": "berlin_recording_services",
    "mappingRevision": "wikidata-berlin-studios-v1",
    "sourceBindingKey": "wikidata_studios_berlin",
    "sourceKey": "wikidata",
    "sourcePolicyDigest": _POLICY_DIGEST,
}
# The campaign projection, exactly as the binding declares it. No category entry exists
# here: a classification is its own claim of the record, not a candidate field.
_FIELD_PROJECTION = {
    "display_name": CandidateField.NAMES,
    "website": CandidateField.WEBSITE_URLS,
}


def _classification() -> WikidataClassificationMap:
    return WikidataClassificationMap(
        studio_class_id=_STUDIO_CLASS,
        campaign_category="recording_studio",
    )


def _adapted(entity_id: str):
    payload: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return adapt_entity(
        payload,
        entity_id=entity_id,
        classification=_classification(),
        request=WikidataEntityRequest(
            entity_ids=(entity_id,),
            retrieved_at_utc=_RETRIEVED_AT,
            run_id=_RUN_ID,
        ),
    )


def _mapped(record) -> Any:
    return materialize_source_record_observations(
        record.content,
        json.dumps(_PROFILE).encode("utf-8"),
        maximum_records=1,
    )


def test_the_real_studio_record_carries_its_category_claim_with_the_statement_it_came_from() -> (
    None
):
    record = _adapted("Q667023")

    entry = record.document["records"][0]
    claim = entry["category_recording_studio"][0]
    assert claim["value"] == "recording_studio"
    assert claim["pointer"] == (
        "/entities/Q667023/claims/P31/Q667023$"
        + record.classification_evidence["statementId"].split("$", 1)[1]
    )
    # The claim points at the statement, not at this campaign's mapping configuration.
    assert "config" not in claim["pointer"]
    assert record.classification_evidence["studioClassId"] == _STUDIO_CLASS

    batch = _mapped(record)
    observation = next(
        item for item in batch.observations if item.field_key == "category_recording_studio"
    )
    assert observation.value.normalized == "recording_studio"
    assert observation.evidence.locator_value == claim["pointer"]
    assert observation.evidence.raw_artifact_digest == batch.source_record_artifact_digest
    assert observation.evidence.mapping_revision == "wikidata-berlin-studios-v1"


def test_a_category_claim_whose_value_disagrees_with_its_key_is_invalid_evidence() -> None:
    record = _adapted("Q667023")
    document = json.loads(json.dumps(record.document))
    document["records"][0]["category_recording_studio"][0]["value"] = "band_recording"
    unsealed = {key: value for key, value in document.items() if key != "artifactDigest"}
    document["artifactDigest"] = (
        "sha256:"
        + sha256(
            json.dumps(
                unsealed, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
    )

    batch = materialize_source_record_observations(
        json.dumps(document).encode("utf-8"),
        json.dumps(_PROFILE).encode("utf-8"),
        maximum_records=1,
    )

    observation = next(
        item for item in batch.observations if item.field_key == "category_recording_studio"
    )
    assert observation.state == "invalid"
    assert observation.value.reason_code == "SOURCE_RECORD_CATEGORY_INVALID"
    assert observation.value.original_excerpt == "band_recording"


def _candidate_payload(record) -> tuple[dict[str, Any], dict[str, Any]]:
    """Materialize the real observations into the revision payload the reader consumes."""

    from candidate_materialization.materializer import _observation_uuid
    from candidate_materialization.materializer import materialize_candidate as materialize
    from resolution_contracts import GeographyReference

    batch = _mapped(record)
    # The fixture's own point, and nothing else: geography is not re-derived here because
    # the boundary classifier is exercised by its own owner's integration proofs.
    coordinates = next(item for item in batch.observations if item.field_key == "coordinates")
    geometry = coordinates.value
    geography = {
        "coverage": "inside",
        "marketAreaRevision": "berlin_boundary_v1",
        "marketAreaDigest": "sha256:" + "d" * 64,
        "latitude": str(geometry.latitude),
        "longitude": str(geometry.longitude),
        "observationIds": [str(_observation_uuid(coordinates.observation_id))],
    }

    revision, _ = materialize(
        _as_observation_batch(batch),
        candidate_id=UUID("019c0000-0000-7000-8000-0000000003b1"),
        entity_kind=EntityKind.PLACE,
        source_key="wikidata",
        source_artifact_ids=(batch.source_record_artifact_id,),
        field_projection=_FIELD_PROJECTION,
        geography=GeographyReference.model_validate(geography),
        recorded_at_utc=_RETRIEVED_AT,
        correlation_id="wikidata-category-provenance",
    )
    # The intake owner's own re-seal: export policy plus the source-native observation
    # ledger, which is the evidence the export proves every field against.
    from collection_infrastructure.postgres.candidate_intake_applier import (
        _ExportPolicy,
        _revision_with_export_facts,
    )

    revision = _revision_with_export_facts(
        revision,
        policy=_ExportPolicy(
            source_key="wikidata",
            legal_status="approved",
            usage_policy="publishable",
            attribution="Wikidata (CC0 1.0)",
        ),
        source_observations=batch.observations,
    )
    return dict(revision.normalized_payload), revision


def _as_observation_batch(batch):
    from collection_infrastructure.postgres.candidate_intake_applier import (
        _source_record_as_observations,
    )

    return _source_record_as_observations(batch)


def _export_row(payload: dict[str, Any]) -> ExportRow:
    return ExportRow(
        candidate_id=UUID("019c0000-0000-7000-8000-0000000003b1"),
        revision=0,
        cluster_id=UUID("019c0000-0000-7000-8000-0000000003b2"),
        snapshot_digest="sha256:" + "e" * 64,
        entity_kind="place",
        normalized_payload=payload,
        evidence_digests=("sha256:" + "f" * 64,),
        export_eligible=True,
        policy_digest="sha256:" + "1" * 64,
        evaluation_digest="sha256:" + "2" * 64,
        blockers=(),
        decision_id=_DECISION_ID,
        decision_outcome="accept_candidate",
        decision_digest=_DECISION_DIGEST,
        decided_at_utc=_RETRIEVED_AT,
        source_key="wikidata",
        legal_status="approved",
        usage_policy="publishable",
        attribution="Wikidata (CC0 1.0)",
    )


def _binding() -> ExportCatalogBinding:
    return ExportCatalogBinding.model_validate(
        {
            "siteKey": "berlin-recording",
            "catalogKey": "berlin-recording-services",
            "configurationRevisionId": _CONFIGURATION_REVISION_ID,
            "categories": [
                {"campaignCategory": "recording_studio", "catalogCategory": "recording-studio"}
            ],
            "nameLocales": [{"campaignLocale": "de", "catalogLocale": "de-DE"}],
            "coordinatePrecisionMeters": "25",
        }
    )


def test_the_export_proves_the_category_exactly_as_the_source_claimed_it() -> None:
    record = _adapted("Q667023")

    payload, revision = _candidate_payload(record)
    category_observation = next(
        item
        for item in revision.normalized_payload["fieldObservations"]
        if item["fieldKey"] == "category_recording_studio"
    )
    source = _export_source(_export_row(payload))

    assert source.category_keys == ("recording_studio",)
    evidence = {item.field_path: item for item in source.field_evidence}
    claim = evidence["categories/recording_studio"]
    assert claim.source_key == "wikidata"
    assert claim.source_record_id == "wikidata:Q667023"
    assert claim.source_url == ("https://www.wikidata.org/wiki/Special:EntityData/Q667023.json")
    assert claim.evidence_digest == category_observation["evidence"]["evidenceDigest"]
    assert claim.source_policy_digest == _POLICY_DIGEST

    item = _item_document(source, _binding())
    assert item["categoryProposals"] == ["recording-studio"]
    assert item["categoryDetails"] == [
        {"categoryKey": "recording-studio", "fieldPath": "categories/recording-studio"}
    ]
    category_reference = next(
        reference
        for reference in item["provenanceReferences"]
        if reference["fieldPath"] == "categories/recording-studio"
    )
    # One statement backs both the collector path and the backend path.
    assert category_reference["evidenceDigest"] == claim.evidence_digest.removeprefix("sha256:")
    assert category_reference["sourceExternalId"] == "wikidata:Q667023"
    assert category_reference["sourceUrl"] == (
        "https://www.wikidata.org/wiki/Special:EntityData/Q667023.json"
    )


def test_a_category_whose_key_and_value_disagree_blocks_the_export_at_its_owner() -> None:
    payload, revision = _candidate_payload(_adapted("Q667023"))
    observations = []
    for observation in revision.normalized_payload["fieldObservations"]:
        if observation["fieldKey"] == "category_recording_studio":
            observation = json.loads(json.dumps(observation))
            observation["value"]["normalized"] = "band_recording"
        observations.append(observation)
    payload = dict(payload)
    payload["fieldObservations"] = observations

    with pytest.raises(Exception) as error:
        _export_source(_export_row(payload))

    assert getattr(error.value, "code", None) == "EXPORT_CATEGORY_VALUE_CONFLICT"


def test_an_unclassified_campaign_category_cannot_be_proposed_to_the_catalog() -> None:
    payload, revision = _candidate_payload(_adapted("Q667023"))
    observations = []
    for observation in revision.normalized_payload["fieldObservations"]:
        if observation["fieldKey"] == "category_recording_studio":
            observation = json.loads(json.dumps(observation))
            observation["fieldKey"] = "category_rehearsal_recording_space"
            observation["value"]["normalized"] = "rehearsal_recording_space"
        observations.append(observation)
    payload = dict(payload)
    payload["fieldObservations"] = observations

    source = _export_source(_export_row(payload))
    assert source.category_keys == ("rehearsal_recording_space",)

    with pytest.raises(IngestionAdapterError) as error:
        _item_document(source, _binding())

    assert error.value.code == "INGESTION_CATEGORY_MAPPING_MISSING"


def test_the_ambiguous_meistersaal_item_never_reaches_a_category_claim() -> None:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))

    with pytest.raises(Exception) as error:
        adapt_entity(
            payload,
            entity_id="Q1372342",
            classification=_classification(),
            request=WikidataEntityRequest(
                entity_ids=("Q1372342",),
                retrieved_at_utc=_RETRIEVED_AT,
                run_id=_RUN_ID,
            ),
        )

    assert error.value.envelope.code == "WIKIDATA_CLASSIFICATION_AMBIGUOUS"
    assert Decimal("52.5062") == Decimal("52.5062")
