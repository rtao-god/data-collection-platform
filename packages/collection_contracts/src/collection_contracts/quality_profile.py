"""Campaign-owned entity-resolution and quality policy.

Stage 7 evaluates quality per cluster and export consumes that verdict, so the policy that
produces it must be authored data, not a worker default. The campaign declares one profile
whose key matches `campaign.yaml: quality_profile`; the sealed run snapshot carries its exact
bytes, and the quality policy digest is derived from this document alone.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints, model_validator

from collection_contracts.campaign_config import (
    CampaignDocument,
    Key,
    Revision,
    Sha256Digest,
)
from collection_contracts.canonical import canonical_digest

Code = Annotated[
    str,
    StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$"),
]
CandidateFieldKey = Literal["names", "phones", "emails", "website_urls", "addresses"]
GeographyCoverageKey = Literal["inside", "boundary", "outside", "unknown"]

QUALITY_PROFILE_SCHEMA_REVISION: Final[Literal["quality-profile-v1"]] = "quality-profile-v1"


class StrictContract(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class QualityThresholds(StrictContract):
    candidate_limit: PositiveInt
    pair_limit: PositiveInt
    name_review_minimum_bps: int = Field(ge=0, le=10_000)
    address_review_minimum_bps: int = Field(ge=0, le=10_000)
    website_auto_match_minimum_strong_features: int = Field(ge=2, le=4)
    address_auto_match_minimum_strong_features: int = Field(ge=2, le=4)
    fuzzy_primary_area_review_reason_code: Code


class QualityRule(StrictContract):
    entity_kind: Key
    required_fields: tuple[CandidateFieldKey, ...] = Field(min_length=1)
    single_value_fields: tuple[CandidateFieldKey, ...] = ()
    minimum_distinct_source_count: PositiveInt
    allowed_geography: tuple[GeographyCoverageKey, ...] = Field(min_length=1)
    boundary_requires_review: bool
    pending_review_blocks_export: bool

    @model_validator(mode="after")
    def validate_canonical_sets(self) -> Self:
        for name in ("required_fields", "single_value_fields", "allowed_geography"):
            values = tuple(getattr(self, name))
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must be unique")
        return self


class QualityProfileDocument(StrictContract):
    schema_revision: Literal["quality-profile-v1"] = QUALITY_PROFILE_SCHEMA_REVISION
    profile_key: Revision
    thresholds: QualityThresholds
    quality_rules: tuple[QualityRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        kinds = tuple(rule.entity_kind for rule in self.quality_rules)
        if len(set(kinds)) != len(kinds):
            raise ValueError("quality_rules must declare one rule per entity kind")
        return self

    def require_campaign_binding(self, campaign: CampaignDocument) -> None:
        """Fail closed when the profile does not belong to this campaign."""
        if self.profile_key != campaign.quality_profile:
            raise ValueError(
                "quality profile key does not match campaign.quality_profile: "
                f"{self.profile_key} != {campaign.quality_profile}"
            )
        unknown = sorted(
            {rule.entity_kind for rule in self.quality_rules}.difference(campaign.entity_kinds)
        )
        if unknown:
            raise ValueError(f"quality profile declares rules for unknown entity kinds: {unknown}")


def quality_profile_digest(document: QualityProfileDocument) -> Sha256Digest:
    """The single policy identity every resolution and export decision refers to."""
    return canonical_digest(document.model_dump(mode="json"))
