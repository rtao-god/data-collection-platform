from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from collection_contracts import (
    EvidenceLocatorKind,
    EvidenceReference,
    ExtractedField,
    ExtractedRecordPayload,
    ExtractedTextValue,
    FieldObservationPayload,
    InvalidObservationValue,
    ObservationBatchPayload,
    ObservationState,
    TextObservationValue,
    canonical_extracted_record_json,
    canonical_observation_batch_json,
    decode_extracted_record,
    decode_observation_batch,
    seal_extracted_record,
    seal_field_observation,
    seal_observation_batch,
)

_DIGEST = f"sha256:{'1' * 64}"


def _evidence(*, span: str | None = "Example Studio") -> EvidenceReference:
    return EvidenceReference(
        raw_artifact_digest=_DIGEST,
        source_url="https://studio.example/",
        locator_kind=EvidenceLocatorKind.JSON_POINTER,
        locator_value="/json-ld/0/name",
        evidence_digest=f"sha256:{'2' * 64}",
        evidence_span=span,
        observed_at_utc=datetime(2026, 8, 14, tzinfo=UTC),
        extractor_revision="official-website-extractor@1",
    )


def test_extracted_record_and_observation_batch_round_trip_with_exact_digests() -> None:
    record = seal_extracted_record(
        ExtractedRecordPayload(
            source_record_id="source-record-example",
            raw_artifact_digest=_DIGEST,
            source_url="https://studio.example/",
            content_type="text/html",
            source_policy_digest=f"sha256:{'3' * 64}",
            extractor_revision="official-website-extractor@1",
            observed_at_utc=datetime(2026, 8, 14, tzinfo=UTC),
            fields=(
                ExtractedField(
                    field_key="display_name",
                    value=ExtractedTextValue(value="Example Studio"),
                    evidence=_evidence(),
                ),
            ),
        )
    )
    observation = seal_field_observation(
        FieldObservationPayload(
            source_record_id=record.source_record_id,
            field_key="display_name",
            state=ObservationState.OBSERVED,
            value=TextObservationValue(
                original="Example Studio",
                normalized="Example Studio",
            ),
            evidence=_evidence(),
            source_policy_digest=record.source_policy_digest,
            normalizer_revision="website-normalizer@1",
            confidence=Decimal("0.95"),
        )
    )
    batch = seal_observation_batch(
        ObservationBatchPayload(
            source_record_id=record.source_record_id,
            extracted_record_digest=record.content_digest,
            raw_artifact_digest=record.raw_artifact_digest,
            source_policy_digest=record.source_policy_digest,
            normalizer_revision="website-normalizer@1",
            observations=(observation,),
        )
    )

    assert decode_extracted_record(canonical_extracted_record_json(record).encode()) == record
    assert decode_observation_batch(canonical_observation_batch_json(batch).encode()) == batch


def test_observed_and_missing_states_cannot_be_interchanged() -> None:
    common = {
        "source_record_id": "source-record-example",
        "field_key": "display_name",
        "source_policy_digest": _DIGEST,
        "normalizer_revision": "website-normalizer@1",
        "confidence": Decimal("1"),
    }
    with pytest.raises(ValidationError):
        FieldObservationPayload(
            **common,
            state=ObservationState.OBSERVED,
            value=None,
            evidence=None,
        )
    with pytest.raises(ValidationError):
        FieldObservationPayload(
            **common,
            state=ObservationState.NOT_OBSERVED,
            value=TextObservationValue(original="fake", normalized="fake"),
            evidence=_evidence(),
        )


def test_invalid_state_requires_evidence_and_typed_invalid_detail() -> None:
    with pytest.raises(ValidationError):
        FieldObservationPayload(
            source_record_id="source-record-example",
            field_key="hourly_price",
            state=ObservationState.INVALID,
            value=InvalidObservationValue(
                reason_code="NORMALIZATION_MONEY_INCOMPLETE",
                original_excerpt="80 per hour",
            ),
            evidence=None,
            source_policy_digest=_DIGEST,
            normalizer_revision="website-normalizer@1",
            confidence=Decimal("1"),
        )
