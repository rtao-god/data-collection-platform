from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from decimal import Decimal
from urllib.parse import SplitResult, urlsplit, urlunsplit

import phonenumbers
import tldextract

from collection_contracts import (
    BooleanObservationValue,
    BooleanPatternRule,
    EmailObservationValue,
    EvidenceReference,
    EvidenceTextBlock,
    ExtractedAddressValue,
    ExtractedBooleanValue,
    ExtractedEmailValue,
    ExtractedField,
    ExtractedMoneyValue,
    ExtractedPhoneValue,
    ExtractedRecord,
    ExtractedStringSetValue,
    ExtractedTextValue,
    ExtractedUrlValue,
    FieldObservation,
    FieldObservationPayload,
    InvalidObservationValue,
    MoneyObservationValue,
    NormalizationFieldRule,
    NormalizationProfile,
    ObservationBatch,
    ObservationBatchPayload,
    ObservationIssue,
    ObservationState,
    ObservationValue,
    PhoneObservationValue,
    StringSetObservationValue,
    StructuredAddressObservationValue,
    TextObservationValue,
    UrlObservationValue,
    seal_field_observation,
    seal_observation_batch,
    verify_extracted_record,
)

_TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())
_PHONE_TYPES: dict[int, str] = {
    phonenumbers.PhoneNumberType.FIXED_LINE: "fixed_line",
    phonenumbers.PhoneNumberType.MOBILE: "mobile",
    phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_line_or_mobile",
    phonenumbers.PhoneNumberType.TOLL_FREE: "toll_free",
    phonenumbers.PhoneNumberType.PREMIUM_RATE: "premium_rate",
    phonenumbers.PhoneNumberType.SHARED_COST: "shared_cost",
    phonenumbers.PhoneNumberType.VOIP: "voip",
    phonenumbers.PhoneNumberType.PERSONAL_NUMBER: "personal_number",
    phonenumbers.PhoneNumberType.PAGER: "pager",
    phonenumbers.PhoneNumberType.UAN: "uan",
    phonenumbers.PhoneNumberType.VOICEMAIL: "voicemail",
    phonenumbers.PhoneNumberType.UNKNOWN: "unknown",
}


class NormalizationError(ValueError):
    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def normalize_extracted_record(
    record: ExtractedRecord,
    profile: NormalizationProfile,
) -> ObservationBatch:
    verify_extracted_record(record)
    fields_by_key: dict[str, list[ExtractedField]] = defaultdict(list)
    for field in record.fields:
        fields_by_key[field.field_key].append(field)

    observations: list[FieldObservation] = []
    issues: list[ObservationIssue] = []
    target_fields: set[str] = set()
    for field_rule in profile.field_rules:
        target_fields.add(field_rule.target_field)
        source_fields = tuple(fields_by_key.get(field_rule.source_field, ()))
        if not source_fields:
            observations.append(_missing_observation(record, profile, field_rule.target_field))
            continue
        for source_field in source_fields:
            if field_rule.target_field in profile.prohibited_fields:
                observations.append(
                    _prohibited_observation(
                        record,
                        profile,
                        field_rule.target_field,
                        source_field.evidence,
                    )
                )
                continue
            observation, issue = _normalize_field(record, profile, field_rule, source_field)
            observations.append(observation)
            if issue is not None:
                issues.append(issue)

    for boolean_rule in profile.boolean_pattern_rules:
        target_fields.add(boolean_rule.target_field)
        if boolean_rule.target_field in profile.prohibited_fields:
            matching = _first_matching_block(
                record,
                (*boolean_rule.negative_patterns, *boolean_rule.positive_patterns),
            )
            observations.append(
                _prohibited_observation(
                    record,
                    profile,
                    boolean_rule.target_field,
                    matching.evidence if matching is not None else None,
                )
                if matching is not None
                else _missing_observation(record, profile, boolean_rule.target_field)
            )
            continue
        observations.append(
            _normalize_boolean_pattern(
                record,
                profile,
                boolean_rule.target_field,
                boolean_rule,
            )
        )

    for prohibited in record.prohibited_fields:
        if prohibited.field_key in target_fields:
            continue
        observations.append(
            _prohibited_observation(
                record,
                profile,
                prohibited.field_key,
                prohibited.evidence,
            )
        )

    payload = ObservationBatchPayload(
        source_record_id=record.source_record_id,
        extracted_record_digest=record.content_digest,
        raw_artifact_digest=record.raw_artifact_digest,
        source_policy_digest=record.source_policy_digest,
        normalizer_revision=profile.normalizer_revision,
        observations=tuple(observations),
        issues=tuple(issues),
    )
    return seal_observation_batch(payload)


def _normalize_field(
    record: ExtractedRecord,
    profile: NormalizationProfile,
    rule: NormalizationFieldRule,
    field: ExtractedField,
) -> tuple[FieldObservation, ObservationIssue | None]:
    try:
        value = _normalize_value(profile, rule, field)
    except NormalizationError as exc:
        observation = _invalid_observation(
            record,
            profile,
            rule.target_field,
            field.evidence,
            code=exc.code,
            excerpt=_raw_excerpt(field),
        )
        return observation, ObservationIssue(
            code=exc.code,
            message=str(exc),
            field_key=rule.target_field,
        )

    payload = FieldObservationPayload(
        source_record_id=record.source_record_id,
        field_key=rule.target_field,
        state=ObservationState.OBSERVED,
        value=value,
        evidence=field.evidence,
        source_policy_digest=record.source_policy_digest,
        normalizer_revision=profile.normalizer_revision,
        confidence=_evidence_confidence(field.evidence),
    )
    return seal_field_observation(payload), None


def _normalize_value(
    profile: NormalizationProfile,
    rule: NormalizationFieldRule,
    field: ExtractedField,
) -> ObservationValue:
    value = field.value
    if rule.value_kind == "text" and isinstance(value, ExtractedTextValue):
        return TextObservationValue(
            original=value.value,
            normalized=_normalize_text(value.value),
            locale=rule.locale or value.locale,
        )
    if rule.value_kind == "boolean" and isinstance(value, ExtractedBooleanValue):
        return BooleanObservationValue(value=value.value)
    if rule.value_kind == "url" and isinstance(value, ExtractedUrlValue):
        normalized_url, registrable = _normalize_url(value.value)
        return UrlObservationValue(
            original=value.value,
            normalized=normalized_url,
            registrable_domain=registrable,
        )
    if rule.value_kind == "email" and isinstance(value, ExtractedEmailValue):
        return EmailObservationValue(
            original=value.value,
            normalized=_normalize_email(value.value),
        )
    if rule.value_kind == "phone" and isinstance(value, ExtractedPhoneValue):
        return _normalize_phone(value.value, profile.default_phone_region)
    if rule.value_kind == "structured_address" and isinstance(value, ExtractedAddressValue):
        return _normalize_address(value)
    if rule.value_kind == "money" and isinstance(value, ExtractedMoneyValue):
        return _normalize_money(value)
    if rule.value_kind == "string_set" and isinstance(value, ExtractedStringSetValue):
        normalized_values = tuple(dict.fromkeys(_normalize_text(item) for item in value.values))
        return StringSetObservationValue(
            original_values=value.values,
            normalized_values=normalized_values,
        )
    raise NormalizationError(
        code="NORMALIZATION_VALUE_KIND_MISMATCH",
        message=(
            f"Extracted field {rule.source_field!r} does not satisfy "
            f"the requested {rule.value_kind!r} normalization contract."
        ),
    )


def _normalize_url(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("unsupported scheme")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("userinfo is forbidden")
        host = parsed.hostname
        if host is None:
            raise ValueError("host is missing")
        normalized_host = host.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise NormalizationError(
            code="NORMALIZATION_URL_INVALID",
            message="The extracted URL is not a valid canonical HTTP(S) URL.",
        ) from exc
    if port is not None and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{normalized_host}:{port}"
    else:
        netloc = normalized_host
    normalized = urlunsplit(
        SplitResult(
            parsed.scheme.lower(),
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )
    domain = _TLD_EXTRACTOR(normalized_host)
    registrable = domain.top_domain_under_public_suffix or normalized_host
    return normalized, registrable


def _normalize_email(value: str) -> str:
    if value.count("@") != 1:
        raise NormalizationError(
            code="NORMALIZATION_EMAIL_INVALID",
            message="The extracted email address has an invalid syntax.",
        )
    local, domain = value.rsplit("@", 1)
    local = local.strip()
    try:
        normalized_domain = domain.strip().encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise NormalizationError(
            code="NORMALIZATION_EMAIL_INVALID",
            message="The extracted email domain is invalid.",
        ) from exc
    if not local or not normalized_domain or "." not in normalized_domain:
        raise NormalizationError(
            code="NORMALIZATION_EMAIL_INVALID",
            message="The extracted email address has an invalid syntax.",
        )
    return f"{local}@{normalized_domain}"


def _normalize_phone(value: str, default_region: str | None) -> PhoneObservationValue:
    try:
        parsed = phonenumbers.parse(value, default_region)
    except phonenumbers.NumberParseException as exc:
        raise NormalizationError(
            code="NORMALIZATION_PHONE_INVALID",
            message="The extracted phone number cannot be parsed without guessing.",
        ) from exc
    if not phonenumbers.is_valid_number(parsed):
        raise NormalizationError(
            code="NORMALIZATION_PHONE_INVALID",
            message="The extracted phone number is not valid according to libphonenumber metadata.",
        )
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    region = phonenumbers.region_code_for_number(parsed)
    number_type = _PHONE_TYPES.get(phonenumbers.number_type(parsed), "unknown")
    return PhoneObservationValue(
        original=value,
        e164=e164,
        region=region,
        number_type=number_type,
    )


def _normalize_address(value: ExtractedAddressValue) -> StructuredAddressObservationValue:
    structured = any(
        (
            value.street_address,
            value.postal_code,
            value.locality,
            value.region,
            value.country_code,
        )
    )
    if not structured:
        raise NormalizationError(
            code="NORMALIZATION_ADDRESS_UNSTRUCTURED",
            message="A free-form address requires explicit review or an approved parser adapter.",
        )
    return StructuredAddressObservationValue(
        street_address=_optional_text(value.street_address),
        postal_code=_optional_text(value.postal_code),
        locality=_optional_text(value.locality),
        region=_optional_text(value.region),
        country_code=value.country_code,
        free_form=_optional_text(value.free_form),
    )


def _normalize_money(value: ExtractedMoneyValue) -> MoneyObservationValue:
    if value.amount is None or value.currency is None or value.basis is None:
        raise NormalizationError(
            code="NORMALIZATION_MONEY_INCOMPLETE",
            message="Money requires explicit amount, currency, and price basis evidence.",
        )
    return MoneyObservationValue(
        amount=value.amount,
        currency=value.currency,
        basis=_normalize_text(value.basis),
        observed_text=value.observed_text,
    )


def _normalize_boolean_pattern(
    record: ExtractedRecord,
    profile: NormalizationProfile,
    target_field: str,
    rule: BooleanPatternRule,
) -> FieldObservation:
    positive_patterns = rule.positive_patterns
    negative_patterns = rule.negative_patterns
    negative = _first_matching_block(record, negative_patterns)
    if negative is not None:
        return seal_field_observation(
            FieldObservationPayload(
                source_record_id=record.source_record_id,
                field_key=target_field,
                state=ObservationState.OBSERVED,
                value=BooleanObservationValue(value=False),
                evidence=negative.evidence,
                source_policy_digest=record.source_policy_digest,
                normalizer_revision=profile.normalizer_revision,
                confidence=Decimal("0.80"),
            )
        )
    positive = _first_matching_block(record, positive_patterns)
    if positive is not None:
        return seal_field_observation(
            FieldObservationPayload(
                source_record_id=record.source_record_id,
                field_key=target_field,
                state=ObservationState.OBSERVED,
                value=BooleanObservationValue(value=True),
                evidence=positive.evidence,
                source_policy_digest=record.source_policy_digest,
                normalizer_revision=profile.normalizer_revision,
                confidence=Decimal("0.75"),
            )
        )
    return _missing_observation(record, profile, target_field)


def _first_matching_block(
    record: ExtractedRecord,
    patterns: tuple[str, ...],
) -> EvidenceTextBlock | None:
    compiled = tuple(re.compile(pattern, flags=re.IGNORECASE | re.UNICODE) for pattern in patterns)
    for block in record.text_blocks:
        if any(pattern.search(block.text) is not None for pattern in compiled):
            return block
    return None


def _missing_observation(
    record: ExtractedRecord,
    profile: NormalizationProfile,
    field_key: str,
) -> FieldObservation:
    return seal_field_observation(
        FieldObservationPayload(
            source_record_id=record.source_record_id,
            field_key=field_key,
            state=ObservationState.NOT_OBSERVED,
            value=None,
            evidence=None,
            source_policy_digest=record.source_policy_digest,
            normalizer_revision=profile.normalizer_revision,
            confidence=Decimal("1"),
        )
    )


def _prohibited_observation(
    record: ExtractedRecord,
    profile: NormalizationProfile,
    field_key: str,
    evidence: EvidenceReference | None,
) -> FieldObservation:
    if evidence is None:
        return _missing_observation(record, profile, field_key)
    redacted = evidence.model_copy(update={"evidence_span": None})
    return seal_field_observation(
        FieldObservationPayload(
            source_record_id=record.source_record_id,
            field_key=field_key,
            state=ObservationState.PROHIBITED_BY_POLICY,
            value=None,
            evidence=redacted,
            source_policy_digest=record.source_policy_digest,
            normalizer_revision=profile.normalizer_revision,
            confidence=Decimal("1"),
        )
    )


def _invalid_observation(
    record: ExtractedRecord,
    profile: NormalizationProfile,
    field_key: str,
    evidence: EvidenceReference,
    *,
    code: str,
    excerpt: str,
) -> FieldObservation:
    return seal_field_observation(
        FieldObservationPayload(
            source_record_id=record.source_record_id,
            field_key=field_key,
            state=ObservationState.INVALID,
            value=InvalidObservationValue(
                reason_code=code,
                original_excerpt=excerpt,
            ),
            evidence=evidence,
            source_policy_digest=record.source_policy_digest,
            normalizer_revision=profile.normalizer_revision,
            confidence=Decimal("1"),
        )
    )


def _evidence_confidence(evidence: EvidenceReference) -> Decimal:
    if evidence.locator_kind.value == "json_pointer":
        return Decimal("0.95")
    return Decimal("0.85")


def _raw_excerpt(field: ExtractedField) -> str:
    span = field.evidence.evidence_span
    if span:
        return span[:120]
    return field.value.model_dump_json(by_alias=True)[:120]


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split())


def _optional_text(value: str | None) -> str | None:
    return _normalize_text(value) if value else None
