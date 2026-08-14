from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._:@+-]{0,127}$")
_FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_COUNTRY_PATTERN = re.compile(r"^[A-Z]{2}$")
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_MAX_EVIDENCE_CHARS = 300
_MAX_RECORD_FIELDS = 1_024
_MAX_TEXT_BLOCKS = 512
_MAX_OBSERVATIONS = 2_048


class _ContractModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


class EvidenceLocatorKind(StrEnum):
    JSON_POINTER = "json_pointer"
    XPATH = "xpath"
    CSS = "css"
    HTML_ATTRIBUTE = "html_attribute"
    TEXT_OFFSET = "text_offset"


class ObservationState(StrEnum):
    OBSERVED = "observed"
    NOT_OBSERVED = "not_observed"
    ABSENT_IN_SOURCE = "absent_in_source"
    UNSUPPORTED = "unsupported"
    PROHIBITED_BY_POLICY = "prohibited_by_policy"
    INVALID = "invalid"
    EXPIRED = "expired"
    DISPUTED = "disputed"


class EvidenceReference(_ContractModel):
    raw_artifact_digest: str = Field(alias="rawArtifactDigest")
    source_url: str = Field(alias="sourceUrl", min_length=1, max_length=8_192)
    locator_kind: EvidenceLocatorKind = Field(alias="locatorKind")
    locator_value: str = Field(alias="locatorValue", min_length=1, max_length=1_024)
    evidence_digest: str = Field(alias="evidenceDigest")
    evidence_span: str | None = Field(
        default=None,
        alias="evidenceSpan",
        max_length=_MAX_EVIDENCE_CHARS,
    )
    observed_at_utc: datetime = Field(alias="observedAtUtc")
    extractor_revision: str = Field(alias="extractorRevision")

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        _require_digest("raw_artifact_digest", self.raw_artifact_digest)
        _require_digest("evidence_digest", self.evidence_digest)
        _require_token("extractor_revision", self.extractor_revision)
        _require_utc("observed_at_utc", self.observed_at_utc)
        return self


class ExtractedTextValue(_ContractModel):
    kind: Literal["text"] = "text"
    value: str = Field(min_length=1, max_length=2_000)
    locale: str | None = Field(default=None, pattern=_LANGUAGE_PATTERN.pattern)


class ExtractedBooleanValue(_ContractModel):
    kind: Literal["boolean"] = "boolean"
    value: bool


class ExtractedUrlValue(_ContractModel):
    kind: Literal["url"] = "url"
    value: str = Field(min_length=1, max_length=8_192)


class ExtractedEmailValue(_ContractModel):
    kind: Literal["email"] = "email"
    value: str = Field(min_length=3, max_length=320)


class ExtractedPhoneValue(_ContractModel):
    kind: Literal["phone"] = "phone"
    value: str = Field(min_length=3, max_length=100)


class ExtractedStringSetValue(_ContractModel):
    kind: Literal["string_set"] = "string_set"
    values: tuple[str, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        normalized = tuple(value.strip() for value in self.values)
        if any(not value or len(value) > 500 for value in normalized):
            raise ValueError("string-set values must be non-empty and bounded")
        if len(set(normalized)) != len(normalized):
            raise ValueError("string-set values must be unique")
        return self


class ExtractedAddressValue(_ContractModel):
    kind: Literal["structured_address"] = "structured_address"
    street_address: str | None = Field(default=None, alias="streetAddress", max_length=300)
    postal_code: str | None = Field(default=None, alias="postalCode", max_length=32)
    locality: str | None = Field(default=None, max_length=200)
    region: str | None = Field(default=None, max_length=200)
    country_code: str | None = Field(
        default=None,
        alias="countryCode",
        pattern=_COUNTRY_PATTERN.pattern,
    )
    free_form: str | None = Field(default=None, alias="freeForm", max_length=500)

    @model_validator(mode="after")
    def validate_address(self) -> Self:
        if not any(
            (
                self.street_address,
                self.postal_code,
                self.locality,
                self.region,
                self.country_code,
                self.free_form,
            )
        ):
            raise ValueError("structured address requires at least one observed component")
        return self


class ExtractedMoneyValue(_ContractModel):
    kind: Literal["money"] = "money"
    amount: Decimal | None = Field(default=None, gt=0, max_digits=19, decimal_places=4)
    currency: str | None = Field(default=None, pattern=_CURRENCY_PATTERN.pattern)
    basis: str | None = Field(default=None, max_length=100)
    observed_text: str = Field(alias="observedText", min_length=1, max_length=300)


type ExtractedValue = Annotated[
    ExtractedTextValue
    | ExtractedBooleanValue
    | ExtractedUrlValue
    | ExtractedEmailValue
    | ExtractedPhoneValue
    | ExtractedStringSetValue
    | ExtractedAddressValue
    | ExtractedMoneyValue,
    Field(discriminator="kind"),
]


class ExtractedField(_ContractModel):
    field_key: str = Field(alias="fieldKey")
    value: ExtractedValue
    evidence: EvidenceReference

    @model_validator(mode="after")
    def validate_field(self) -> Self:
        _require_field_key(self.field_key)
        if self.evidence.evidence_span is None:
            raise ValueError("an extracted field requires a bounded evidence span")
        return self


class ProhibitedFieldEvidence(_ContractModel):
    field_key: str = Field(alias="fieldKey")
    evidence: EvidenceReference

    @model_validator(mode="after")
    def validate_field(self) -> Self:
        _require_field_key(self.field_key)
        if self.evidence.evidence_span is not None:
            raise ValueError("prohibited-field evidence must not retain source content")
        return self


class EvidenceTextBlock(_ContractModel):
    text: str = Field(min_length=1, max_length=_MAX_EVIDENCE_CHARS)
    locale: str | None = Field(default=None, pattern=_LANGUAGE_PATTERN.pattern)
    evidence: EvidenceReference

    @model_validator(mode="after")
    def validate_block(self) -> Self:
        if self.evidence.evidence_span != self.text:
            raise ValueError("text block and evidence span must identify the same bounded text")
        return self


class ExtractionIssue(_ContractModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    message: str = Field(min_length=1, max_length=500)
    field_key: str | None = Field(default=None, alias="fieldKey")
    evidence: EvidenceReference | None = None

    @model_validator(mode="after")
    def validate_field_key(self) -> Self:
        if self.field_key is not None:
            _require_field_key(self.field_key)
        return self


class ExtractionRequest(_ContractModel):
    contract: Literal["collector-extraction-request"] = "collector-extraction-request"
    contract_revision: Literal["extraction-request@1"] = Field(
        default="extraction-request@1",
        alias="contractRevision",
    )
    source_record_id: str = Field(alias="sourceRecordId")
    raw_artifact_digest: str = Field(alias="rawArtifactDigest")
    source_url: str = Field(alias="sourceUrl", min_length=1, max_length=8_192)
    content_type: Literal["text/html", "application/xhtml+xml"] = Field(alias="contentType")
    source_policy_digest: str = Field(alias="sourcePolicyDigest")
    extractor_revision: str = Field(alias="extractorRevision")
    observed_at_utc: datetime = Field(alias="observedAtUtc")
    locale: str | None = Field(default=None, pattern=_LANGUAGE_PATTERN.pattern)
    allowed_fields: tuple[str, ...] = Field(alias="allowedFields", min_length=1, max_length=256)
    prohibited_fields: tuple[str, ...] = Field(
        default=(),
        alias="prohibitedFields",
        max_length=256,
    )
    maximum_document_bytes: int = Field(
        default=8 * 1024 * 1024,
        alias="maximumDocumentBytes",
        ge=1_024,
        le=64 * 1024 * 1024,
    )
    maximum_evidence_chars: int = Field(
        default=_MAX_EVIDENCE_CHARS,
        alias="maximumEvidenceChars",
        ge=40,
        le=_MAX_EVIDENCE_CHARS,
    )

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _require_token("source_record_id", self.source_record_id)
        _require_digest("raw_artifact_digest", self.raw_artifact_digest)
        _require_digest("source_policy_digest", self.source_policy_digest)
        _require_token("extractor_revision", self.extractor_revision)
        _require_utc("observed_at_utc", self.observed_at_utc)
        _require_unique_fields("allowed_fields", self.allowed_fields)
        _require_unique_fields("prohibited_fields", self.prohibited_fields)
        overlap = set(self.allowed_fields).intersection(self.prohibited_fields)
        if overlap:
            raise ValueError(f"allowed and prohibited fields overlap: {sorted(overlap)}")
        return self


class ExtractedRecordPayload(_ContractModel):
    contract: Literal["collector-extracted-record"] = "collector-extracted-record"
    contract_revision: Literal["extracted-record@1"] = Field(
        default="extracted-record@1",
        alias="contractRevision",
    )
    source_record_id: str = Field(alias="sourceRecordId")
    raw_artifact_digest: str = Field(alias="rawArtifactDigest")
    source_url: str = Field(alias="sourceUrl", min_length=1, max_length=8_192)
    content_type: Literal["text/html", "application/xhtml+xml"] = Field(alias="contentType")
    source_policy_digest: str = Field(alias="sourcePolicyDigest")
    extractor_revision: str = Field(alias="extractorRevision")
    observed_at_utc: datetime = Field(alias="observedAtUtc")
    entity_kind_candidates: tuple[Literal["organization", "place", "provider"], ...] = Field(
        default=(),
        alias="entityKindCandidates",
    )
    source_categories: tuple[str, ...] = Field(default=(), alias="sourceCategories")
    fields: tuple[ExtractedField, ...] = Field(default=(), max_length=_MAX_RECORD_FIELDS)
    prohibited_fields: tuple[ProhibitedFieldEvidence, ...] = Field(
        default=(),
        alias="prohibitedFields",
        max_length=256,
    )
    text_blocks: tuple[EvidenceTextBlock, ...] = Field(
        default=(),
        alias="textBlocks",
        max_length=_MAX_TEXT_BLOCKS,
    )
    issues: tuple[ExtractionIssue, ...] = Field(default=(), max_length=512)

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        _require_token("source_record_id", self.source_record_id)
        _require_digest("raw_artifact_digest", self.raw_artifact_digest)
        _require_digest("source_policy_digest", self.source_policy_digest)
        _require_token("extractor_revision", self.extractor_revision)
        _require_utc("observed_at_utc", self.observed_at_utc)
        if len(set(self.entity_kind_candidates)) != len(self.entity_kind_candidates):
            raise ValueError("entity-kind candidates must be unique")
        if len(set(self.source_categories)) != len(self.source_categories):
            raise ValueError("source categories must be unique")
        field_identities = tuple(
            (
                field.field_key,
                field.evidence.locator_kind,
                field.evidence.locator_value,
                field.evidence.evidence_digest,
            )
            for field in self.fields
        )
        if len(set(field_identities)) != len(field_identities):
            raise ValueError("extracted field evidence identities must be unique")
        return self


class ExtractedRecord(ExtractedRecordPayload):
    content_digest: str = Field(alias="contentDigest")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        _require_digest("content_digest", self.content_digest)
        return self


class NormalizationFieldRule(_ContractModel):
    source_field: str = Field(alias="sourceField")
    target_field: str = Field(alias="targetField")
    value_kind: Literal[
        "text",
        "boolean",
        "url",
        "email",
        "phone",
        "structured_address",
        "money",
        "string_set",
    ] = Field(alias="valueKind")
    locale: str | None = Field(default=None, pattern=_LANGUAGE_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        _require_field_key(self.source_field)
        _require_field_key(self.target_field)
        return self


class BooleanPatternRule(_ContractModel):
    target_field: str = Field(alias="targetField")
    locale: str | None = Field(default=None, pattern=_LANGUAGE_PATTERN.pattern)
    positive_patterns: tuple[str, ...] = Field(
        alias="positivePatterns", min_length=1, max_length=32
    )
    negative_patterns: tuple[str, ...] = Field(default=(), alias="negativePatterns", max_length=32)

    @model_validator(mode="after")
    def validate_patterns(self) -> Self:
        _require_field_key(self.target_field)
        for pattern in (*self.positive_patterns, *self.negative_patterns):
            if not pattern or len(pattern) > 300:
                raise ValueError("pattern rules must be non-empty and bounded")
            re.compile(pattern)
        return self


class NormalizationProfile(_ContractModel):
    contract: Literal["collector-normalization-profile"] = "collector-normalization-profile"
    contract_revision: Literal["normalization-profile@1"] = Field(
        default="normalization-profile@1",
        alias="contractRevision",
    )
    normalizer_revision: str = Field(alias="normalizerRevision")
    default_phone_region: str | None = Field(
        default=None,
        alias="defaultPhoneRegion",
        pattern=_COUNTRY_PATTERN.pattern,
    )
    field_rules: tuple[NormalizationFieldRule, ...] = Field(
        alias="fieldRules",
        min_length=1,
        max_length=256,
    )
    boolean_pattern_rules: tuple[BooleanPatternRule, ...] = Field(
        default=(),
        alias="booleanPatternRules",
        max_length=256,
    )
    prohibited_fields: tuple[str, ...] = Field(
        default=(),
        alias="prohibitedFields",
        max_length=256,
    )

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        _require_token("normalizer_revision", self.normalizer_revision)
        _require_unique_fields("prohibited_fields", self.prohibited_fields)
        targets = tuple(rule.target_field for rule in self.field_rules)
        if len(set(targets)) != len(targets):
            raise ValueError("normalization field-rule targets must be unique")
        pattern_targets = tuple(rule.target_field for rule in self.boolean_pattern_rules)
        if len(set(pattern_targets)) != len(pattern_targets):
            raise ValueError("boolean pattern-rule targets must be unique")
        overlap = set(targets).intersection(pattern_targets)
        if overlap:
            raise ValueError(f"field and pattern rule targets overlap: {sorted(overlap)}")
        return self


class TextObservationValue(_ContractModel):
    kind: Literal["text"] = "text"
    original: str = Field(min_length=1, max_length=2_000)
    normalized: str = Field(min_length=1, max_length=2_000)
    locale: str | None = Field(default=None, pattern=_LANGUAGE_PATTERN.pattern)


class BooleanObservationValue(_ContractModel):
    kind: Literal["boolean"] = "boolean"
    value: bool


class UrlObservationValue(_ContractModel):
    kind: Literal["url"] = "url"
    original: str = Field(min_length=1, max_length=8_192)
    normalized: str = Field(min_length=1, max_length=8_192)
    registrable_domain: str = Field(alias="registrableDomain", min_length=1, max_length=253)


class EmailObservationValue(_ContractModel):
    kind: Literal["email"] = "email"
    original: str = Field(min_length=3, max_length=320)
    normalized: str = Field(min_length=3, max_length=320)


class PhoneObservationValue(_ContractModel):
    kind: Literal["phone"] = "phone"
    original: str = Field(min_length=3, max_length=100)
    e164: str = Field(pattern=r"^\+[1-9][0-9]{5,14}$")
    region: str | None = Field(default=None, pattern=_COUNTRY_PATTERN.pattern)
    number_type: str = Field(alias="numberType", min_length=1, max_length=50)


class StringSetObservationValue(_ContractModel):
    kind: Literal["string_set"] = "string_set"
    original_values: tuple[str, ...] = Field(alias="originalValues", min_length=1, max_length=128)
    normalized_values: tuple[str, ...] = Field(
        alias="normalizedValues",
        min_length=1,
        max_length=128,
    )

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        if len(set(self.normalized_values)) != len(self.normalized_values):
            raise ValueError("normalized string-set values must be unique")
        return self


class StructuredAddressObservationValue(_ContractModel):
    kind: Literal["structured_address"] = "structured_address"
    street_address: str | None = Field(default=None, alias="streetAddress", max_length=300)
    postal_code: str | None = Field(default=None, alias="postalCode", max_length=32)
    locality: str | None = Field(default=None, max_length=200)
    region: str | None = Field(default=None, max_length=200)
    country_code: str | None = Field(
        default=None,
        alias="countryCode",
        pattern=_COUNTRY_PATTERN.pattern,
    )
    free_form: str | None = Field(default=None, alias="freeForm", max_length=500)

    @model_validator(mode="after")
    def validate_address(self) -> Self:
        if not any(
            (
                self.street_address,
                self.postal_code,
                self.locality,
                self.region,
                self.country_code,
                self.free_form,
            )
        ):
            raise ValueError("normalized address requires at least one component")
        return self


class MoneyObservationValue(_ContractModel):
    kind: Literal["money"] = "money"
    amount: Decimal = Field(gt=0, max_digits=19, decimal_places=4)
    currency: str = Field(pattern=_CURRENCY_PATTERN.pattern)
    basis: str = Field(min_length=1, max_length=100)
    observed_text: str = Field(alias="observedText", min_length=1, max_length=300)


class InvalidObservationValue(_ContractModel):
    kind: Literal["invalid"] = "invalid"
    reason_code: str = Field(alias="reasonCode", pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    original_excerpt: str = Field(alias="originalExcerpt", min_length=1, max_length=120)


type ObservationValue = Annotated[
    TextObservationValue
    | BooleanObservationValue
    | UrlObservationValue
    | EmailObservationValue
    | PhoneObservationValue
    | StringSetObservationValue
    | StructuredAddressObservationValue
    | MoneyObservationValue
    | InvalidObservationValue,
    Field(discriminator="kind"),
]


class FieldObservationPayload(_ContractModel):
    source_record_id: str = Field(alias="sourceRecordId")
    field_key: str = Field(alias="fieldKey")
    state: ObservationState
    value: ObservationValue | None = None
    evidence: EvidenceReference | None = None
    source_policy_digest: str = Field(alias="sourcePolicyDigest")
    normalizer_revision: str = Field(alias="normalizerRevision")
    confidence: Decimal = Field(ge=0, le=1, max_digits=5, decimal_places=4)

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        _require_token("source_record_id", self.source_record_id)
        _require_field_key(self.field_key)
        _require_digest("source_policy_digest", self.source_policy_digest)
        _require_token("normalizer_revision", self.normalizer_revision)
        if self.state is ObservationState.OBSERVED:
            if self.value is None or isinstance(self.value, InvalidObservationValue):
                raise ValueError("observed field requires a valid typed value")
            if self.evidence is None:
                raise ValueError("observed field requires evidence")
        elif self.state is ObservationState.INVALID:
            if not isinstance(self.value, InvalidObservationValue) or self.evidence is None:
                raise ValueError("invalid field requires invalid value detail and evidence")
        elif self.state in {
            ObservationState.ABSENT_IN_SOURCE,
            ObservationState.PROHIBITED_BY_POLICY,
        }:
            if self.value is not None or self.evidence is None:
                raise ValueError("explicit absent/prohibited states require evidence and no value")
        elif self.state in {ObservationState.NOT_OBSERVED, ObservationState.UNSUPPORTED}:
            if self.value is not None or self.evidence is not None:
                raise ValueError("not-observed/unsupported states cannot contain value or evidence")
        elif self.evidence is None:
            raise ValueError("expired/disputed observations require evidence")
        return self


class FieldObservation(FieldObservationPayload):
    observation_id: str = Field(alias="observationId")

    @model_validator(mode="after")
    def validate_observation_id(self) -> Self:
        _require_digest("observation_id", self.observation_id)
        expected = _digest_model(
            FieldObservationPayload.model_validate(
                self.model_dump(mode="python", exclude={"observation_id"})
            )
        )
        if self.observation_id != expected:
            raise ValueError("field observation identity does not match canonical content")
        return self


class ObservationIssue(_ContractModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    message: str = Field(min_length=1, max_length=500)
    field_key: str | None = Field(default=None, alias="fieldKey")

    @model_validator(mode="after")
    def validate_field_key(self) -> Self:
        if self.field_key is not None:
            _require_field_key(self.field_key)
        return self


class ObservationBatchPayload(_ContractModel):
    contract: Literal["collector-field-observation-batch"] = "collector-field-observation-batch"
    contract_revision: Literal["field-observation-batch@1"] = Field(
        default="field-observation-batch@1",
        alias="contractRevision",
    )
    source_record_id: str = Field(alias="sourceRecordId")
    extracted_record_digest: str = Field(alias="extractedRecordDigest")
    raw_artifact_digest: str = Field(alias="rawArtifactDigest")
    source_policy_digest: str = Field(alias="sourcePolicyDigest")
    normalizer_revision: str = Field(alias="normalizerRevision")
    observations: tuple[FieldObservation, ...] = Field(max_length=_MAX_OBSERVATIONS)
    issues: tuple[ObservationIssue, ...] = Field(default=(), max_length=512)

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        _require_token("source_record_id", self.source_record_id)
        _require_digest("extracted_record_digest", self.extracted_record_digest)
        _require_digest("raw_artifact_digest", self.raw_artifact_digest)
        _require_digest("source_policy_digest", self.source_policy_digest)
        _require_token("normalizer_revision", self.normalizer_revision)
        identities = tuple(item.observation_id for item in self.observations)
        if len(set(identities)) != len(identities):
            raise ValueError("field observation identities must be unique within a batch")
        return self


class ObservationBatch(ObservationBatchPayload):
    content_digest: str = Field(alias="contentDigest")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        _require_digest("content_digest", self.content_digest)
        return self


def seal_extracted_record(payload: ExtractedRecordPayload) -> ExtractedRecord:
    return ExtractedRecord(
        **payload.model_dump(mode="python"),
        content_digest=_digest_model(payload),
    )


def verify_extracted_record(record: ExtractedRecord) -> None:
    payload = ExtractedRecordPayload.model_validate(
        record.model_dump(mode="python", exclude={"content_digest"})
    )
    if record.content_digest != _digest_model(payload):
        raise ValueError("extracted record digest does not match canonical content")


def canonical_extracted_record_json(record: ExtractedRecord) -> str:
    verify_extracted_record(record)
    return _canonical_json(record.model_dump(mode="json", by_alias=True)) + "\n"


def decode_extracted_record(content: bytes) -> ExtractedRecord:
    record = ExtractedRecord.model_validate_json(content)
    verify_extracted_record(record)
    return record


def seal_field_observation(payload: FieldObservationPayload) -> FieldObservation:
    return FieldObservation(
        **payload.model_dump(mode="python"),
        observation_id=_digest_model(payload),
    )


def seal_observation_batch(payload: ObservationBatchPayload) -> ObservationBatch:
    return ObservationBatch(
        **payload.model_dump(mode="python"),
        content_digest=_digest_model(payload),
    )


def verify_observation_batch(batch: ObservationBatch) -> None:
    payload = ObservationBatchPayload.model_validate(
        batch.model_dump(mode="python", exclude={"content_digest"})
    )
    if batch.content_digest != _digest_model(payload):
        raise ValueError("observation batch digest does not match canonical content")


def canonical_observation_batch_json(batch: ObservationBatch) -> str:
    verify_observation_batch(batch)
    return _canonical_json(batch.model_dump(mode="json", by_alias=True)) + "\n"


def decode_observation_batch(content: bytes) -> ObservationBatch:
    batch = ObservationBatch.model_validate_json(content)
    verify_observation_batch(batch)
    return batch


def _digest_model(model: BaseModel) -> str:
    content = _canonical_json(model.model_dump(mode="json", by_alias=True)).encode("utf-8")
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _require_digest(name: str, value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be canonical SHA-256")


def _require_token(name: str, value: str) -> None:
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid token format")


def _require_field_key(value: str) -> None:
    if _FIELD_PATTERN.fullmatch(value) is None:
        raise ValueError("field key has an invalid format")


def _require_unique_fields(name: str, values: tuple[str, ...]) -> None:
    for value in values:
        _require_field_key(value)
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must be unique")


def _require_utc(name: str, value: datetime) -> None:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{name} must be timezone-aware UTC")
