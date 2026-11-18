from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, ClassVar, Literal
from uuid import UUID
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

from collection_contracts.observations import NormalizationProfile

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
Sha256Digest = Annotated[
    str,
    StringConstraints(
        pattern=r"^sha256:[0-9a-f]{64}$",
        strip_whitespace=True,
    ),
]
# The Catalog owner publishes hyphenated product keys for sites, catalogs and categories.
# A campaign category such as `voice_over_adr` is a different vocabulary and never a
# substitute, so both grammars are typed separately instead of being normalized into one.
CatalogProductKey = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=80,
        pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
        strip_whitespace=True,
    ),
]
CatalogCategoryKey = CatalogProductKey
# The Catalog publishes localized text under BCP 47 display locales; a campaign locale such
# as `und` (source published no language) is a different vocabulary and is never rewritten.
CatalogLocale = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=35,
        pattern=r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$",
        strip_whitespace=True,
    ),
]
CampaignLocale = CatalogLocale


class StrictContract(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


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


class GeographyDocument(StrictContract):
    schema_revision: Revision
    geography_revision: Revision
    boundary_artifact_path: RelativeFile
    boundary_digest: Sha256Digest
    provenance_artifact_path: RelativeFile
    provenance_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_owned_paths(self) -> GeographyDocument:
        if not self.boundary_artifact_path.startswith("geography/"):
            raise ValueError("boundary artifact must be owned by geography/")
        if not self.boundary_artifact_path.endswith(".geojson"):
            raise ValueError("boundary artifact must be GeoJSON")
        if not self.provenance_artifact_path.startswith("geography/"):
            raise ValueError("provenance artifact must be owned by geography/")
        if not self.provenance_artifact_path.endswith(".provenance.json"):
            raise ValueError("provenance artifact path is invalid")
        if self.boundary_artifact_path == self.provenance_artifact_path:
            raise ValueError("geography artifacts must be distinct")
        return self


class GeographyProvenanceDocument(StrictContract):
    contract: Literal["campaign-geography-provenance"]
    contract_revision: Literal["campaign-geography-provenance-v1"]
    authority: DisplayText
    dataset_title: DisplayText
    dataset_identifier: str = Field(min_length=1, max_length=200)
    distribution_owner: DisplayText
    distribution_url: AnyHttpUrl
    distribution_feature_count: PositiveInt
    derivation: DisplayText
    license_identifier: str = Field(min_length=1, max_length=100)
    license_title: DisplayText
    source_digest: Sha256Digest
    boundary_digest: Sha256Digest
    boundary_artifact_path: RelativeFile


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


class CatalogCategoryMappingEntry(StrictContract):
    """One declared correspondence between a campaign category and a Catalog category.

    The two vocabularies are authored by different owners and are not derived from each
    other, so the correspondence is stated explicitly and pinned to the exact Catalog
    configuration revision it was authored against. Nothing infers a target by rewriting
    a key.
    """

    campaign_category: Key
    catalog_category: CatalogCategoryKey


class CatalogNameLocaleMapping(StrictContract):
    """One declared established-language to Catalog-locale correspondence, sealed.

    Only a language something actually established is mapped: the source that published the
    name, or the operator who entered it. `und` means no language was established, and the
    Catalog has no display locale for it, so a name in `und` stays blocked at export instead
    of being shown under a locale the evidence never supported.
    """

    campaign_locale: CampaignLocale
    catalog_locale: CatalogLocale

    @model_validator(mode="after")
    def reject_undetermined_locale(self) -> CatalogNameLocaleMapping:
        if self.campaign_locale == "und":
            raise ValueError(
                "the undetermined locale `und` states that no language was established and "
                "cannot be given a Catalog display locale"
            )
        return self


class UnmappedCampaignCategory(StrictContract):
    """A campaign category deliberately left unclassified, with the reason it stays blocked."""

    campaign_category: Key
    reason: DisplayText


class CatalogCategoryMappingDocument(StrictContract):
    schema_revision: Revision
    site_key: CatalogProductKey
    catalog_key: CatalogProductKey
    configuration_revision_id: UUID
    categories: tuple[CatalogCategoryMappingEntry, ...] = Field(default=(), alias="categories")
    unmapped: tuple[UnmappedCampaignCategory, ...] = ()
    name_locales: tuple[CatalogNameLocaleMapping, ...] = ()
    # The accuracy this campaign asserts for a source-observed point when it is mapped into
    # this Catalog. The catalog contract requires an accuracy claim for every observed
    # point, and the source geometries carry none, so the campaign states it once, here,
    # instead of an adapter silently choosing one per item.
    coordinate_precision_meters: Decimal = Field(gt=0, le=Decimal("100000"))

    @model_validator(mode="after")
    def validate_mapping(self) -> CatalogCategoryMappingDocument:
        campaign_categories = tuple(item.campaign_category for item in self.categories) + tuple(
            item.campaign_category for item in self.unmapped
        )
        _require_unique("mapped campaign categories", campaign_categories)
        # A second campaign category aimed at the same Catalog category would make the
        # produced proposal ambiguous, so a package cannot carry two claims about one slot.
        _require_unique(
            "target Catalog categories",
            tuple(item.catalog_category for item in self.categories),
        )
        _require_unique(
            "mapped campaign locales", tuple(item.campaign_locale for item in self.name_locales)
        )
        _require_unique(
            "target Catalog locales", tuple(item.catalog_locale for item in self.name_locales)
        )
        return self

    @property
    def name_locale_pairs(self) -> dict[str, str]:
        return {item.campaign_locale: item.catalog_locale for item in self.name_locales}

    @property
    def pairs(self) -> dict[str, str]:
        return {item.campaign_category: item.catalog_category for item in self.categories}

    @property
    def unmapped_categories(self) -> frozenset[str]:
        return frozenset(item.campaign_category for item in self.unmapped)


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


class NormalizationProfileDefinition(StrictContract):
    key: Key
    profile: NormalizationProfile


class NormalizationProfilesDocument(StrictContract):
    schema_revision: Revision
    items: tuple[NormalizationProfileDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_items(self) -> NormalizationProfilesDocument:
        _require_unique("normalization profile keys", tuple(item.key for item in self.items))
        return self


class OsmTagFilterDeclaration(StrictContract):
    key: str = Field(pattern=r"^[A-Za-z0-9_:.-]{1,64}$")
    values: tuple[str, ...] = Field(min_length=1, max_length=100)


class OsmQueryDeclaration(StrictContract):
    """Campaign-declared Overpass query. The polygon comes from verified geography."""

    element_types: tuple[Literal["node", "relation", "way"], ...] = Field(min_length=1)
    tag_filters: tuple[OsmTagFilterDeclaration, ...] = Field(min_length=1, max_length=100)
    timeout_seconds: int = Field(default=60, ge=1, le=180)
    maximum_elements: int = Field(default=25_000, ge=1, le=100_000)


_WIKIDATA_ITEM_ID = re.compile(r"^Q[1-9][0-9]{0,11}$")
_WIKIDATA_MAX_ENTITY_IDS = 50


class WikidataEntityQueryDeclaration(StrictContract):
    """The operator-reviewed entity set one Wikidata acquisition may read.

    It is a declared list, not a discovery result: nothing about it is inferred at run
    time, and it is bounded so an acquisition cannot widen into a crawl.
    """

    entity_ids: tuple[str, ...] = Field(min_length=1, max_length=_WIKIDATA_MAX_ENTITY_IDS)
    label_languages: tuple[str, ...] = Field(default=("de", "en"), min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_declared_set(self) -> WikidataEntityQueryDeclaration:
        if tuple(sorted(set(self.entity_ids))) != self.entity_ids:
            raise ValueError("wikidata_query entity ids must be unique and sorted")
        invalid = [item for item in self.entity_ids if _WIKIDATA_ITEM_ID.fullmatch(item) is None]
        if invalid:
            raise ValueError(f"wikidata_query declares non-item entity ids: {invalid}")
        if len(set(self.label_languages)) != len(self.label_languages):
            raise ValueError("wikidata_query label languages must be unique")
        return self


class WikidataClassificationDeclaration(StrictContract):
    """The operator-authored Wikidata class -> campaign category correspondence.

    An item's `instance of` statements are user-editable, so the correspondence is
    configuration: an entity whose classes do not match it exactly stays ambiguous.
    """

    studio_class_id: str
    campaign_category: Key
    compatible_class_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_declared_map(self) -> WikidataClassificationDeclaration:
        if _WIKIDATA_ITEM_ID.fullmatch(self.studio_class_id) is None:
            raise ValueError("wikidata_classification studio class must be an item id")
        if len(set(self.compatible_class_ids)) != len(self.compatible_class_ids):
            raise ValueError("wikidata_classification compatible classes must be unique")
        invalid = [
            item for item in self.compatible_class_ids if _WIKIDATA_ITEM_ID.fullmatch(item) is None
        ]
        if invalid:
            raise ValueError(f"wikidata_classification declares non-item classes: {invalid}")
        if self.studio_class_id in self.compatible_class_ids:
            raise ValueError("wikidata_classification cannot list its own studio class")
        return self


# Source tags may use a namespace prefix (OSM `contact:website`), so the alias pattern
# allows `:` and `.` in addition to the plain snake_case form.
_SOURCE_TAG_PATTERN = re.compile(r"^[a-z][a-z0-9_.:]{0,99}$")


class SourceFieldProjection(StrictContract):
    """One explicit source field -> candidate field decision.

    Declared in the campaign so a new source field cannot enter a candidate without
    an explicit, versioned projection decision.
    """

    source_field: str = Field(pattern=r"^[a-z][a-z0-9_.:]{0,99}$")
    candidate_field: Literal[
        "names",
        "phones",
        "emails",
        "website_urls",
        "addresses",
        # Source-native point geometry. A projected coordinate lets a candidate be
        # classified against the market area; it is never synthesized from a centroid.
        "coordinates",
    ]
    # Additional source tags that carry the same meaning as `source_field`. These are
    # aliases of one candidate field, not new target attributes: values from every alias
    # are collected under `source_field`, so the target contract never changes.
    source_aliases: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_field(self) -> SourceFieldProjection:
        if not self.source_field.strip():
            raise ValueError("source field projection requires a non-empty source field")
        aliases = tuple(self.source_aliases)
        if len(set(aliases)) != len(aliases):
            raise ValueError("source field aliases must be unique")
        if self.source_field in aliases:
            raise ValueError("a source field cannot alias itself")
        for alias in aliases:
            if not _SOURCE_TAG_PATTERN.fullmatch(alias):
                raise ValueError(f"source field alias {alias!r} is not a valid source tag")
        return self

    @property
    def source_tags(self) -> tuple[str, ...]:
        """Every source tag this projection reads, primary first."""

        return (self.source_field, *self.source_aliases)


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
        "wikidata_entity_query",
    ]
    seed_provider: SeedProvider
    crawl_budget: CrawlBudget
    schedule_enabled: bool
    extraction_profile: Revision
    normalization_profile: Key | None = None
    source_entity_kind: Literal["organization", "place", "provider"] | None = None
    source_field_projection: tuple[SourceFieldProjection, ...] = ()
    osm_query: OsmQueryDeclaration | None = None
    wikidata_query: WikidataEntityQueryDeclaration | None = None
    wikidata_classification: WikidataClassificationDeclaration | None = None

    @model_validator(mode="after")
    def validate_source_native_contract(self) -> SourceBinding:
        if self.capability == "openstreetmap_query":
            if self.seed_provider.kind != "not_applicable":
                raise ValueError("openstreetmap_query cannot depend on a seed file")
            if not self.source_field_projection:
                raise ValueError("openstreetmap_query requires a source field projection")
            if self.osm_query is None:
                raise ValueError("openstreetmap_query requires an osm_query declaration")
            if self.wikidata_query is not None or self.wikidata_classification is not None:
                raise ValueError("openstreetmap_query cannot declare Wikidata contracts")
        elif self.capability == "wikidata_entity_query":
            if self.seed_provider.kind != "not_applicable":
                raise ValueError("wikidata_entity_query cannot depend on a seed file")
            if not self.source_field_projection:
                raise ValueError("wikidata_entity_query requires a source field projection")
            if self.wikidata_query is None:
                raise ValueError("wikidata_entity_query requires a wikidata_query declaration")
            if self.wikidata_classification is None:
                raise ValueError(
                    "wikidata_entity_query requires an explicit wikidata_classification"
                )
            if self.osm_query is not None:
                raise ValueError("wikidata_entity_query cannot declare an osm_query")
        elif (
            self.source_field_projection
            or self.osm_query is not None
            or self.wikidata_query is not None
            or self.wikidata_classification is not None
        ):
            raise ValueError("source field projection belongs only to source-native discovery")
        return self

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


class SeedRoutePageInterest(StrictContract):
    key: Key
    tokens: tuple[str, ...] = Field(min_length=1)
    priority: PositiveInt

    @field_validator("tokens")
    @classmethod
    def validate_tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({token.casefold() for token in value if token.strip()}))
        if not normalized or normalized != value:
            raise ValueError(
                "page-interest tokens must be non-empty, unique, case-folded, and sorted"
            )
        return value


class SeedWebsiteRoute(StrictContract):
    key: Key
    source_field: Literal["website"]
    source_binding: Key
    target_binding: Key
    route_kind: Literal["official_website_page"]
    selection: Literal["exclusive", "fan_out"]
    page_interests: tuple[SeedRoutePageInterest, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_interests(self) -> SeedWebsiteRoute:
        _require_unique("page-interest keys", tuple(item.key for item in self.page_interests))
        return self


class SeedRoutingNotConfigured(StrictContract):
    state: Literal["not_configured"]


class SeedRoutingConfigured(StrictContract):
    state: Literal["configured"]
    routes: tuple[SeedWebsiteRoute, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_routes(self) -> SeedRoutingConfigured:
        _require_unique("seed route keys", tuple(item.key for item in self.routes))
        routes_by_source: dict[tuple[str, str], list[SeedWebsiteRoute]] = {}
        for route in self.routes:
            routes_by_source.setdefault((route.source_binding, route.source_field), []).append(
                route
            )
        for (source_binding, field_key), routes in routes_by_source.items():
            if len(routes) > 1 and any(route.selection != "fan_out" for route in routes):
                raise ValueError(
                    f"multiple routes for {source_binding}.{field_key} require explicit "
                    "fan_out selection"
                )
        return self

    def routes_for_source(
        self, source_binding: str, source_field: str
    ) -> tuple[SeedWebsiteRoute, ...]:
        return tuple(
            route
            for route in self.routes
            if route.source_binding == source_binding and route.source_field == source_field
        )


SeedRouting = Annotated[
    SeedRoutingNotConfigured | SeedRoutingConfigured,
    Field(discriminator="state"),
]


class SourceBindingsDocument(StrictContract):
    schema_revision: Revision
    items: tuple[SourceBinding, ...] = Field(min_length=1)
    seed_routing: SeedRouting = Field(
        default_factory=lambda: SeedRoutingNotConfigured(state="not_configured")
    )

    @model_validator(mode="after")
    def validate_unique_items(self) -> SourceBindingsDocument:
        _require_unique("source binding keys", tuple(item.key for item in self.items))
        return self


class ManualSeedRow(StrictContract):
    row_number: PositiveInt
    expected_entity_kind: Key
    display_name: DisplayText
    # The operator's own classification of the seed, declared beside the facts it
    # accompanies. It is source evidence about the entry, not a campaign-wide default.
    category: Key
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


class UserAgentConfigured(StrictContract):
    state: Literal["configured"]
    value: Annotated[str, StringConstraints(min_length=1, max_length=512)]

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("user agent contains a forbidden line break")
        return value


class UserAgentNotConfigured(StrictContract):
    state: Literal["not_configured"]


UserAgentSetting = Annotated[
    UserAgentConfigured | UserAgentNotConfigured,
    Field(discriminator="state"),
]


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
    user_agent: UserAgentSetting = Field(
        default_factory=lambda: UserAgentNotConfigured(state="not_configured")
    )
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
        "wikidata",
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
