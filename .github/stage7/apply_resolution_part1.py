from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(content).lstrip(), encoding="utf-8")


def insert_toml_list_item(text: str, *, section: str, key: str, value: str) -> str:
    if f'"{value}"' in text:
        return text
    section_start = text.index(section)
    list_start = text.index(f"{key} = [", section_start)
    list_end = text.index("\n]", list_start)
    return text[:list_end] + f'\n  "{value}",' + text[list_end:]


def transformed(path: str, replacements: tuple[tuple[str, str], ...]) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    for old, new in replacements:
        text = text.replace(old, new)
    return text


workspace_path = ROOT / "pyproject.toml"
workspace = workspace_path.read_text(encoding="utf-8")
for member in (
    "apps/resolution_worker",
    "packages/entity_resolution_core",
    "packages/quality_core",
    "packages/resolution_contracts",
):
    workspace = insert_toml_list_item(
        workspace,
        section="[tool.uv.workspace]",
        key="members",
        value=member,
    )
for source in (
    "apps/resolution_worker/src/resolution_worker",
    "packages/entity_resolution_core/src/entity_resolution_core",
    "packages/quality_core/src/quality_core",
    "packages/resolution_contracts/src/resolution_contracts",
):
    workspace = insert_toml_list_item(
        workspace,
        section="[tool.mypy]",
        key="files",
        value=source,
    )
workspace_path.write_text(workspace, encoding="utf-8")

contracts_pyproject = transformed(
    "packages/observation_contracts/pyproject.toml",
    (
        ("observation-contracts", "resolution-contracts"),
        ("observation_contracts", "resolution_contracts"),
        ("Observation contracts", "Resolution contracts"),
    ),
)
write("packages/resolution_contracts/pyproject.toml", contracts_pyproject)

core_template = transformed(
    "packages/manual_import_core/pyproject.toml",
    (
        ("manual-import-core", "entity-resolution-core"),
        ("manual_import_core", "entity_resolution_core"),
        ("Manual import", "Entity resolution"),
    ),
)
core_template, count = re.subn(
    r"dependencies = \[[^\n]*\]",
    'dependencies = ["resolution-contracts"]',
    core_template,
    count=1,
)
if count != 1:
    raise RuntimeError("entity_resolution_core dependency list was not found")
write("packages/entity_resolution_core/pyproject.toml", core_template)

quality_template = transformed(
    "packages/manual_import_core/pyproject.toml",
    (
        ("manual-import-core", "quality-core"),
        ("manual_import_core", "quality_core"),
        ("Manual import", "Resolution quality"),
    ),
)
quality_template, count = re.subn(
    r"dependencies = \[[^\n]*\]",
    'dependencies = ["resolution-contracts"]',
    quality_template,
    count=1,
)
if count != 1:
    raise RuntimeError("quality_core dependency list was not found")
write("packages/quality_core/pyproject.toml", quality_template)

worker_pyproject = transformed(
    "apps/extraction_worker/pyproject.toml",
    (
        ("extraction-worker", "resolution-worker"),
        ("extraction_worker", "resolution_worker"),
        ("Extraction", "Resolution"),
    ),
)
worker_pyproject, count = re.subn(
    r"dependencies = \[\n.*?\n\]",
    """dependencies = [
  "entity-resolution-core",
  "pydantic==2.13.4",
  "quality-core",
  "resolution-contracts",
  "source-connector-sdk",
]""",
    worker_pyproject,
    count=1,
    flags=re.S,
)
if count != 1:
    raise RuntimeError("resolution_worker dependency list was not found")
write("apps/resolution_worker/pyproject.toml", worker_pyproject)

worker_contracts = transformed(
    "apps/extraction_worker/src/extraction_worker/contracts.py",
    (
        ("extraction_worker", "resolution_worker"),
        ("Extraction", "Resolution"),
        ("EXTRACTION", "RESOLUTION"),
        ("extraction", "resolution"),
    ),
)
write("apps/resolution_worker/src/resolution_worker/contracts.py", worker_contracts)

app_text = transformed(
    "apps/extraction_worker/src/extraction_worker/app.py",
    (
        ("extraction_core", "entity_resolution_core"),
        ("ExtractionEngine", "EntityResolutionEngine"),
        ("extraction_worker", "resolution_worker"),
        ("SdkExtractionWorkerGateway", "SdkResolutionWorkerGateway"),
        ("ExtractionWorker", "ResolutionWorker"),
        ("Extraction", "Resolution"),
        ("EXTRACTION", "RESOLUTION"),
        ("extraction", "resolution"),
    ),
)
app_text = re.sub(
    r"from entity_resolution_core import \([^)]*\)",
    "from entity_resolution_core import EntityResolutionEngine",
    app_text,
    flags=re.S,
)
app_text = re.sub(
    r"from entity_resolution_core import [^\n]+",
    "from entity_resolution_core import EntityResolutionEngine",
    app_text,
    count=1,
)
write("apps/resolution_worker/src/resolution_worker/app.py", app_text)

write(
    "packages/resolution_contracts/src/resolution_contracts/contracts.py",
    r'''
    from __future__ import annotations

    import json
    import re
    from hashlib import sha256
    from typing import Literal, Self
    from urllib.parse import urlsplit
    from uuid import UUID, uuid5

    from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

    GeographyCoverage = Literal["inside", "boundary", "outside", "unknown"]
    MarketAreaReadiness = Literal["ready", "blocked"]
    DecisionAction = Literal["match", "separate"]
    PairDisposition = Literal[
        "auto_match",
        "manual_match",
        "review_required",
        "no_match",
        "manual_separate",
    ]
    ClusterLineageKind = Literal["new", "unchanged", "split", "merge", "recombined"]

    _PAIR_NAMESPACE = UUID("73f7d730-a79d-5c0c-b4aa-a77f560b4ac8")
    _CLUSTER_NAMESPACE = UUID("1ed23c15-8458-55f2-a40b-7d37793933df")
    _CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
    _FIELD = re.compile(r"^[a-z][a-z0-9_]{0,99}$")


    class MarketAreaIdentity(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        revision: str = Field(min_length=1, max_length=200)
        digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
        readiness: MarketAreaReadiness
        blocker_codes: tuple[str, ...] = Field(alias="blockerCodes")

        @field_validator("blocker_codes", mode="before")
        @classmethod
        def _codes(cls, value: object) -> tuple[str, ...]:
            return _canonical_codes(value)

        @model_validator(mode="after")
        def _readiness_shape(self) -> Self:
            if self.readiness == "ready" and self.blocker_codes:
                raise ValueError("ready market area cannot contain blockers")
            if self.readiness == "blocked" and not self.blocker_codes:
                raise ValueError("blocked market area requires blocker codes")
            return self


    class CandidateGeography(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        coverage: GeographyCoverage
        market_area_revision: str = Field(alias="marketAreaRevision", min_length=1, max_length=200)
        market_area_digest: str = Field(
            alias="marketAreaDigest",
            pattern=r"^sha256:[0-9a-f]{64}$",
        )
        latitude: float | None = Field(default=None, ge=-90, le=90)
        longitude: float | None = Field(default=None, ge=-180, le=180)
        evidence_observation_ids: tuple[UUID, ...] = Field(alias="evidenceObservationIds")

        @field_validator("evidence_observation_ids", mode="before")
        @classmethod
        def _evidence_ids(cls, value: object) -> tuple[UUID, ...]:
            return _canonical_uuids(value)

        @model_validator(mode="after")
        def _coverage_shape(self) -> Self:
            has_point = self.latitude is not None or self.longitude is not None
            if self.coverage == "unknown":
                if has_point or self.evidence_observation_ids:
                    raise ValueError("unknown geography cannot invent a classified point")
            elif (
                self.latitude is None
                or self.longitude is None
                or not self.evidence_observation_ids
            ):
                raise ValueError("classified geography requires a point and evidence")
            return self


    class CandidateRecord(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        candidate_id: UUID = Field(alias="candidateId")
        entity_kind: str = Field(alias="entityKind", pattern=r"^[a-z][a-z0-9_]{0,99}$")
        names: tuple[str, ...]
        phones: tuple[str, ...]
        emails: tuple[str, ...]
        websites: tuple[str, ...]
        addresses: tuple[str, ...]
        observation_ids: tuple[UUID, ...] = Field(alias="observationIds")
        source_artifact_ids: tuple[str, ...] = Field(alias="sourceArtifactIds")
        source_keys: tuple[str, ...] = Field(alias="sourceKeys")
        geography: CandidateGeography

        @field_validator(
            "names",
            "phones",
            "emails",
            "websites",
            "addresses",
            "source_artifact_ids",
            "source_keys",
            mode="before",
        )
        @classmethod
        def _strings(cls, value: object) -> tuple[str, ...]:
            return _canonical_strings(value)

        @field_validator("observation_ids", mode="before")
        @classmethod
        def _observations(cls, value: object) -> tuple[UUID, ...]:
            return _canonical_uuids(value)


    class ResolutionDecision(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        decision_id: UUID = Field(alias="decisionId")
        left_candidate_id: UUID = Field(alias="leftCandidateId")
        right_candidate_id: UUID = Field(alias="rightCandidateId")
        action: DecisionAction
        revision: int = Field(ge=0)
        actor_reference: str = Field(alias="actorReference", min_length=1, max_length=200)
        reason_code: str = Field(alias="reasonCode", pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
        decision_digest: str = Field(
            alias="decisionDigest",
            pattern=r"^sha256:[0-9a-f]{64}$",
        )

        @model_validator(mode="after")
        def _validate_decision(self) -> Self:
            if self.left_candidate_id >= self.right_candidate_id:
                raise ValueError("decision candidate pair must be canonical")
            if self.decision_digest != self.computed_digest():
                raise ValueError("decision digest does not match canonical decision")
            return self

        def computed_digest(self) -> str:
            return _digest(
                {
                    "action": self.action,
                    "actorReference": self.actor_reference,
                    "decisionId": str(self.decision_id),
                    "leftCandidateId": str(self.left_candidate_id),
                    "reasonCode": self.reason_code,
                    "revision": self.revision,
                    "rightCandidateId": str(self.right_candidate_id),
                }
            )

        @classmethod
        def create(
            cls,
            *,
            decision_id: UUID,
            left_candidate_id: UUID,
            right_candidate_id: UUID,
            action: DecisionAction,
            revision: int,
            actor_reference: str,
            reason_code: str,
        ) -> Self:
            left, right = canonical_pair(left_candidate_id, right_candidate_id)
            provisional = {
                "decisionId": decision_id,
                "leftCandidateId": left,
                "rightCandidateId": right,
                "action": action,
                "revision": revision,
                "actorReference": actor_reference,
                "reasonCode": reason_code,
            }
            digest = _digest(
                {
                    "action": action,
                    "actorReference": actor_reference,
                    "decisionId": str(decision_id),
                    "leftCandidateId": str(left),
                    "reasonCode": reason_code,
                    "revision": revision,
                    "rightCandidateId": str(right),
                }
            )
            return cls.model_validate({**provisional, "decisionDigest": digest})


    class PriorCluster(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        cluster_id: UUID = Field(alias="clusterId")
        member_candidate_ids: tuple[UUID, ...] = Field(
            alias="memberCandidateIds",
            min_length=1,
        )

        @field_validator("member_candidate_ids", mode="before")
        @classmethod
        def _members(cls, value: object) -> tuple[UUID, ...]:
            return _canonical_uuids(value)

        @model_validator(mode="after")
        def _identity(self) -> Self:
            if self.cluster_id != deterministic_cluster_id(self.member_candidate_ids):
                raise ValueError("prior cluster ID does not match membership")
            return self


    class ResolutionThresholds(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        name_review_basis_points: int = Field(alias="nameReviewBasisPoints", ge=0, le=10000)
        address_review_basis_points: int = Field(
            alias="addressReviewBasisPoints",
            ge=0,
            le=10000,
        )
        website_name_auto_basis_points: int = Field(
            alias="websiteNameAutoBasisPoints",
            ge=0,
            le=10000,
        )
        website_address_auto_basis_points: int = Field(
            alias="websiteAddressAutoBasisPoints",
            ge=0,
            le=10000,
        )
        address_name_auto_basis_points: int = Field(
            alias="addressNameAutoBasisPoints",
            ge=0,
            le=10000,
        )
        maximum_candidates: int = Field(alias="maximumCandidates", ge=1, le=5000)
        maximum_pairs: int = Field(alias="maximumPairs", ge=1, le=250000)


    class EntityQualityRule(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        entity_kind: str = Field(alias="entityKind", pattern=r"^[a-z][a-z0-9_]{0,99}$")
        required_fields: tuple[str, ...] = Field(alias="requiredFields")
        single_value_fields: tuple[str, ...] = Field(alias="singleValueFields")
        minimum_source_count: int = Field(alias="minimumSourceCount", ge=1, le=1000)
        allowed_geography: tuple[GeographyCoverage, ...] = Field(alias="allowedGeography")
        boundary_requires_review: bool = Field(alias="boundaryRequiresReview")
        pending_review_blocks_export: bool = Field(alias="pendingReviewBlocksExport")

        @field_validator("required_fields", "single_value_fields", mode="before")
        @classmethod
        def _fields(cls, value: object) -> tuple[str, ...]:
            fields = _canonical_strings(value)
            if any(_FIELD.fullmatch(item) is None for item in fields):
                raise ValueError("quality rule contains an invalid field key")
            return fields

        @field_validator("allowed_geography", mode="before")
        @classmethod
        def _coverage(cls, value: object) -> tuple[str, ...]:
            return tuple(sorted(set(str(item) for item in _as_sequence(value))))


    class ResolutionPolicy(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        thresholds: ResolutionThresholds
        quality_rules: tuple[EntityQualityRule, ...] = Field(alias="qualityRules")

        @model_validator(mode="after")
        def _rules(self) -> Self:
            kinds = tuple(item.entity_kind for item in self.quality_rules)
            if tuple(sorted(set(kinds))) != kinds:
                raise ValueError("quality rules must be unique and sorted by entity kind")
            return self


    class EntityResolutionBatch(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        contract: Literal["entity-resolution-batch"]
        contract_revision: Literal["entity-resolution-batch-v1"] = Field(
            alias="contractRevision"
        )
        batch_id: UUID = Field(alias="batchId")
        campaign_key: str = Field(alias="campaignKey", pattern=r"^[a-z][a-z0-9_]{0,199}$")
        market_area: MarketAreaIdentity = Field(alias="marketArea")
        candidates: tuple[CandidateRecord, ...] = Field(min_length=1)
        decisions: tuple[ResolutionDecision, ...]
        prior_clusters: tuple[PriorCluster, ...] = Field(alias="priorClusters")
        policy: ResolutionPolicy

        @model_validator(mode="after")
        def _validate_batch(self) -> Self:
            if self.market_area.readiness != "ready":
                raise ValueError("market area is not ready for entity resolution")
            if len(self.candidates) > self.policy.thresholds.maximum_candidates:
                raise ValueError("candidate count exceeds the resolution limit")
            candidate_ids = tuple(item.candidate_id for item in self.candidates)
            if tuple(sorted(set(candidate_ids))) != candidate_ids:
                raise ValueError("candidates must be unique and sorted")
            candidate_set = set(candidate_ids)
            for candidate in self.candidates:
                geography = candidate.geography
                if (
                    geography.market_area_revision != self.market_area.revision
                    or geography.market_area_digest != self.market_area.digest
                ):
                    raise ValueError("candidate geography identity does not match the batch")
            decision_pairs: list[tuple[UUID, UUID]] = []
            decision_ids: list[UUID] = []
            for decision in self.decisions:
                pair = (decision.left_candidate_id, decision.right_candidate_id)
                if not set(pair) <= candidate_set:
                    raise ValueError("decision references a candidate outside the batch")
                decision_pairs.append(pair)
                decision_ids.append(decision.decision_id)
            if len(set(decision_pairs)) != len(decision_pairs):
                raise ValueError("a batch may contain only one decision per pair")
            if len(set(decision_ids)) != len(decision_ids):
                raise ValueError("decision IDs must be unique")
            if tuple(sorted(self.decisions, key=_decision_sort_key)) != self.decisions:
                raise ValueError("decisions must be sorted")
            seen_prior_members: set[UUID] = set()
            prior_ids: list[UUID] = []
            for cluster in self.prior_clusters:
                if not set(cluster.member_candidate_ids) <= candidate_set:
                    raise ValueError("prior cluster references a candidate outside the batch")
                if seen_prior_members & set(cluster.member_candidate_ids):
                    raise ValueError("candidate cannot belong to multiple prior clusters")
                seen_prior_members.update(cluster.member_candidate_ids)
                prior_ids.append(cluster.cluster_id)
            if tuple(sorted(set(prior_ids))) != tuple(prior_ids):
                raise ValueError("prior clusters must be unique and sorted")
            return self

        @classmethod
        def from_bytes(cls, value: bytes) -> Self:
            if len(value) > 10 * 1024 * 1024:
                raise ValueError("resolution batch exceeds the configured limit")
            payload = json.loads(value.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("resolution batch must be a JSON object")
            return cls.model_validate(payload)

        def canonical_bytes(self) -> bytes:
            return _canonical_bytes(self.model_dump(by_alias=True, exclude_none=True, mode="json"))

        def digest(self) -> str:
            return f"sha256:{sha256(self.canonical_bytes()).hexdigest()}"


    class PairFeatures(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        exact_phone: bool = Field(alias="exactPhone")
        exact_email: bool = Field(alias="exactEmail")
        exact_website_host: bool = Field(alias="exactWebsiteHost")
        exact_address: bool = Field(alias="exactAddress")
        name_similarity_basis_points: int = Field(
            alias="nameSimilarityBasisPoints",
            ge=0,
            le=10000,
        )
        address_similarity_basis_points: int = Field(
            alias="addressSimilarityBasisPoints",
            ge=0,
            le=10000,
        )
        compatible_entity_kind: bool = Field(alias="compatibleEntityKind")
        both_market_covered: bool = Field(alias="bothMarketCovered")
        shared_source: bool = Field(alias="sharedSource")
        strong_non_name_feature_count: int = Field(
            alias="strongNonNameFeatureCount",
            ge=0,
            le=4,
        )
        name_only: bool = Field(alias="nameOnly")


    class PairResolution(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        pair_id: UUID = Field(alias="pairId")
        left_candidate_id: UUID = Field(alias="leftCandidateId")
        right_candidate_id: UUID = Field(alias="rightCandidateId")
        disposition: PairDisposition
        reason_codes: tuple[str, ...] = Field(alias="reasonCodes")
        features: PairFeatures

        @field_validator("reason_codes", mode="before")
        @classmethod
        def _reasons(cls, value: object) -> tuple[str, ...]:
            return _canonical_codes(value)

        @model_validator(mode="after")
        def _identity(self) -> Self:
            if self.left_candidate_id >= self.right_candidate_id:
                raise ValueError("pair candidate IDs must be canonical")
            if self.pair_id != deterministic_pair_id(
                self.left_candidate_id,
                self.right_candidate_id,
            ):
                raise ValueError("pair ID does not match candidate pair")
            if self.disposition == "auto_match" and self.features.name_only:
                raise ValueError("name-only pair cannot auto-match")
            return self


    class ClusterLineage(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        cluster_id: UUID = Field(alias="clusterId")
        member_candidate_ids: tuple[UUID, ...] = Field(
            alias="memberCandidateIds",
            min_length=1,
        )
        parent_cluster_ids: tuple[UUID, ...] = Field(alias="parentClusterIds")
        lineage_kind: ClusterLineageKind = Field(alias="lineageKind")
        parent_members_digest: str = Field(
            alias="parentMembersDigest",
            pattern=r"^sha256:[0-9a-f]{64}$",
        )
        blocked_match_pair_ids: tuple[UUID, ...] = Field(alias="blockedMatchPairIds")

        @field_validator(
            "member_candidate_ids",
            "parent_cluster_ids",
            "blocked_match_pair_ids",
            mode="before",
        )
        @classmethod
        def _ids(cls, value: object) -> tuple[UUID, ...]:
            return _canonical_uuids(value)

        @model_validator(mode="after")
        def _identity(self) -> Self:
            if self.cluster_id != deterministic_cluster_id(self.member_candidate_ids):
                raise ValueError("cluster ID does not match membership")
            return self


    class ResolutionDiagnostic(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
        pair_id: UUID | None = Field(default=None, alias="pairId")
        cluster_id: UUID | None = Field(default=None, alias="clusterId")


    class ResolutionGraph(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        pairs: tuple[PairResolution, ...]
        blocked_match_pair_ids: tuple[UUID, ...] = Field(alias="blockedMatchPairIds")
        clusters: tuple[ClusterLineage, ...]
        pending_review_pair_ids: tuple[UUID, ...] = Field(alias="pendingReviewPairIds")
        diagnostics: tuple[ResolutionDiagnostic, ...]

        @model_validator(mode="after")
        def _canonical_order(self) -> Self:
            if tuple(sorted(self.pairs, key=lambda item: item.pair_id)) != self.pairs:
                raise ValueError("pair resolutions must be sorted")
            if tuple(sorted(self.clusters, key=lambda item: item.cluster_id)) != self.clusters:
                raise ValueError("clusters must be sorted")
            if tuple(sorted(set(self.blocked_match_pair_ids))) != self.blocked_match_pair_ids:
                raise ValueError("blocked match pair IDs must be unique and sorted")
            if tuple(sorted(set(self.pending_review_pair_ids))) != self.pending_review_pair_ids:
                raise ValueError("pending review pair IDs must be unique and sorted")
            return self


    class FieldCoverage(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        field_key: str = Field(alias="fieldKey", pattern=r"^[a-z][a-z0-9_]{0,99}$")
        distinct_value_count: int = Field(alias="distinctValueCount", ge=0)
        present: bool


    class ClusterQuality(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        cluster_id: UUID = Field(alias="clusterId")
        export_eligible: bool = Field(alias="exportEligible")
        blocker_codes: tuple[str, ...] = Field(alias="blockerCodes")
        warning_codes: tuple[str, ...] = Field(alias="warningCodes")
        field_coverage: tuple[FieldCoverage, ...] = Field(alias="fieldCoverage")

        @field_validator("blocker_codes", "warning_codes", mode="before")
        @classmethod
        def _codes(cls, value: object) -> tuple[str, ...]:
            return _canonical_codes(value)

        @model_validator(mode="after")
        def _eligibility(self) -> Self:
            if self.export_eligible == bool(self.blocker_codes):
                raise ValueError("export eligibility must be the inverse of blockers")
            fields = tuple(item.field_key for item in self.field_coverage)
            if tuple(sorted(set(fields))) != fields:
                raise ValueError("field coverage must be unique and sorted")
            return self


    class ResolutionSnapshot(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

        contract: Literal["entity-resolution-snapshot"]
        contract_revision: Literal["entity-resolution-snapshot-v1"] = Field(
            alias="contractRevision"
        )
        batch_id: UUID = Field(alias="batchId")
        batch_digest: str = Field(alias="batchDigest", pattern=r"^sha256:[0-9a-f]{64}$")
        campaign_key: str = Field(alias="campaignKey")
        market_area_revision: str = Field(alias="marketAreaRevision")
        market_area_digest: str = Field(
            alias="marketAreaDigest",
            pattern=r"^sha256:[0-9a-f]{64}$",
        )
        candidate_count: int = Field(alias="candidateCount", ge=1)
        graph: ResolutionGraph
        quality: tuple[ClusterQuality, ...]

        @model_validator(mode="after")
        def _quality_coverage(self) -> Self:
            cluster_ids = tuple(item.cluster_id for item in self.graph.clusters)
            quality_ids = tuple(item.cluster_id for item in self.quality)
            if quality_ids != cluster_ids:
                raise ValueError("quality must cover clusters in canonical order")
            return self

        def canonical_bytes(self) -> bytes:
            return _canonical_bytes(self.model_dump(by_alias=True, exclude_none=True, mode="json"))

        def digest(self) -> str:
            return f"sha256:{sha256(self.canonical_bytes()).hexdigest()}"


    def canonical_pair(left: UUID, right: UUID) -> tuple[UUID, UUID]:
        if left == right:
            raise ValueError("candidate pair cannot reference the same candidate")
        return (left, right) if left < right else (right, left)


    def deterministic_pair_id(left: UUID, right: UUID) -> UUID:
        first, second = canonical_pair(left, right)
        return uuid5(_PAIR_NAMESPACE, f"{first}|{second}")


    def deterministic_cluster_id(members: tuple[UUID, ...]) -> UUID:
        canonical = tuple(sorted(set(members)))
        if not canonical:
            raise ValueError("cluster membership cannot be empty")
        return uuid5(_CLUSTER_NAMESPACE, "|".join(str(item) for item in canonical))


    def membership_digest(clusters: tuple[PriorCluster, ...]) -> str:
        return _digest(
            [
                {
                    "clusterId": str(cluster.cluster_id),
                    "memberCandidateIds": [
                        str(item) for item in cluster.member_candidate_ids
                    ],
                }
                for cluster in sorted(clusters, key=lambda item: item.cluster_id)
            ]
        )


    def _decision_sort_key(decision: ResolutionDecision) -> tuple[UUID, UUID, UUID]:
        return (
            decision.left_candidate_id,
            decision.right_candidate_id,
            decision.decision_id,
        )


    def _canonical_strings(value: object) -> tuple[str, ...]:
        values = tuple(str(item).strip() for item in _as_sequence(value))
        return tuple(sorted({item for item in values if item}))


    def _canonical_uuids(value: object) -> tuple[UUID, ...]:
        return tuple(sorted({UUID(str(item)) for item in _as_sequence(value)}))


    def _canonical_codes(value: object) -> tuple[str, ...]:
        codes = tuple(sorted({str(item) for item in _as_sequence(value)}))
        if any(_CODE.fullmatch(item) is None for item in codes):
            raise ValueError("collection contains an invalid result code")
        return codes


    def _as_sequence(value: object) -> tuple[object, ...]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("value must be an array")
        return tuple(value)


    def _digest(value: object) -> str:
        return f"sha256:{sha256(_canonical_bytes(value)).hexdigest()}"


    def _canonical_bytes(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ''',
)

write(
    "packages/resolution_contracts/src/resolution_contracts/__init__.py",
    r'''
    from resolution_contracts.contracts import (
        CandidateGeography,
        CandidateRecord,
        ClusterLineage,
        ClusterLineageKind,
        ClusterQuality,
        DecisionAction,
        EntityQualityRule,
        EntityResolutionBatch,
        FieldCoverage,
        GeographyCoverage,
        MarketAreaIdentity,
        PairDisposition,
        PairFeatures,
        PairResolution,
        PriorCluster,
        ResolutionDecision,
        ResolutionDiagnostic,
        ResolutionGraph,
        ResolutionPolicy,
        ResolutionSnapshot,
        ResolutionThresholds,
        canonical_pair,
        deterministic_cluster_id,
        deterministic_pair_id,
        membership_digest,
    )

    __all__ = [
        "CandidateGeography",
        "CandidateRecord",
        "ClusterLineage",
        "ClusterLineageKind",
        "ClusterQuality",
        "DecisionAction",
        "EntityQualityRule",
        "EntityResolutionBatch",
        "FieldCoverage",
        "GeographyCoverage",
        "MarketAreaIdentity",
        "PairDisposition",
        "PairFeatures",
        "PairResolution",
        "PriorCluster",
        "ResolutionDecision",
        "ResolutionDiagnostic",
        "ResolutionGraph",
        "ResolutionPolicy",
        "ResolutionSnapshot",
        "ResolutionThresholds",
        "canonical_pair",
        "deterministic_cluster_id",
        "deterministic_pair_id",
        "membership_digest",
    ]
    ''',
)
