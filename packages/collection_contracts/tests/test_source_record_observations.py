from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from collection_contracts import (
    ObservationState,
    SourceRecordBatchPayload,
    SourceRecordEvidence,
    SourceRecordLocatorKind,
    SourceRecordObservationPayload,
    TextObservationValue,
    canonical_source_record_observation_batch_json,
    decode_source_record_observation_batch,
    seal_source_record_observation,
    seal_source_record_observation_batch,
    verify_source_record_observation_batch,
)

_RUN_ID = UUID("11111111-1111-5111-8111-111111111111")
_RECORD_ARTIFACT_ID = UUID("22222222-2222-5222-8222-222222222222")
_RECORD_DIGEST = f"sha256:{'a' * 64}"
_POLICY_DIGEST = f"sha256:{'b' * 64}"
_PROFILE_DIGEST = f"sha256:{'c' * 64}"


def _evidence(*, artifact_id: UUID = _RECORD_ARTIFACT_ID) -> SourceRecordEvidence:
    return SourceRecordEvidence(
        rawArtifactId=artifact_id,
        rawArtifactDigest=_RECORD_DIGEST,
        sourceUrl="https://www.openstreetmap.org/node/101",
        locatorKind=SourceRecordLocatorKind.JSON_POINTER,
        locatorValue="/properties/name",
        evidenceDigest=f"sha256:{'d' * 64}",
        evidenceSpan="Studio Kreuzberg",
        observedAtUtc=datetime(2026, 11, 15, tzinfo=UTC),
        mappingRevision="overture-places-mapping-v1",
    )


def _observation(
    *,
    field_key: str = "display_name",
    evidence: SourceRecordEvidence | None = None,
) -> object:
    return seal_source_record_observation(
        SourceRecordObservationPayload(
            sourceRecordId="overture:1234",
            fieldKey=field_key,
            state=ObservationState.OBSERVED,
            value=TextObservationValue(
                original="Studio Kreuzberg",
                normalized="Studio Kreuzberg",
            ),
            evidence=evidence if evidence is not None else _evidence(),
            sourcePolicyDigest=_POLICY_DIGEST,
            mappingRevision="overture-places-mapping-v1",
            confidence="0.9000",
        )
    )


def _batch_payload(**overrides: object) -> SourceRecordBatchPayload:
    base: dict[str, object] = {
        "runId": _RUN_ID,
        "sourceKey": "overture_places",
        "sourceBindingKey": "overture_places_berlin",
        "sourceRecordId": "overture:1234",
        "sourceRecordArtifactId": _RECORD_ARTIFACT_ID,
        "sourceRecordArtifactDigest": _RECORD_DIGEST,
        "datasetRelease": "2026-11-19.0",
        "sourcePolicyDigest": _POLICY_DIGEST,
        "mappingRevision": "overture-places-mapping-v1",
        "observationProfileDigest": _PROFILE_DIGEST,
        "observations": (_observation(),),
    }
    base.update(overrides)
    return SourceRecordBatchPayload(**base)


def test_source_record_batch_seals_and_round_trips_canonically() -> None:
    batch = seal_source_record_observation_batch(_batch_payload())

    verify_source_record_observation_batch(batch)
    canonical = canonical_source_record_observation_batch_json(batch)
    decoded = decode_source_record_observation_batch(canonical.encode("utf-8"))

    assert decoded == batch
    assert canonical.endswith("\n")
    assert json.loads(canonical)["contractRevision"] == "source-record-observation-batch@1"
    assert json.loads(canonical)["contract"] == "collector-source-record-observation-batch"


def test_source_record_batch_does_not_claim_extracted_record_lineage() -> None:
    batch = seal_source_record_observation_batch(_batch_payload())
    payload = batch.model_dump(mode="json", by_alias=True)

    assert "extractedRecordDigest" not in payload
    assert "extractedRecordArtifactId" not in payload


def test_observed_website_absence_is_not_observed_without_evidence() -> None:
    """A missing website is unknown, never a confirmed absence."""

    observation = seal_source_record_observation(
        SourceRecordObservationPayload(
            sourceRecordId="overture:1234",
            fieldKey="website",
            state=ObservationState.NOT_OBSERVED,
            sourcePolicyDigest=_POLICY_DIGEST,
            mappingRevision="overture-places-mapping-v1",
            confidence="0.0000",
        )
    )

    assert observation.value is None
    assert observation.evidence is None

    with pytest.raises(ValidationError):
        SourceRecordObservationPayload(
            sourceRecordId="overture:1234",
            fieldKey="website",
            state=ObservationState.ABSENT_IN_SOURCE,
            sourcePolicyDigest=_POLICY_DIGEST,
            mappingRevision="overture-places-mapping-v1",
            confidence="1.0000",
        )


def test_batch_rejects_observation_from_another_source_record() -> None:
    foreign = seal_source_record_observation(
        SourceRecordObservationPayload(
            sourceRecordId="overture:9999",
            fieldKey="display_name",
            state=ObservationState.OBSERVED,
            value=TextObservationValue(original="Other", normalized="Other"),
            evidence=_evidence(),
            sourcePolicyDigest=_POLICY_DIGEST,
            mappingRevision="overture-places-mapping-v1",
            confidence="0.5000",
        )
    )

    with pytest.raises(ValidationError):
        _batch_payload(observations=(foreign,))


def test_batch_rejects_evidence_pointing_at_another_artifact() -> None:
    stray = _observation(evidence=_evidence(artifact_id=uuid4()))

    with pytest.raises(ValidationError):
        _batch_payload(observations=(stray,))


def test_batch_rejects_tampered_content_digest() -> None:
    batch = seal_source_record_observation_batch(_batch_payload())
    tampered = batch.model_copy(update={"content_digest": f"sha256:{'0' * 64}"})

    with pytest.raises(ValueError):
        verify_source_record_observation_batch(tampered)
    with pytest.raises(ValueError):
        canonical_source_record_observation_batch_json(tampered)
