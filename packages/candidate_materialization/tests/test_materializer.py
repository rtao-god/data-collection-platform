from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from candidate_materialization import materialize_candidate
from collection_contracts import (
    EvidenceLocatorKind,
    EvidenceReference,
    FieldObservationPayload,
    ObservationBatchPayload,
    ObservationState,
    StructuredAddressObservationValue,
    TextObservationValue,
    seal_field_observation,
    seal_observation_batch,
)
from resolution_contracts import CandidateField, EntityKind, GeographyCoverage, GeographyReference

_DIGEST = f"sha256:{'1' * 64}"
_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2026, 8, 21, tzinfo=UTC)


def _batch():
    evidence = EvidenceReference(
        raw_artifact_digest=_DIGEST,
        source_url="https://studio.example/",
        locator_kind=EvidenceLocatorKind.JSON_POINTER,
        locator_value="/name",
        evidence_digest=f"sha256:{'2' * 64}",
        evidence_span="Example Studio",
        observed_at_utc=_NOW,
        extractor_revision="extractor@1",
    )
    name = seal_field_observation(
        FieldObservationPayload(
            source_record_id="record-1",
            field_key="name",
            state=ObservationState.OBSERVED,
            value=TextObservationValue(original="Example Studio", normalized="example studio"),
            evidence=evidence,
            source_policy_digest=f"sha256:{'3' * 64}",
            normalizer_revision="normalizer@1",
            confidence="1",
        )
    )
    address_evidence = evidence.model_copy(
        update={
            "locator_value": "/address",
            "evidence_digest": f"sha256:{'4' * 64}",
            "evidence_span": "Fürstenstraße 1, 10115 Berlin, DE",
        }
    )
    address = seal_field_observation(
        FieldObservationPayload(
            source_record_id="record-1",
            field_key="address",
            state=ObservationState.OBSERVED,
            value=StructuredAddressObservationValue(
                street_address="Fürstenstraße 1",
                postal_code="10115",
                locality="Berlin",
                country_code="DE",
            ),
            evidence=address_evidence,
            source_policy_digest=f"sha256:{'3' * 64}",
            normalizer_revision="normalizer@1",
            confidence="1",
        )
    )
    return seal_observation_batch(
        ObservationBatchPayload(
            source_record_id="record-1",
            extracted_record_digest=f"sha256:{'5' * 64}",
            raw_artifact_digest=_DIGEST,
            source_policy_digest=f"sha256:{'3' * 64}",
            normalizer_revision="normalizer@1",
            observations=(name, address),
        )
    )


def _materialize(batch, *, geography: GeographyReference | None = None):
    if geography is None:
        geography = GeographyReference(
            coverage=GeographyCoverage.UNKNOWN,
            market_area_revision="berlin-boundary@1",
            market_area_digest=f"sha256:{'6' * 64}",
        )
    return materialize_candidate(
        batch,
        candidate_id=UUID("00000000-0000-0000-0000-000000000001"),
        entity_kind=EntityKind.PLACE,
        source_key="official_site",
        source_artifact_ids=(_ARTIFACT_ID,),
        observation_artifact_ids={_DIGEST: _ARTIFACT_ID},
        field_projection={
            "name": CandidateField.NAMES,
            "address": CandidateField.ADDRESSES,
        },
        geography=geography,
        recorded_at_utc=_NOW,
        correlation_id="test-correlation",
    )


def test_materialization_preserves_evidence_and_unknown_geography() -> None:
    batch = _batch()

    revision, candidate = _materialize(batch)

    assert revision.resolution_state == "review"
    assert revision.source_lineage_digest
    assert {item.evidence_digest for item in revision.evidence} == {
        item.observation_id for item in batch.observations
    }
    assert candidate.names == ("example studio",)
    assert candidate.addresses == ("fürstenstrasse 1, 10115, berlin, de",)
    assert candidate.geography.coverage is GeographyCoverage.UNKNOWN
    assert candidate.geography.latitude is None
    assert candidate.geography.longitude is None
    assert candidate.geography.observation_ids == ()


def test_materialization_rejects_observation_batch_with_forged_observation_identity() -> None:
    batch = _batch()
    changed = batch.observations[0].model_copy(
        update={"value": TextObservationValue(original="Changed", normalized="changed")}
    )
    tampered = batch.model_copy(update={"observations": (changed, *batch.observations[1:])})

    with pytest.raises(
        ValueError,
        match="field observation identity does not match canonical content",
    ):
        _materialize(tampered)


def test_materialization_rejects_batch_with_stale_content_digest() -> None:
    batch = _batch()
    tampered = batch.model_copy(update={"content_digest": f"sha256:{'9' * 64}"})

    with pytest.raises(
        ValueError, match="observation batch digest does not match canonical content"
    ):
        _materialize(tampered)


def test_materialization_accepts_classification_bound_to_observation_provenance() -> None:
    classified_geography = GeographyReference(
        coverage=GeographyCoverage.INSIDE,
        market_area_revision="berlin-boundary@1",
        market_area_digest=f"sha256:{'6' * 64}",
        latitude=Decimal("52.52"),
        longitude=Decimal("13.405"),
        observation_ids=(UUID("00000000-0000-0000-0000-000000000099"),),
    )

    revision, candidate = _materialize(_batch(), geography=classified_geography)

    # Coverage is decided by the caller's exact boundary evaluation and is persisted with
    # the revision, so an export reads the decision instead of re-deriving it.
    assert candidate.geography.coverage is GeographyCoverage.INSIDE
    assert revision.normalized_payload["geography"] == {
        "coverage": "inside",
        "marketAreaRevision": "berlin-boundary@1",
        "marketAreaDigest": f"sha256:{'6' * 64}",
        "latitude": "52.52",
        "longitude": "13.405",
        "observationIds": ["00000000-0000-0000-0000-000000000099"],
    }


def test_materialization_rejects_classification_without_observation_provenance() -> None:
    unbound_geography = GeographyReference.model_construct(
        coverage=GeographyCoverage.INSIDE,
        market_area_revision="berlin-boundary@1",
        market_area_digest=f"sha256:{'6' * 64}",
        latitude=Decimal("52.52"),
        longitude=Decimal("13.405"),
        observation_ids=(),
    )

    with pytest.raises(ValueError, match="requires observation provenance"):
        _materialize(_batch(), geography=unbound_geography)


def test_materialization_revalidates_unknown_geography_provenance_shape() -> None:
    verified_shape = GeographyReference(
        coverage=GeographyCoverage.UNKNOWN,
        market_area_revision="berlin-boundary@1",
        market_area_digest=f"sha256:{'6' * 64}",
    )
    forged = verified_shape.model_copy(
        update={
            "latitude": Decimal("52.52"),
            "longitude": Decimal("13.405"),
            "observation_ids": (UUID("00000000-0000-0000-0000-000000000099"),),
        }
    )

    with pytest.raises(ValidationError, match="unknown geography must not invent coordinates"):
        _materialize(_batch(), geography=forged)


def test_every_sealed_observation_carries_the_source_artifact_identity() -> None:
    revision, _ = _materialize(_batch())

    identities = {
        observation["evidence"]["rawArtifactId"]
        for observation in revision.normalized_payload["fieldObservations"]
    }
    assert identities == {str(_ARTIFACT_ID)}
    digests = {
        observation["evidence"]["rawArtifactDigest"]
        for observation in revision.normalized_payload["fieldObservations"]
    }
    assert digests == {_DIGEST}


def test_observation_evidence_that_binds_no_artifact_is_refused() -> None:
    batch = _batch()
    foreign = EvidenceReference(
        raw_artifact_digest=f"sha256:{'9' * 64}",
        source_url="https://studio.example/",
        locator_kind=EvidenceLocatorKind.JSON_POINTER,
        locator_value="/name",
        evidence_digest=f"sha256:{'2' * 64}",
        observed_at_utc=_NOW,
        extractor_revision="extractor@1",
    )
    forged = batch.observations[0].model_copy(update={"evidence": foreign})
    changed = seal_observation_batch(
        ObservationBatchPayload(
            **{
                **batch.model_dump(mode="python", exclude={"content_digest"}),
                "observations": (
                    seal_field_observation(
                        FieldObservationPayload(
                            **{
                                **forged.model_dump(mode="python", exclude={"observation_id"}),
                                "evidence": foreign,
                            }
                        )
                    ),
                    *batch.observations[1:],
                ),
            }
        )
    )

    with pytest.raises(ValueError, match="does not bind exactly one source artifact"):
        _materialize(changed)
