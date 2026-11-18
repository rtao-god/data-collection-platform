from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from collection_contracts.canonical import canonical_digest

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Key = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")]
# BCP 47 in its canonical short form: `und` is the only defensible value when the source
# published no language for a name, and the export must never invent `de` for it.
Locale = Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]
# An export profile is a campaign-declared revision, not a bare key: the shipped campaign
# declares `collector-candidate-export-v1`, which a `Key` pattern would reject outright.
ProfileRevision = Annotated[
    str, Field(min_length=3, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
]

# Field states the export can prove. They mirror the collection observation states: an
# absent-in-source fact is not the same thing as a value nobody collected.
FieldState = Literal["observed", "unknown", "not_observed", "absent_in_source", "disputed"]
# Backend-admitted usage policies. `research_only` and `forbidden` never appear here: an
# export is a redistribution act, so a restricted source cannot enter the package at all.
UsagePolicy = Literal["publishable", "display_with_attribution", "link_only", "owner_authorized"]
"""The strongest spread a source's evidence may receive in a published catalog."""

ContactKind = Literal["website", "email", "phone"]

# Evidence is addressed by the field it proves. A grouped field carries one segment
# (`names`), while a field the consumer addresses per member carries the member after a
# slash (`categories/recording_studio`), so one claim can cite exactly the evidence that
# supports it instead of a bundle-wide digest.
EvidencePath = Annotated[
    str,
    Field(min_length=1, max_length=200, pattern=r"^[a-z][a-z0-9_]*(?:/[a-z0-9][a-z0-9_-]*)*$"),
]
# The Catalog owner publishes hyphenated product keys. A campaign category key is a
# different vocabulary and is never transcribed into this grammar by substitution.
CatalogCategoryKey = Annotated[
    str, Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
]


class ExportContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


class ExportFieldEvidence(ExportContract):
    """Where one exported field came from, in the source's own terms.

    `fieldPath` is the candidate field the evidence supports, so a downstream consumer can
    attach exactly one provenance record to exactly one proposal instead of re-deriving
    lineage from a flat value list.
    """

    field_path: EvidencePath = Field(alias="fieldPath")
    source_key: Key = Field(alias="sourceKey")
    source_record_id: Annotated[str, Field(alias="sourceRecordId", min_length=1, max_length=200)]
    source_url: Annotated[str, Field(alias="sourceUrl", min_length=1, max_length=8_192)]
    observed_at_utc: datetime = Field(alias="observedAtUtc")
    legal_status: Key = Field(alias="legalStatus")
    usage_policy: UsagePolicy = Field(alias="usagePolicy")
    evidence_digest: Digest = Field(alias="evidenceDigest")
    source_policy_digest: Digest = Field(alias="sourcePolicyDigest")
    attribution: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @field_validator("observed_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("export evidence timestamp must be timezone-aware")
        return value.astimezone(UTC)


class ExportLocalizedName(ExportContract):
    locale: Locale
    state: FieldState
    value: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    field_path: Key = Field(alias="fieldPath")

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.state == "observed" and self.value is None:
            raise ValueError("an observed exported name requires its value")
        if self.state != "observed" and self.value is not None:
            raise ValueError("a non-observed exported name cannot carry a value")
        return self


class ExportContact(ExportContract):
    kind: ContactKind
    state: FieldState
    target: Annotated[str, Field(min_length=1, max_length=2_048)] | None = None
    field_path: Key = Field(alias="fieldPath")

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.state == "observed" and self.target is None:
            raise ValueError("an observed exported contact requires its target")
        if self.state != "observed" and self.target is not None:
            raise ValueError("a non-observed exported contact cannot carry a target")
        return self


class ExportTypedAttribute(ExportContract):
    attribute_key: Key = Field(alias="attributeKey")
    state: FieldState
    text_value: Annotated[str, Field(min_length=1, max_length=2_000)] | None = Field(
        default=None, alias="textValue"
    )
    field_path: Key = Field(alias="fieldPath")

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.state == "observed" and self.text_value is None:
            raise ValueError("an observed exported attribute requires its value")
        if self.state != "observed" and self.text_value is not None:
            raise ValueError("a non-observed exported attribute cannot carry a value")
        return self


class ExportGeography(ExportContract):
    """Coverage decided by the export owner against the run's exact boundary artifact."""

    coverage: Literal["inside", "boundary"]
    market_area_revision: Key = Field(alias="marketAreaRevision")
    market_area_digest: Digest = Field(alias="marketAreaDigest")
    latitude: Annotated[float, Field(ge=-90, le=90)] | None = None
    longitude: Annotated[float, Field(ge=-180, le=180)] | None = None
    field_path: Key = Field(alias="fieldPath")

    @model_validator(mode="after")
    def validate_point(self) -> Self:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("exported geography coordinates must be provided together")
        if self.latitude is None:
            raise ValueError("an exported catalog entry requires its observed point")
        return self


class ExportQualityIssue(ExportContract):
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")]
    severity: Literal["blocking", "warning", "information"]
    required_action: Annotated[str, Field(alias="requiredAction", min_length=1, max_length=500)]


class ExportQualitySummary(ExportContract):
    state: Literal["passed", "review_required", "blocked"]
    issues: tuple[ExportQualityIssue, ...] = ()


class ExportReviewDecision(ExportContract):
    decision_id: UUID = Field(alias="decisionId")
    decision_kind: Annotated[str, Field(alias="decisionKind", min_length=1, max_length=100)]
    decision_digest: Digest = Field(alias="decisionDigest")
    decided_at_utc: datetime = Field(alias="decidedAtUtc")

    @field_validator("decided_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("export review timestamp must be timezone-aware")
        return value.astimezone(UTC)


class ExportSourcePolicy(ExportContract):
    """The exact source policy the exported evidence was collected under.

    `legal_status` is the collector's original grant and is never converted. `usage_policy`
    states the redistribution right that grant actually carries for this published entry, so
    `link_only` always means a genuine `reference_only` source and never a downgraded
    `research_only` one.
    """

    source_key: Key = Field(alias="sourceKey")
    policy_digest: Digest = Field(alias="policyDigest")
    legal_status: Key = Field(alias="legalStatus")
    usage_policy: UsagePolicy = Field(alias="usagePolicy")
    attribution: Annotated[str, Field(min_length=1, max_length=200)] | None = None


_UNSEALED_DIGEST = "sha256:" + "0" * 64


class ExportCandidateEntry(ExportContract):
    # Field names are the wire names; the contract stays flat so a consumer reads one
    # document per candidate revision.
    """One export-eligible catalog entry, self-sufficient for downstream transformation.

    Everything a consumer needs to build catalog proposals without reading collection
    state is sealed here: typed field values with their states, per-field evidence, the
    decided geography, the quality assessment and the exact review decisions.
    """

    candidate_id: UUID = Field(alias="candidateId")
    candidate_revision: Annotated[int, Field(ge=0, alias="candidateRevision")]
    snapshot_digest: Digest = Field(alias="snapshotDigest")
    entity_kind: Key = Field(alias="entityKind")
    cluster_id: UUID = Field(alias="clusterId")
    localized_names: tuple[ExportLocalizedName, ...] = Field(alias="localizedNames")
    contacts: tuple[ExportContact, ...] = ()
    typed_attributes: tuple[ExportTypedAttribute, ...] = Field(default=(), alias="typedAttributes")
    category_keys: tuple[Key, ...] = Field(alias="categoryKeys")
    geography: ExportGeography
    field_evidence: tuple[ExportFieldEvidence, ...] = Field(alias="fieldEvidence")
    quality: ExportQualitySummary
    quality_evaluation_digest: Digest = Field(alias="qualityEvaluationDigest")
    quality_policy_digest: Digest = Field(alias="qualityPolicyDigest")
    review_decisions: tuple[ExportReviewDecision, ...] = Field(alias="reviewDecisions")
    source_policies: tuple[ExportSourcePolicy, ...] = Field(alias="sourcePolicies")
    source_artifact_ids: tuple[UUID, ...] = Field(min_length=1, alias="sourceArtifactIds")
    content_digest: Digest = Field(alias="contentDigest")

    @model_validator(mode="after")
    def validate_entry(self) -> Self:
        observed_names = tuple(item for item in self.localized_names if item.state == "observed")
        if not observed_names:
            raise ValueError("an exported candidate requires at least one observed name")
        for name in ("category_keys", "source_artifact_ids", "source_policies"):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"export candidate {name} must be unique")
        if not self.category_keys:
            raise ValueError("an exported candidate requires at least one proven category")
        if not self.source_policies:
            raise ValueError("an exported candidate requires its exact source policies")
        if not self.field_evidence:
            raise ValueError("an exported candidate requires field-level evidence")
        evidenced = {item.field_path for item in self.field_evidence}
        policies = {item.source_key: item for item in self.source_policies}
        declared = (
            {item.field_path for item in self.localized_names}
            | {item.field_path for item in self.contacts}
            | {item.field_path for item in self.typed_attributes}
            | {self.geography.field_path}
            | {f"categories/{category}" for category in self.category_keys}
        )
        missing = declared - evidenced
        if missing:
            raise ValueError(
                "exported fields have no matching field evidence: " + ", ".join(sorted(missing))
            )
        for evidence in self.field_evidence:
            policy = policies.get(evidence.source_key)
            if policy is None:
                raise ValueError(f"field evidence names undeclared source {evidence.source_key!r}")
            if evidence.source_policy_digest != policy.policy_digest:
                raise ValueError(
                    f"field evidence for {evidence.field_path!r} disagrees with its source "
                    "policy digest"
                )
            if policy.usage_policy == "link_only" and evidence.usage_policy != "link_only":
                raise ValueError(
                    f"field evidence for {evidence.field_path!r} widens a link-only source"
                )
        return self

    def verify_content_digest(self) -> Self:
        """Prove the entry is exactly what its declared content digest covers."""

        if self.content_digest != export_candidate_digest(self):
            raise ValueError("candidate content digest does not match exported content")
        return self


def seal_export_candidate(document: Mapping[str, object]) -> ExportCandidateEntry:
    """Validate one candidate document and seal it with its content digest.

    The digest must cover the normalized wire document, so it is computed from the
    validated model rather than from the caller's raw values: ints, floats, UUID strings
    and timestamps all normalize on validation, and a digest over raw input would not be
    reproducible by a consumer.
    """

    draft = {key: value for key, value in document.items() if key != "contentDigest"}
    candidate = dict(draft)
    candidate["contentDigest"] = _UNSEALED_DIGEST
    entry = ExportCandidateEntry.model_validate(candidate)
    normalized = {
        key: value
        for key, value in entry.model_dump(mode="json", by_alias=True).items()
        if key != "contentDigest"
    }
    sealed = ExportCandidateEntry.model_validate(
        {**draft, "contentDigest": export_candidate_content_digest(normalized)}
    )
    return sealed.verify_content_digest()


class ExportCatalogCategoryMapping(ExportContract):
    """One declared campaign-category to Catalog-category correspondence, sealed."""

    campaign_category: Key = Field(alias="campaignCategory")
    catalog_category: CatalogCategoryKey = Field(alias="catalogCategory")


class ExportCatalogNameLocale(ExportContract):
    """One declared established-language to Catalog display-locale correspondence, sealed.

    `und` states that no language was established for a name, so it can never be the source
    side of a display-locale claim: a consumer would otherwise publish under a language the
    evidence never supported.
    """

    campaign_locale: Annotated[
        str, Field(alias="campaignLocale", pattern=r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    ]
    catalog_locale: Annotated[
        str, Field(alias="catalogLocale", pattern=r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    ]

    @model_validator(mode="after")
    def reject_undetermined_locale(self) -> ExportCatalogNameLocale:
        if self.campaign_locale == "und":
            raise ValueError(
                "the undetermined locale `und` cannot be sealed as a Catalog display locale"
            )
        return self


class ExportCatalogBinding(ExportContract):
    """The exact Catalog target these categories were classified against.

    The mapping is pinned to one configuration revision so a consumer can prove that the
    categories it proposes were authored against the catalog revision it was told to feed,
    rather than resolved against whatever the target happened to publish later.
    """

    site_key: Annotated[
        str, Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
    ] = Field(alias="siteKey")
    catalog_key: Annotated[
        str, Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
    ] = Field(alias="catalogKey")
    configuration_revision_id: UUID = Field(alias="configurationRevisionId")
    categories: tuple[ExportCatalogCategoryMapping, ...] = Field(min_length=1)
    name_locales: tuple[ExportCatalogNameLocale, ...] = Field(default=(), alias="nameLocales")
    # The campaign's declared accuracy claim for the points it maps into this Catalog.
    coordinate_precision_meters: Decimal = Field(
        alias="coordinatePrecisionMeters", gt=0, le=Decimal("100000")
    )

    @model_validator(mode="after")
    def validate_categories(self) -> Self:
        campaign_categories = tuple(item.campaign_category for item in self.categories)
        if len(campaign_categories) != len(set(campaign_categories)):
            raise ValueError("catalog binding declares a campaign category more than once")
        catalog_categories = tuple(item.catalog_category for item in self.categories)
        if len(catalog_categories) != len(set(catalog_categories)):
            raise ValueError("catalog binding maps two campaign categories to one target")
        campaign_locales = tuple(item.campaign_locale for item in self.name_locales)
        if len(campaign_locales) != len(set(campaign_locales)):
            raise ValueError("catalog binding declares a campaign locale more than once")
        catalog_locales = tuple(item.catalog_locale for item in self.name_locales)
        if len(catalog_locales) != len(set(catalog_locales)):
            raise ValueError("catalog binding maps two campaign locales to one target")
        return self

    @property
    def targets(self) -> Mapping[str, str]:
        return {item.campaign_category: item.catalog_category for item in self.categories}


class ExportPackagePayload(ExportContract):
    contract: Literal["collector-export-package"] = "collector-export-package"
    contract_revision: Literal["export-package@2"] = Field(
        default="export-package@2",
        alias="contractRevision",
    )
    run_id: UUID = Field(alias="runId")
    campaign_bundle_digest: Digest = Field(alias="campaignBundleDigest")
    campaign_key: Key = Field(alias="campaignKey")
    export_profile_key: ProfileRevision = Field(alias="exportProfileKey")
    export_profile_revision: ProfileRevision = Field(alias="exportProfileRevision")
    quality_profile_digest: Digest = Field(alias="qualityProfileDigest")
    catalog_binding: ExportCatalogBinding = Field(alias="catalogBinding")
    entries: tuple[ExportCandidateEntry, ...]
    generated_at_utc: datetime = Field(alias="generatedAtUtc")
    correlation_id: Annotated[str, Field(alias="correlationId", min_length=1, max_length=200)]

    @field_validator("generated_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("export timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        identities = tuple((item.candidate_id, item.candidate_revision) for item in self.entries)
        if len(identities) != len(set(identities)):
            raise ValueError("export package contains duplicate candidate revisions")
        keys = tuple(item.candidate_id for item in self.entries)
        if len(keys) != len(set(keys)):
            raise ValueError("export package must contain one revision per candidate")
        if tuple(sorted(keys)) != keys:
            raise ValueError("export package entries must be ordered by candidate id")
        targets = self.catalog_binding.targets
        unmapped = sorted(
            {
                category
                for entry in self.entries
                for category in entry.category_keys
                if category not in targets
            }
        )
        if unmapped:
            raise ValueError(
                "exported categories have no declared Catalog target: " + ", ".join(unmapped)
            )
        locales = {item.campaign_locale for item in self.catalog_binding.name_locales}
        unlocalized = sorted(
            {
                name.locale
                for entry in self.entries
                for name in entry.localized_names
                if name.locale not in locales
            }
        )
        if unlocalized:
            raise ValueError(
                "exported names use campaign locales the binding does not map: "
                + ", ".join(unlocalized)
            )
        return self


class ExportPackage(ExportPackagePayload):
    package_digest: Digest = Field(alias="packageDigest")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        # The digest covers the wire document, so verification must serialize the same way
        # the sealer did; python-mode dumps keep datetimes unnormalized and would not match.
        document = self.model_dump(mode="json", by_alias=True)
        document.pop("packageDigest", None)
        expected = export_package_digest(ExportPackagePayload.model_validate(document))
        if self.package_digest != expected:
            raise ValueError("package_digest does not match the canonical package content")
        return self


def export_candidate_digest(entry: ExportCandidateEntry) -> str:
    """Deterministic identity of one exported candidate revision.

    Excludes `content_digest` itself so the digest can be verified independently of the
    field that carries it, and stays stable across re-exports of unchanged evidence.
    """

    document = entry.model_dump(mode="json", by_alias=True)
    document.pop("contentDigest", None)
    return export_candidate_content_digest(document)


def export_candidate_content_digest(document: Mapping[str, object]) -> str:
    """Digest of an already-serialized entry document without its content digest."""

    return canonical_digest(document)


def _wire_timestamp(value: datetime) -> str:
    """The exact timestamp form used on the wire, so seals and verifications agree."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def export_package_digest(payload: ExportPackagePayload) -> str:
    return canonical_digest(
        {
            "campaignBundleDigest": payload.campaign_bundle_digest,
            "campaignKey": payload.campaign_key,
            "catalogBinding": payload.catalog_binding.model_dump(mode="json", by_alias=True),
            "contract": payload.contract,
            "contractRevision": payload.contract_revision,
            "entries": [item.model_dump(mode="json", by_alias=True) for item in payload.entries],
            "exportProfileKey": payload.export_profile_key,
            "exportProfileRevision": payload.export_profile_revision,
            "generatedAtUtc": _wire_timestamp(payload.generated_at_utc),
            "qualityProfileDigest": payload.quality_profile_digest,
            "runId": str(payload.run_id),
        }
    )


def seal_export_package(payload: ExportPackagePayload) -> ExportPackage:
    validated = ExportPackagePayload.model_validate(payload.model_dump(mode="json", by_alias=True))
    document = validated.model_dump(mode="json", by_alias=True)
    document["packageDigest"] = export_package_digest(validated)
    return ExportPackage.model_validate(document)


def verify_export_package(package: ExportPackage) -> None:
    document = package.model_dump(mode="json", by_alias=True)
    document.pop("packageDigest", None)
    payload = ExportPackagePayload.model_validate(document)
    if package.package_digest != export_package_digest(payload):
        raise ValueError("export package digest does not match canonical content")


def canonical_export_package_json(package: ExportPackage) -> str:
    import json

    verify_export_package(package)
    return (
        json.dumps(
            package.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def decode_export_package(content: bytes) -> ExportPackage:
    package = ExportPackage.model_validate_json(content)
    verify_export_package(package)
    return package
