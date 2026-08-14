from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from collection_contracts import (
    BooleanPatternRule,
    EvidenceLocatorKind,
    EvidenceReference,
    EvidenceTextBlock,
    ExtractedAddressValue,
    ExtractedEmailValue,
    ExtractedField,
    ExtractedMoneyValue,
    ExtractedPhoneValue,
    ExtractedRecord,
    ExtractedRecordPayload,
    ExtractedTextValue,
    ExtractedUrlValue,
    NormalizationFieldRule,
    NormalizationProfile,
    ObservationState,
    seal_extracted_record,
)
from normalization_core import normalize_extracted_record

_DIGEST = f"sha256:{'1' * 64}"


def _evidence(locator: str, span: str | None = "evidence") -> EvidenceReference:
    return EvidenceReference(
        raw_artifact_digest=_DIGEST,
        source_url="https://studio.example/",
        locator_kind=EvidenceLocatorKind.JSON_POINTER,
        locator_value=locator,
        evidence_digest=f"sha256:{'2' * 64}",
        evidence_span=span,
        observed_at_utc=datetime(2026, 8, 14, tzinfo=UTC),
        extractor_revision="official-website-extractor@1",
    )


def _field(key: str, value: object, locator: str) -> ExtractedField:
    return ExtractedField(field_key=key, value=value, evidence=_evidence(locator))


def _record() -> ExtractedRecord:
    return seal_extracted_record(
        ExtractedRecordPayload(
            source_record_id="source-record-example",
            raw_artifact_digest=_DIGEST,
            source_url="https://studio.example/",
            content_type="text/html",
            source_policy_digest=f"sha256:{'3' * 64}",
            extractor_revision="official-website-extractor@1",
            observed_at_utc=datetime(2026, 8, 14, tzinfo=UTC),
            fields=(
                _field(
                    "display_name",
                    ExtractedTextValue(value="  Example   Studio  ", locale="de-DE"),
                    "/json-ld/0/name",
                ),
                _field(
                    "website",
                    ExtractedUrlValue(value="HTTPS://Studio.Example:443/contact?q=1#top"),
                    "/json-ld/0/url",
                ),
                _field(
                    "email",
                    ExtractedEmailValue(value="Info@Studio.Example"),
                    "/json-ld/0/email",
                ),
                _field(
                    "phone",
                    ExtractedPhoneValue(value="+49 30 123456"),
                    "/json-ld/0/telephone",
                ),
                _field(
                    "address",
                    ExtractedAddressValue(
                        street_address=" Example Street 1 ",
                        postal_code="10115",
                        locality="Berlin",
                        country_code="DE",
                    ),
                    "/json-ld/0/address",
                ),
                _field(
                    "hourly_price",
                    ExtractedMoneyValue(
                        amount=Decimal("80"),
                        currency="EUR",
                        basis=" hour ",
                        observed_text='{"price":"80","priceCurrency":"EUR"}',
                    ),
                    "/json-ld/0/offers",
                ),
                _field(
                    "incomplete_price",
                    ExtractedMoneyValue(
                        amount=Decimal("90"),
                        currency=None,
                        basis="hour",
                        observed_text="90 per hour",
                    ),
                    "/json-ld/0/offers/1",
                ),
                _field(
                    "review_text",
                    ExtractedTextValue(value="forbidden review"),
                    "/json-ld/0/review",
                ),
            ),
            text_blocks=(
                EvidenceTextBlock(
                    text="Recording and mixing. No grand piano.",
                    locale="en",
                    evidence=_evidence(
                        "/html/body/p[1]",
                        "Recording and mixing. No grand piano.",
                    ),
                ),
            ),
        )
    )


def _profile() -> NormalizationProfile:
    return NormalizationProfile(
        normalizer_revision="website-normalizer@1",
        default_phone_region="DE",
        field_rules=(
            NormalizationFieldRule(
                source_field="display_name",
                target_field="display_name",
                value_kind="text",
                locale="de-DE",
            ),
            NormalizationFieldRule(
                source_field="website",
                target_field="website",
                value_kind="url",
            ),
            NormalizationFieldRule(
                source_field="email",
                target_field="email",
                value_kind="email",
            ),
            NormalizationFieldRule(
                source_field="phone",
                target_field="phone",
                value_kind="phone",
            ),
            NormalizationFieldRule(
                source_field="address",
                target_field="address",
                value_kind="structured_address",
            ),
            NormalizationFieldRule(
                source_field="hourly_price",
                target_field="hourly_price",
                value_kind="money",
            ),
            NormalizationFieldRule(
                source_field="incomplete_price",
                target_field="incomplete_price",
                value_kind="money",
            ),
            NormalizationFieldRule(
                source_field="missing",
                target_field="parking",
                value_kind="boolean",
            ),
            NormalizationFieldRule(
                source_field="review_text",
                target_field="review_text",
                value_kind="text",
            ),
        ),
        boolean_pattern_rules=(
            BooleanPatternRule(
                target_field="grand_piano",
                positive_patterns=(r"\bgrand piano\b",),
                negative_patterns=(r"\bno grand piano\b",),
            ),
            BooleanPatternRule(
                target_field="mixing",
                positive_patterns=(r"\bmixing\b",),
            ),
        ),
        prohibited_fields=("review_text",),
    )


def test_normalizes_typed_values_and_preserves_explicit_states() -> None:
    batch = normalize_extracted_record(_record(), _profile())
    by_key = {observation.field_key: observation for observation in batch.observations}

    assert by_key["display_name"].value.normalized == "Example Studio"
    assert by_key["website"].value.normalized == "https://studio.example/contact?q=1"
    assert by_key["website"].value.registrable_domain == "studio.example"
    assert by_key["email"].value.normalized == "Info@studio.example"
    assert by_key["phone"].value.e164 == "+4930123456"
    assert by_key["address"].value.locality == "Berlin"
    assert by_key["hourly_price"].value.currency == "EUR"
    assert by_key["hourly_price"].value.basis == "hour"
    assert by_key["incomplete_price"].state is ObservationState.INVALID
    assert by_key["incomplete_price"].value.reason_code == "NORMALIZATION_MONEY_INCOMPLETE"
    assert by_key["parking"].state is ObservationState.NOT_OBSERVED
    assert by_key["parking"].value is None
    assert by_key["parking"].evidence is None
    assert by_key["review_text"].state is ObservationState.PROHIBITED_BY_POLICY
    assert by_key["review_text"].value is None
    assert by_key["review_text"].evidence.evidence_span is None
    assert by_key["grand_piano"].value.value is False
    assert by_key["mixing"].value.value is True
    assert batch.content_digest.startswith("sha256:")


def test_same_inputs_produce_same_observation_identities_and_batch_digest() -> None:
    first = normalize_extracted_record(_record(), _profile())
    second = normalize_extracted_record(_record(), _profile())

    assert first == second
    assert tuple(item.observation_id for item in first.observations) == tuple(
        item.observation_id for item in second.observations
    )
