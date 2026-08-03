from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    field_validator,
    model_validator,
)

Key = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]*$",
        strip_whitespace=True,
    ),
]
DisplayText = Annotated[str, StringConstraints(min_length=1, max_length=300, strip_whitespace=True)]
Revision = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
        strip_whitespace=True,
    ),
]
RelativeFile = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=240,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./-]*$",
        strip_whitespace=True,
    ),
]


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CampaignBlocker(StrictContract):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    owner: str = Field(min_length=1, max_length=100)
    message: DisplayText
    required_action: DisplayText


class ReadyCampaign(StrictContract):
    state: Literal["ready"]


class BlockedCampaign(StrictContract):
    state: Literal["blocked"]
    blockers: tuple[CampaignBlocker, ...] = Field(min_length=1)


CampaignReadiness = Annotated[ReadyCampaign | BlockedCampaign, Field(discriminator="state")]


class CampaignDocument(StrictContract):
    schema_revision: Revision
    campaign_key: Key
    display_name: DisplayText
    locales: tuple[str, ...] = Field(min_length=1)
    timezone: str = Field(min_length=1, max_length=80)
    entity_kinds: tuple[Key, ...] = Field(min_length=1)
    target_categories: tuple[Key, ...] = Field(min_length=1)
    geography_revision: Revision
    enabled_source_bindings: tuple[Key, ...] = Field(min_length=1)
    quality_profile: Revision
    export_profile: Revision
    readiness: CampaignReadiness

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def validate_unique_references(self) -> CampaignDocument:
        _require_unique("locales", self.locales)
        _require_unique("entity_kinds", self.entity_kinds)
        _require_unique("target_categories", self.target_categories)
        _require_unique("enabled_source_bindings", self.enabled_source_bindings)
        return self


class EntityKind(StrictContract):
    key: Key
    display_name: DisplayText
    description: DisplayText


class EntityKindsDocument(StrictContract):
    schema_revision: Revision
    items: tuple[EntityKind, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_items(self) -> EntityKindsDocument:
        _require_unique("entity kind keys", tuple(item.key for item in self.items))
        return self


class TaxonomyCategory(StrictContract):
    key: Key
    display_name: DisplayText
    applicable_entity_kinds: tuple[Key, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_kinds(self) -> TaxonomyCategory:
        _require_unique("applicable entity kinds", self.applicable_entity_kinds)
        return self


class TaxonomyDocument(StrictContract):
    schema_revision: Revision
    items: tuple[TaxonomyCategory, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_items(self) -> TaxonomyDocument:
        _require_unique("taxonomy keys", tuple(item.key for item in self.items))
        return self


class UnitNotApplicable(StrictContract):
    state: Literal["not_applicable"]


class DefinedUnit(StrictContract):
    state: Literal["defined"]
    value: str = Field(min_length=1, max_length=40)


UnitContract = Annotated[UnitNotApplicable | DefinedUnit, Field(discriminator="state")]


class OptionsNotApplicable(StrictContract):
    state: Literal["not_applicable"]


class DefinedOptions(StrictContract):
    state: Literal["defined"]
    values: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_options(self) -> DefinedOptions:
        _require_unique("attribute options", self.values)
        return self


OptionsContract = Annotated[OptionsNotApplicable | DefinedOptions, Field(discriminator="state")]


class AttributeDefinition(StrictContract):
    key: Key
    display_name: DisplayText
    value_type: Literal[
        "boolean",
        "decimal",
        "email",
        "enum",
        "external_reference",
        "integer",
        "localized_text",
        "measurement",
        "money",
        "phone",
        "set",
        "structured_address",
        "text",
        "url",
    ]
    cardinality: Literal["one", "many"]
    unit: UnitContract
    options: OptionsContract
    normalization_rule: Revision
    missing_allowed: bool
    evidence_required: bool

    @model_validator(mode="after")
    def validate_options_match_type(self) -> AttributeDefinition:
        if self.value_type == "enum" and self.options.state != "defined":
            raise ValueError("enum attribute requires defined options")
        if self.value_type != "enum" and self.options.state != "not_applicable":
            raise ValueError("non-enum attribute cannot define enum options")
        return self


class AttributesDocument(StrictContract):
    schema_revision: Revision
    items: tuple[AttributeDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_items(self) -> AttributesDocument:
        _require_unique("attribute keys", tuple(item.key for item in self.items))
        return self


class FileSeedProvider(StrictContract):
    kind: Literal["file"]
    path: RelativeFile
    format: Literal["csv", "json", "jsonl"]


class SeedNotApplicable(StrictContract):
    kind: Literal["not_applicable"]


SeedProvider = Annotated[FileSeedProvider | SeedNotApplicable, Field(discriminator="kind")]


class CrawlBudgetNotApplicable(StrictContract):
    state: Literal["not_applicable"]


class DefinedCrawlBudget(StrictContract):
    state: Literal["defined"]
    max_work_units: PositiveInt
    max_total_bytes: PositiveInt


CrawlBudget = Annotated[
    CrawlBudgetNotApplicable | DefinedCrawlBudget,
    Field(discriminator="state"),
]


class SourceBinding(StrictContract):
    key: Key
    source_key: Key
    connector_key: Key
    source_policy_key: Key
    capability: Literal[
        "browser_fetch",
        "http_fetch",
        "manual_import",
        "openstreetmap_query",
    ]
    seed_provider: SeedProvider
    crawl_budget: CrawlBudget
    schedule_enabled: bool
    extraction_profile: Revision

    @model_validator(mode="after")
    def validate_capability_contract(self) -> SourceBinding:
        if self.capability == "manual_import":
            if self.seed_provider.kind != "file":
                raise ValueError("manual_import requires a file seed provider")
            if self.crawl_budget.state != "not_applicable":
                raise ValueError("manual_import cannot define a crawl budget")
            if self.schedule_enabled:
                raise ValueError("manual_import schedule must be disabled")
        return self


class SourceBindingsDocument(StrictContract):
    schema_revision: Revision
    items: tuple[SourceBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_items(self) -> SourceBindingsDocument:
        _require_unique("source binding keys", tuple(item.key for item in self.items))
        return self


class ManualSeedRow(StrictContract):
    row_number: PositiveInt
    expected_entity_kind: Key
    display_name: DisplayText
    website: AnyHttpUrl | None
    osm_id: str | None = Field(max_length=120)
    reference_urls: tuple[AnyHttpUrl, ...]
    note: str | None = Field(max_length=1000)
    provenance: DisplayText

    @model_validator(mode="after")
    def validate_references(self) -> ManualSeedRow:
        normalized = tuple(str(value) for value in self.reference_urls)
        _require_unique("reference URLs", normalized)
        return self


class ReviewedTerms(StrictContract):
    state: Literal["reviewed"]
    terms_url: AnyHttpUrl
    reviewed_at: date
    expires_at: date
    reviewer: DisplayText

    @model_validator(mode="after")
    def validate_dates(self) -> ReviewedTerms:
        if self.expires_at <= self.reviewed_at:
            raise ValueError("terms expires_at must be later than reviewed_at")
        return self


class TermsNotApplicable(StrictContract):
    state: Literal["not_applicable"]
    reason: DisplayText


TermsReview = Annotated[ReviewedTerms | TermsNotApplicable, Field(discriminator="state")]


class ManualAccess(StrictContract):
    kind: Literal["manual"]
    accepted_formats: tuple[Literal["csv", "json", "jsonl"], ...] = Field(min_length=1)
    accepted_encodings: tuple[Literal["utf-8"], ...] = Field(min_length=1)
    max_file_bytes: PositiveInt
    partial_mode_allowed: bool

    @model_validator(mode="after")
    def validate_unique_values(self) -> ManualAccess:
        _require_unique("accepted formats", self.accepted_formats)
        _require_unique("accepted encodings", self.accepted_encodings)
        return self


class NetworkAccess(StrictContract):
    kind: Literal["network"]
    allowed_methods: tuple[Literal["GET", "HEAD", "POST"], ...] = Field(min_length=1)
    allowed_host_patterns: tuple[str, ...] = Field(min_length=1)
    max_requests_per_second: float = Field(gt=0)
    max_concurrency: PositiveInt
    connect_timeout_seconds: float = Field(gt=0)
    read_timeout_seconds: float = Field(gt=0)
    max_encoded_bytes: PositiveInt
    max_decoded_bytes: PositiveInt
    content_type_allowlist: tuple[str, ...] = Field(min_length=1)
    redirect_limit: NonNegativeInt

    @model_validator(mode="after")
    def validate_network_values(self) -> NetworkAccess:
        _require_unique("allowed methods", self.allowed_methods)
        _require_unique("allowed host patterns", self.allowed_host_patterns)
        _require_unique("content types", self.content_type_allowlist)
        if self.max_decoded_bytes < self.max_encoded_bytes:
            raise ValueError("max_decoded_bytes cannot be lower than max_encoded_bytes")
        return self


SourceAccess = Annotated[ManualAccess | NetworkAccess, Field(discriminator="kind")]


class SourcePolicy(StrictContract):
    schema_revision: Revision
    policy_key: Key
    policy_revision: Revision
    source_key: Key
    source_type: Literal[
        "manual_import",
        "official_directory",
        "official_website",
        "openstreetmap",
        "reference",
    ]
    legal_status: Literal["approved", "blocked", "expired", "reference_only", "research_only"]
    terms_review: TermsReview
    access: SourceAccess
    storage_class: Revision
    retention_days: NonNegativeInt
    allowed_fields: tuple[Key, ...] = Field(min_length=1)
    prohibited_fields: tuple[Key, ...]
    attribution_required: bool
    browser: Literal["allowed", "forbidden"]
    screenshots: Literal["allowed", "forbidden"]
    retry_budget: NonNegativeInt
    stop_on_status_codes: tuple[int, ...]
    reviewer: DisplayText
    approver: DisplayText

    @model_validator(mode="after")
    def validate_source_policy(self) -> SourcePolicy:
        _require_unique("allowed fields", self.allowed_fields)
        _require_unique("prohibited fields", self.prohibited_fields)
        _require_unique("stop status codes", self.stop_on_status_codes)
        overlap = set(self.allowed_fields).intersection(self.prohibited_fields)
        if overlap:
            raise ValueError(f"fields cannot be both allowed and prohibited: {sorted(overlap)}")
        if self.source_type == "manual_import" and self.access.kind != "manual":
            raise ValueError("manual_import source requires manual access contract")
        if self.access.kind == "manual" and self.terms_review.state != "not_applicable":
            raise ValueError("manual access requires explicit not_applicable terms state")
        if self.access.kind == "manual" and self.browser != "forbidden":
            raise ValueError("manual access cannot allow browser acquisition")
        return self


def _require_unique(label: str, values: tuple[object, ...]) -> None:
    seen: set[object] = set()
    duplicates: set[object] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise ValueError(f"duplicate {label}: {sorted(str(value) for value in duplicates)}")
