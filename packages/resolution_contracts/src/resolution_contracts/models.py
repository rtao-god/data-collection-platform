from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from resolution_contracts.identity import candidate_pair_id, deterministic_cluster_id

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._:@+-]{0,127}$")
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_E164_PATTERN = re.compile(r"^\+[1-9][0-9]{6,14}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_MAX_CANDIDATES = 10_000
_MAX_PAIRS = 1_000_000
_MAX_VALUES = 64
_MAX_REFERENCES = 512


class _ContractModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


class EntityKind(StrEnum):
    ORGANIZATION = "organization"
    PLACE = "place"
    PROVIDER = "provider"


class GeographyCoverage(StrEnum):
    INSIDE = "inside"
    BOUNDARY = "boundary"
    OUTSIDE = "outside"
    UNKNOWN = "unknown"


class MarketAreaReadiness(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"


class ManualDecisionAction(StrEnum):
    MATCH = "match"
    SEPARATE = "separate"


class MatchDisposition(StrEnum):
    AUTO_MATCH = "auto_match"
    MANUAL_MATCH = "manual_match"
    REVIEW_REQUIRED = "review_required"
    NO_MATCH = "no_match"
    MANUAL_SEPARATE = "manual_separate"


class ClusterLineageKind(StrEnum):
    NEW = "new"
    UNCHANGED = "unchanged"
    SPLIT = "split"
    MERGE = "merge"
    RECOMBINED = "recombined"


class CandidateField(StrEnum):
    NAMES = "names"
    PHONES = "phones"
    EMAILS = "emails"
    WEBSITE_URLS = "website_urls"
    ADDRESSES = "addresses"


class QualitySeverity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFORMATION = "information"


class GeographyReference(_ContractModel):
    coverage: GeographyCoverage
    market_area_revision: str = Field(alias="marketAreaRevision")
    market_area_digest: str = Field(alias="marketAreaDigest")
    latitude: Decimal | None = Field(default=None, ge=Decimal("-90"), le=Decimal("90"))
    longitude: Decimal | None = Field(default=None, ge=Decimal("-180"), le=Decimal("180"))
    observation_ids: tuple[UUID, ...] = Field(
        default=(), alias="observationIds", max_length=_MAX_REFERENCES
    )

    @model_validator(mode="after")
    def validate_geography(self) -> Self:
        _require_token("market_area_revision", self.market_area_revision)
        _require_digest("market_area_digest", self.market_area_digest)
        _require_sorted_unique_uuids("observation_ids", self.observation_ids)
        has_point = self.latitude is not None and self.longitude is not None
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("geography coordinates must be provided together")
        if self.coverage is GeographyCoverage.UNKNOWN:
            if has_point or self.observation_ids:
                raise ValueError("unknown geography must not invent coordinates or observations")
        elif not has_point:
            raise ValueError("classified geography requires coordinates")
        elif not self.observation_ids:
            raise ValueError("classified geography requires observation provenance")
        return self


class ResolutionCandidate(_ContractModel):
    candidate_id: UUID = Field(alias="candidateId")
    entity_kind: EntityKind = Field(alias="entityKind")
    names: tuple[str, ...] = Field(default=(), max_length=_MAX_VALUES)
    phones: tuple[str, ...] = Field(default=(), max_length=_MAX_VALUES)
    emails: tuple[str, ...] = Field(default=(), max_length=_MAX_VALUES)
    website_urls: tuple[str, ...] = Field(default=(), alias="websiteUrls", max_length=_MAX_VALUES)
    addresses: tuple[str, ...] = Field(default=(), max_length=_MAX_VALUES)
    observation_ids: tuple[UUID, ...] = Field(
        default=(), alias="observationIds", max_length=_MAX_REFERENCES
    )
    source_artifact_ids: tuple[UUID, ...] = Field(
        default=(), alias="sourceArtifactIds", max_length=_MAX_REFERENCES
    )
    source_keys: tuple[str, ...] = Field(default=(), alias="sourceKeys", max_length=_MAX_VALUES)
    geography: GeographyReference

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        _require_sorted_unique_normalized("names", self.names)
        _require_sorted_unique_normalized("addresses", self.addresses)
        _require_sorted_unique_strings("phones", self.phones)
        _require_sorted_unique_strings("emails", self.emails)
        _require_sorted_unique_strings("website_urls", self.website_urls)
        _require_sorted_unique_uuids("observation_ids", self.observation_ids)
        _require_sorted_unique_uuids("source_artifact_ids", self.source_artifact_ids)
        _require_sorted_unique_strings("source_keys", self.source_keys)
        for value in self.phones:
            if _E164_PATTERN.fullmatch(value) is None:
                raise ValueError("phones must contain canonical E.164 values")
        for value in self.emails:
            if value != value.casefold() or _EMAIL_PATTERN.fullmatch(value) is None:
                raise ValueError("emails must contain normalized lower-case addresses")
        for value in self.website_urls:
            _require_canonical_http_url(value)
        for value in self.source_keys:
            _require_token("source_key", value)
        return self


class ManualResolutionDecisionPayload(_ContractModel):
    decision_id: UUID = Field(alias="decisionId")
    left_candidate_id: UUID = Field(alias="leftCandidateId")
    right_candidate_id: UUID = Field(alias="rightCandidateId")
    action: ManualDecisionAction
    revision: int = Field(ge=0)
    actor_reference: str = Field(alias="actorReference")
    reason_code: str = Field(alias="reasonCode")

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        _require_canonical_pair(self.left_candidate_id, self.right_candidate_id)
        _require_token("actor_reference", self.actor_reference)
        _require_code("reason_code", self.reason_code)
        return self


class ManualResolutionDecision(ManualResolutionDecisionPayload):
    decision_digest: str = Field(alias="decisionDigest")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        _require_digest("decision_digest", self.decision_digest)
        return self


class PriorClusterPayload(_ContractModel):
    cluster_id: UUID = Field(alias="clusterId")
    member_candidate_ids: tuple[UUID, ...] = Field(
        alias="memberCandidateIds", min_length=1, max_length=_MAX_CANDIDATES
    )

    @model_validator(mode="after")
    def validate_cluster(self) -> Self:
        _require_sorted_unique_uuids("member_candidate_ids", self.member_candidate_ids)
        if self.cluster_id != deterministic_cluster_id(self.member_candidate_ids):
            raise ValueError("prior cluster ID does not match deterministic membership")
        return self


class PriorCluster(PriorClusterPayload):
    content_digest: str = Field(alias="contentDigest")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        _require_digest("content_digest", self.content_digest)
        return self


class ResolutionThresholds(_ContractModel):
    candidate_limit: int = Field(alias="candidateLimit", ge=1, le=_MAX_CANDIDATES)
    pair_limit: int = Field(alias="pairLimit", ge=1, le=_MAX_PAIRS)
    name_review_minimum_bps: int = Field(alias="nameReviewMinimumBps", ge=0, le=10_000)
    address_review_minimum_bps: int = Field(alias="addressReviewMinimumBps", ge=0, le=10_000)
    website_auto_match_minimum_strong_features: int = Field(
        alias="websiteAutoMatchMinimumStrongFeatures", ge=2, le=4
    )
    address_auto_match_minimum_strong_features: int = Field(
        alias="addressAutoMatchMinimumStrongFeatures", ge=2, le=4
    )
    fuzzy_primary_area_review_reason_code: str = Field(alias="fuzzyPrimaryAreaReviewReasonCode")

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        _require_code(
            "fuzzy_primary_area_review_reason_code",
            self.fuzzy_primary_area_review_reason_code,
        )
        return self


class ClusterQualityRule(_ContractModel):
    entity_kind: EntityKind = Field(alias="entityKind")
    required_fields: tuple[CandidateField, ...] = Field(alias="requiredFields")
    single_value_fields: tuple[CandidateField, ...] = Field(alias="singleValueFields")
    minimum_distinct_source_count: int = Field(
        alias="minimumDistinctSourceCount", ge=1, le=_MAX_VALUES
    )
    allowed_geography: tuple[GeographyCoverage, ...] = Field(alias="allowedGeography", min_length=1)
    boundary_requires_review: bool = Field(alias="boundaryRequiresReview")
    pending_review_blocks_export: bool = Field(alias="pendingReviewBlocksExport")

    @model_validator(mode="after")
    def validate_rule(self) -> Self:
        _require_sorted_unique_enums("required_fields", self.required_fields)
        _require_sorted_unique_enums("single_value_fields", self.single_value_fields)
        _require_sorted_unique_enums("allowed_geography", self.allowed_geography)
        return self


class ResolutionBatchPayload(_ContractModel):
    contract: Literal["collector-entity-resolution-batch"] = "collector-entity-resolution-batch"
    contract_revision: Literal["entity-resolution-batch-v1"] = Field(
        default="entity-resolution-batch-v1", alias="contractRevision"
    )
    batch_id: UUID = Field(alias="batchId")
    market_area_revision: str = Field(alias="marketAreaRevision")
    market_area_digest: str = Field(alias="marketAreaDigest")
    market_area_readiness: MarketAreaReadiness = Field(alias="marketAreaReadiness")
    candidates: tuple[ResolutionCandidate, ...] = Field(min_length=1, max_length=_MAX_CANDIDATES)
    manual_decisions: tuple[ManualResolutionDecision, ...] = Field(
        default=(), alias="manualDecisions", max_length=_MAX_PAIRS
    )
    prior_clusters: tuple[PriorCluster, ...] = Field(
        default=(), alias="priorClusters", max_length=_MAX_CANDIDATES
    )
    thresholds: ResolutionThresholds
    quality_rules: tuple[ClusterQualityRule, ...] = Field(
        alias="qualityRules", max_length=len(EntityKind)
    )

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        _require_token("market_area_revision", self.market_area_revision)
        _require_digest("market_area_digest", self.market_area_digest)
        if len(self.candidates) > self.thresholds.candidate_limit:
            raise ValueError("candidate count exceeds the declared batch limit")
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        _require_sorted_unique_uuids("candidate identities", candidate_ids)
        candidate_id_set = set(candidate_ids)
        for candidate in self.candidates:
            geography = candidate.geography
            if (
                geography.market_area_revision != self.market_area_revision
                or geography.market_area_digest != self.market_area_digest
            ):
                raise ValueError("candidate geography does not match the batch market area")

        decision_pairs: list[tuple[UUID, UUID]] = []
        for decision in self.manual_decisions:
            if (
                decision.left_candidate_id not in candidate_id_set
                or decision.right_candidate_id not in candidate_id_set
            ):
                raise ValueError("manual decision references an unknown candidate")
            decision_pairs.append((decision.left_candidate_id, decision.right_candidate_id))
        if decision_pairs != sorted(decision_pairs, key=_uuid_pair_sort_key):
            raise ValueError("manual decisions must be sorted by canonical candidate pair")
        if len(set(decision_pairs)) != len(decision_pairs):
            raise ValueError("only one active manual decision is allowed per candidate pair")

        prior_ids = tuple(item.cluster_id for item in self.prior_clusters)
        _require_sorted_unique_uuids("prior cluster identities", prior_ids)
        prior_members: set[UUID] = set()
        for cluster in self.prior_clusters:
            unknown = set(cluster.member_candidate_ids).difference(candidate_id_set)
            if unknown:
                raise ValueError("prior cluster references an unknown candidate")
            overlap = prior_members.intersection(cluster.member_candidate_ids)
            if overlap:
                raise ValueError("prior clusters must not overlap")
            prior_members.update(cluster.member_candidate_ids)

        rule_kinds = tuple(item.entity_kind for item in self.quality_rules)
        _require_sorted_unique_enums("quality rule entity kinds", rule_kinds)
        return self


class ResolutionBatch(ResolutionBatchPayload):
    batch_digest: str = Field(alias="batchDigest")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        _require_digest("batch_digest", self.batch_digest)
        return self


class PairFeatures(_ContractModel):
    pair_id: str = Field(alias="pairId")
    left_candidate_id: UUID = Field(alias="leftCandidateId")
    right_candidate_id: UUID = Field(alias="rightCandidateId")
    exact_phone_overlap: bool = Field(alias="exactPhoneOverlap")
    exact_email_overlap: bool = Field(alias="exactEmailOverlap")
    exact_website_host_overlap: bool = Field(alias="exactWebsiteHostOverlap")
    name_similarity_bps: int = Field(alias="nameSimilarityBps", ge=0, le=10_000)
    address_similarity_bps: int = Field(alias="addressSimilarityBps", ge=0, le=10_000)
    entity_kind_compatible: bool = Field(alias="entityKindCompatible")
    geography_compatible: bool = Field(alias="geographyCompatible")
    source_overlap_count: int = Field(alias="sourceOverlapCount", ge=0, le=_MAX_VALUES)
    strong_non_name_feature_count: int = Field(alias="strongNonNameFeatureCount", ge=0, le=4)
    name_only: bool = Field(alias="nameOnly")

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        _require_digest("pair_id", self.pair_id)
        _require_canonical_pair(self.left_candidate_id, self.right_candidate_id)
        if self.pair_id != candidate_pair_id(self.left_candidate_id, self.right_candidate_id):
            raise ValueError("pair ID does not match canonical candidate identity")
        expected_strong_features = sum(
            (
                self.exact_phone_overlap,
                self.exact_email_overlap,
                self.exact_website_host_overlap,
                self.address_similarity_bps == 10_000,
            )
        )
        if self.strong_non_name_feature_count != expected_strong_features:
            raise ValueError("strong non-name feature count does not match pair features")
        expected_name_only = (
            self.name_similarity_bps > 0
            and expected_strong_features == 0
            and self.address_similarity_bps == 0
        )
        if self.name_only is not expected_name_only:
            raise ValueError("name-only marker does not match pair features")
        return self


class PairResolution(_ContractModel):
    features: PairFeatures
    disposition: MatchDisposition
    reason_codes: tuple[str, ...] = Field(alias="reasonCodes", min_length=1, max_length=16)
    evidence_strength: int = Field(alias="evidenceStrength", ge=0, le=100_000)

    @model_validator(mode="after")
    def validate_reasons(self) -> Self:
        _require_sorted_unique_strings("reason_codes", self.reason_codes)
        for code in self.reason_codes:
            _require_code("reason_code", code)
        return self


class BlockedMatchEdge(_ContractModel):
    blocked_edge_id: str = Field(alias="blockedEdgeId")
    attempted_pair_id: str = Field(alias="attemptedPairId")
    separation_pair_id: str = Field(alias="separationPairId")
    reason_code: Literal["EXPLICIT_SEPARATION_TRANSITIVE_BLOCK"] = Field(alias="reasonCode")

    @model_validator(mode="after")
    def validate_edge(self) -> Self:
        _require_digest("blocked_edge_id", self.blocked_edge_id)
        _require_digest("attempted_pair_id", self.attempted_pair_id)
        _require_digest("separation_pair_id", self.separation_pair_id)
        return self


class ResolvedCluster(_ContractModel):
    cluster_id: UUID = Field(alias="clusterId")
    member_candidate_ids: tuple[UUID, ...] = Field(
        alias="memberCandidateIds", min_length=1, max_length=_MAX_CANDIDATES
    )
    parent_cluster_ids: tuple[UUID, ...] = Field(
        default=(), alias="parentClusterIds", max_length=_MAX_CANDIDATES
    )
    lineage_kind: ClusterLineageKind = Field(alias="lineageKind")
    parent_membership_digest: str = Field(alias="parentMembershipDigest")
    blocked_match_edge_ids: tuple[str, ...] = Field(
        default=(), alias="blockedMatchEdgeIds", max_length=_MAX_PAIRS
    )

    @model_validator(mode="after")
    def validate_cluster(self) -> Self:
        _require_sorted_unique_uuids("member_candidate_ids", self.member_candidate_ids)
        if self.cluster_id != deterministic_cluster_id(self.member_candidate_ids):
            raise ValueError("cluster ID does not match deterministic membership")
        _require_sorted_unique_uuids("parent_cluster_ids", self.parent_cluster_ids)
        _require_digest("parent_membership_digest", self.parent_membership_digest)
        _require_sorted_unique_strings("blocked_match_edge_ids", self.blocked_match_edge_ids)
        for value in self.blocked_match_edge_ids:
            _require_digest("blocked_match_edge_id", value)
        return self


class ClusterQualityIssue(_ContractModel):
    code: str
    severity: QualitySeverity
    field: CandidateField | None = None
    related_pair_id: str | None = Field(default=None, alias="relatedPairId")
    related_blocked_edge_id: str | None = Field(default=None, alias="relatedBlockedEdgeId")

    @model_validator(mode="after")
    def validate_issue(self) -> Self:
        _require_code("quality issue code", self.code)
        if self.related_pair_id is not None:
            _require_digest("related_pair_id", self.related_pair_id)
        if self.related_blocked_edge_id is not None:
            _require_digest("related_blocked_edge_id", self.related_blocked_edge_id)
        if self.related_pair_id is not None and self.related_blocked_edge_id is not None:
            raise ValueError("quality issue cannot reference a pair and blocked edge together")
        return self


class ClusterQualityAssessment(_ContractModel):
    cluster_id: UUID = Field(alias="clusterId")
    export_eligible: bool = Field(alias="exportEligible")
    issues: tuple[ClusterQualityIssue, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_assessment(self) -> Self:
        issue_keys = tuple(
            (
                item.severity.value,
                item.code,
                item.field.value if item.field is not None else "",
                item.related_pair_id or "",
                item.related_blocked_edge_id or "",
            )
            for item in self.issues
        )
        if issue_keys != tuple(sorted(issue_keys)) or len(set(issue_keys)) != len(issue_keys):
            raise ValueError("quality issues must be sorted and unique")
        has_blocker = any(item.severity is QualitySeverity.BLOCKING for item in self.issues)
        if self.export_eligible == has_blocker:
            raise ValueError("export eligibility must be the inverse of blocking quality issues")
        return self


class ResolutionDiagnostic(_ContractModel):
    code: str
    count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_diagnostic(self) -> Self:
        _require_code("diagnostic code", self.code)
        return self


class ResolutionSnapshotPayload(_ContractModel):
    contract: Literal["collector-entity-resolution-snapshot"] = (
        "collector-entity-resolution-snapshot"
    )
    contract_revision: Literal["entity-resolution-snapshot-v1"] = Field(
        default="entity-resolution-snapshot-v1", alias="contractRevision"
    )
    batch_id: UUID = Field(alias="batchId")
    batch_digest: str = Field(alias="batchDigest")
    market_area_revision: str = Field(alias="marketAreaRevision")
    market_area_digest: str = Field(alias="marketAreaDigest")
    pair_resolutions: tuple[PairResolution, ...] = Field(
        alias="pairResolutions", max_length=_MAX_PAIRS
    )
    blocked_match_edges: tuple[BlockedMatchEdge, ...] = Field(
        alias="blockedMatchEdges", max_length=_MAX_PAIRS
    )
    clusters: tuple[ResolvedCluster, ...] = Field(max_length=_MAX_CANDIDATES)
    quality_assessments: tuple[ClusterQualityAssessment, ...] = Field(
        alias="qualityAssessments", max_length=_MAX_CANDIDATES
    )
    pending_review_pair_ids: tuple[str, ...] = Field(
        alias="pendingReviewPairIds", max_length=_MAX_PAIRS
    )
    diagnostics: tuple[ResolutionDiagnostic, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        _require_digest("batch_digest", self.batch_digest)
        _require_token("market_area_revision", self.market_area_revision)
        _require_digest("market_area_digest", self.market_area_digest)
        pair_ids = tuple(item.features.pair_id for item in self.pair_resolutions)
        _require_sorted_unique_strings("pair resolution identities", pair_ids)
        blocked_ids = tuple(item.blocked_edge_id for item in self.blocked_match_edges)
        _require_sorted_unique_strings("blocked edge identities", blocked_ids)
        cluster_ids = tuple(item.cluster_id for item in self.clusters)
        _require_sorted_unique_uuids("cluster identities", cluster_ids)
        assessment_ids = tuple(item.cluster_id for item in self.quality_assessments)
        if assessment_ids != cluster_ids:
            raise ValueError("quality assessments must cover each cluster in canonical order")
        _require_sorted_unique_strings("pending_review_pair_ids", self.pending_review_pair_ids)
        expected_pending = tuple(
            item.features.pair_id
            for item in self.pair_resolutions
            if item.disposition is MatchDisposition.REVIEW_REQUIRED
        )
        if self.pending_review_pair_ids != expected_pending:
            raise ValueError("pending review identities must exactly cover review dispositions")

        pairs = {item.features.pair_id: item for item in self.pair_resolutions}
        for edge in self.blocked_match_edges:
            attempted = pairs.get(edge.attempted_pair_id)
            separation = pairs.get(edge.separation_pair_id)
            if attempted is None or separation is None:
                raise ValueError("blocked match edge must reference emitted pair resolutions")
            if attempted.disposition not in {
                MatchDisposition.AUTO_MATCH,
                MatchDisposition.MANUAL_MATCH,
            }:
                raise ValueError("blocked match edge must reference an accepted attempted pair")
            if separation.disposition is not MatchDisposition.MANUAL_SEPARATE:
                raise ValueError("blocked match edge must reference a manual separation pair")

        seen_members: set[UUID] = set()
        referenced_blocked_edges: set[str] = set()
        for cluster in self.clusters:
            overlap = seen_members.intersection(cluster.member_candidate_ids)
            if overlap:
                raise ValueError("resolved clusters must not share candidate membership")
            seen_members.update(cluster.member_candidate_ids)
            unknown_edges = set(cluster.blocked_match_edge_ids).difference(blocked_ids)
            if unknown_edges:
                raise ValueError("cluster references an unknown blocked match edge")
            referenced_blocked_edges.update(cluster.blocked_match_edge_ids)
        if referenced_blocked_edges != set(blocked_ids):
            raise ValueError("each blocked match edge must be attached to an affected cluster")

        diagnostic_codes = tuple(item.code for item in self.diagnostics)
        _require_sorted_unique_strings("diagnostic codes", diagnostic_codes)
        return self


class ResolutionSnapshot(ResolutionSnapshotPayload):
    snapshot_digest: str = Field(alias="snapshotDigest")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        _require_digest("snapshot_digest", self.snapshot_digest)
        return self


def _require_digest(name: str, value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be canonical SHA-256")


def _require_token(name: str, value: str) -> None:
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid token format")


def _require_code(name: str, value: str) -> None:
    if _CODE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid code format")


def _require_canonical_pair(left: UUID, right: UUID) -> None:
    if left == right or left.hex >= right.hex:
        raise ValueError("candidate pair must contain two distinct canonically ordered IDs")


def _uuid_pair_sort_key(value: tuple[UUID, UUID]) -> tuple[str, str]:
    return value[0].hex, value[1].hex


def _require_sorted_unique_uuids(name: str, values: tuple[UUID, ...]) -> None:
    if values != tuple(sorted(set(values), key=lambda value: value.hex)):
        raise ValueError(f"{name} must be sorted and unique")


def _require_sorted_unique_strings(name: str, values: tuple[str, ...]) -> None:
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{name} must be sorted and unique")
    if any(not value or value != value.strip() for value in values):
        raise ValueError(f"{name} contains an empty or non-canonical string")


def _require_sorted_unique_normalized(name: str, values: tuple[str, ...]) -> None:
    _require_sorted_unique_strings(name, values)
    for value in values:
        if value != unicodedata.normalize("NFKC", value).casefold():
            raise ValueError(f"{name} must contain NFKC case-folded values")


def _require_sorted_unique_enums(name: str, values: tuple[StrEnum, ...]) -> None:
    serialized = tuple(item.value for item in values)
    if serialized != tuple(sorted(set(serialized))):
        raise ValueError(f"{name} must be sorted and unique")


def _require_canonical_http_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("website URL must be an absolute HTTP(S) URL")
    if parsed.fragment:
        raise ValueError("website URL must not contain a fragment")
    if parsed.scheme != parsed.scheme.casefold() or parsed.hostname != parsed.hostname.casefold():
        raise ValueError("website URL scheme and host must be lower-case")
    if (parsed.scheme == "http" and parsed.port == 80) or (
        parsed.scheme == "https" and parsed.port == 443
    ):
        raise ValueError("website URL must not retain a default port")
