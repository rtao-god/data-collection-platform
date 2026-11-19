from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, NamedTuple, Protocol, TypeVar, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from collection_application.canonicalization import sha256_json
from collection_application.geography import decode_boundary_geojson
from collection_contracts import (
    ExtractedRecord,
    ExtractionRequest,
    ManualRecordLineage,
    NormalizationProfile,
    NormalizationProfilesDocument,
    OsmSourceRecord,
    WebsiteRouteContext,
    canonical_extracted_record_json,
    canonical_manual_record_lineage_bytes,
    canonical_osm_source_record_json,
    decode_extracted_record,
    decode_osm_envelope,
    decode_source_record_observation_batch,
    owner_error,
)
from collection_contracts.campaign_config import (
    CampaignDocument,
    GeographyDocument,
    NetworkAccess,
    SeedRoutingConfigured,
    SourceBinding,
    SourceBindingsDocument,
    SourcePolicy,
    UserAgentConfigured,
)
from collection_contracts.osm_query_spec import boundary_contains
from collection_contracts.website_route import decode_website_route_context
from collection_contracts.wikidata_entity_query_spec import (
    decode_wikidata_query_spec,
)
from collection_contracts.wikidata_entity_result import RESULT_CONTRACT as _WIKIDATA_RESULT_CONTRACT
from collection_contracts.wikidata_entity_result import (
    WikidataAdaptedRecordEntry,
    decode_wikidata_entity_result,
)
from collection_domain import WorkCapability, WorkStage
from manual_import_core import decode_canonical_manual_import_record
from official_http_contracts import HttpAcquisitionManifest
from resolution_contracts import (
    decode_resolution_batch,
    decode_resolution_snapshot,
)

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,199}$")
_MANUAL_SOURCE_ROLES = (
    re.compile(r"^manual_source:(csv|json|jsonl):(atomic|partial)$"),
    re.compile(r"^manual_import_source:(csv|json|jsonl):(atomic|partial)$"),
)
_WEBSITE_ROUTE_NAMESPACE = UUID("01dd1c8e-4ee6-5f1c-a93b-b5fa058370e5")
_NORMALIZATION_WORK_NAMESPACE = UUID("1f315e17-25b0-5a91-81a3-0bb93e3a1a1e")
_PAGE_EXTRACTION_TRANSITION_KEY = "official-http-page-extraction-v1"
_NORMALIZATION_TRANSITION_KEY = "extracted-record-normalization-v1"
_CANDIDATE_INTAKE_TRANSITION_KEY = "field-observation-batch-candidate-intake-v1"
_CANDIDATE_INTAKE_OUTPUT_CONTRACT = "field-observation-batch@1"
_OSM_RESULT_TRANSITION_KEY = "osm-overpass-result-source-record-mapping-v1"
_OSM_RESULT_OUTPUT_CONTRACT = "osm-overpass-result@1"
_SOURCE_RECORD_MAPPING_TRANSITION_KEY = "source-record-mapping-observation-batch-v1"
_SOURCE_RECORD_INTAKE_TRANSITION_KEY = "source-record-observation-batch-candidate-intake-v1"
_OSM_MAX_MAPPING_UNITS = 5_000
_SOURCE_RECORD_ARTIFACT_NAMESPACE = UUID("6f4d2b1e-8a3c-5b7d-9e2f-1c3a5b7d9e2f")
_SOURCE_RECORD_MAPPING_OUTPUT_CONTRACT = "source-record-observation-batch@1"
_SOURCE_RECORD_MAPPING_WORK_NAMESPACE = UUID("7c1f9b44-2d0a-5c63-9e18-4a7d3b6f0c25")
_WIKIDATA_RESULT_TRANSITION_KEY = "wikidata-entity-result-source-record-mapping-v1"
_WIKIDATA_RESULT_OUTPUT_CONTRACT = _WIKIDATA_RESULT_CONTRACT
_WIKIDATA_MAPPING_REVISION = "wikidata-entity-source-record-mapping-v1"
_WIKIDATA_RAW_ROLE = "wikidata_raw_response"
_WIKIDATA_QUERY_ROLE = "wikidata_entity_query_spec"
# The declared set is bounded at 50 entities, so a plan can never fan out past the same
# ceiling the connector contract enforces.
_WIKIDATA_MAX_MAPPING_UNITS = 50
_OSM_MAPPING_REVISION = "osm-overpass-source-record-mapping-v1"
_OSM_DATASET_RELEASE = "osm-overpass-live-v1"
_OSM_RAW_RESPONSE_ROLE = "osm_observations"
# Every role a source transition may keep as the untouched response a mapping unit was
# derived from. The action itself is source-neutral; only this set names the sources.
_SOURCE_RECORD_RAW_ROLES = frozenset({_OSM_RAW_RESPONSE_ROLE, _WIKIDATA_RAW_ROLE})
_SOURCE_RECORD_ROLE = "source_record"
_SOURCE_RECORD_MAPPING_TRANSITION_KEYS = frozenset(
    {_SOURCE_RECORD_MAPPING_TRANSITION_KEY, _WIKIDATA_RESULT_TRANSITION_KEY}
)
_CANDIDATE_ID_NAMESPACE = UUID("c8d4f0b6-1f2a-5d7e-9a34-6b0c2e5d81f7")
_ENTITY_RESOLUTION_TRANSITION_KEY = "entity-resolution-snapshot-quality-v1"
_ENTITY_RESOLUTION_OUTPUT_CONTRACT = "entity-resolution-snapshot@1"
_RESOLUTION_BATCH_ROLE = "resolution_batch"
_RESOLUTION_SNAPSHOT_ROLE = "resolution_snapshot"
_NORMALIZATION_MAX_PROFILE_BYTES = 1024 * 1024
_ResultT = TypeVar("_ResultT")


class PipelineAdvancementState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    APPLIED = "applied"
    BLOCKED = "blocked"


class PipelineTransitionDisposition(StrEnum):
    APPLY = "apply"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    artifact_id: UUID
    role: str
    content_digest: str
    size_bytes: int
    content_type: str

    def __post_init__(self) -> None:
        _require_token("artifact role", self.role)
        _require_sha256("artifact content digest", self.content_digest)
        if self.size_bytes < 0:
            raise ValueError("artifact size cannot be negative")
        _require_plain_text("artifact content type", self.content_type, maximum=200)


@dataclass(frozen=True, slots=True)
class SucceededWorkOutput:
    source_work_unit_id: UUID
    run_id: UUID
    stage_run_id: UUID
    stage: WorkStage
    capability: str
    output_contract: str
    output_digest: str
    output_artifact: ArtifactIdentity
    input_artifacts: tuple[ArtifactIdentity, ...]
    source_binding_key: str | None = None
    config_bundle_digest: str | None = None
    campaign_bundle_content: bytes | None = None
    output_content: bytes | None = None
    related_output_artifacts: tuple[ArtifactIdentity, ...] = ()
    http_request_content: bytes | None = None
    website_route_context_content: bytes | None = None
    extraction_request_content: bytes | None = None
    normalization_profile_content: bytes | None = None
    wikidata_query_spec_content: bytes | None = None
    resolution_batch_content: bytes | None = None
    manual_record_content: bytes | None = None
    manual_record_artifact: ArtifactIdentity | None = None
    manual_record_lineage_content: bytes | None = None

    def __post_init__(self) -> None:
        _require_token("work capability", self.capability)
        _require_token("work output contract", self.output_contract)
        _require_sha256("work output digest", self.output_digest)
        artifact_ids = tuple(item.artifact_id for item in self.input_artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("work input artifacts contain duplicate artifact identities")
        roles = tuple(item.role for item in self.input_artifacts)
        if len(roles) != len(set(roles)):
            raise ValueError("work input artifacts contain duplicate roles")
        if self.config_bundle_digest is not None:
            _require_sha256("run config bundle digest", self.config_bundle_digest)
        if self.campaign_bundle_content is not None:
            if self.config_bundle_digest is None:
                raise ValueError("campaign bundle content requires its exact run digest")
            if _digest_bytes(self.campaign_bundle_content) != self.config_bundle_digest:
                raise ValueError("campaign bundle bytes differ from the run config digest")
        if self.output_content is not None and (
            _digest_bytes(self.output_content) != self.output_artifact.content_digest
        ):
            raise ValueError("work output bytes differ from the exact artifact digest")


@dataclass(frozen=True, slots=True)
class PipelineBlocker:
    owner: str
    code: str
    message: str
    required_action: str
    context: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_token("pipeline blocker owner", self.owner)
        if _CODE_PATTERN.fullmatch(self.code) is None:
            raise ValueError("pipeline blocker code must use canonical upper snake case")
        _require_plain_text("pipeline blocker message", self.message, maximum=1_000)
        _require_plain_text(
            "pipeline blocker required action",
            self.required_action,
            maximum=1_000,
        )
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))


@dataclass(frozen=True, slots=True)
class WebsiteRouteAction:
    route_key: str
    target_binding_key: str
    source_key: str
    source_policy_digest: str
    retry_budget: int
    work_id: UUID
    semantic_key: str
    input_digest: str
    request_payload: bytes
    context_digest: str
    context_payload: bytes
    robots_decision_artifact_id: UUID | None = None
    robots_raw_artifact_id: UUID | None = None
    manual_record_lineage_artifact: ArtifactIdentity | None = None
    manual_record_lineage_content: bytes | None = None

    def __post_init__(self) -> None:
        _require_token("website route key", self.route_key)
        _require_token("website target binding key", self.target_binding_key)
        _require_token("website source key", self.source_key)
        _require_sha256("website source policy digest", self.source_policy_digest)
        if not 0 <= self.retry_budget <= 99:
            raise ValueError("website route retry budget is outside the work engine limit")
        _require_sha256("website work semantic key", self.semantic_key)
        _require_sha256("website request input digest", self.input_digest)
        if not self.request_payload:
            raise ValueError("website request payload cannot be empty")
        _require_sha256("website route context digest", self.context_digest)
        if _digest_bytes(self.request_payload) != self.input_digest:
            raise ValueError("website request payload differs from its input digest")
        if _digest_bytes(self.context_payload) != self.context_digest:
            raise ValueError("website route context differs from its digest")
        if self.manual_record_lineage_artifact is not None:
            if self.manual_record_lineage_artifact.role != "manual_record_lineage":
                raise ValueError("manual-record lineage artifact role is not canonical")
            if self.manual_record_lineage_content is not None and (
                _digest_bytes(self.manual_record_lineage_content)
                != self.manual_record_lineage_artifact.content_digest
            ):
                raise ValueError(
                    "manual-record lineage bytes differ from its exact artifact digest"
                )
        if (
            self.manual_record_lineage_content is not None
            and not self.manual_record_lineage_content
        ):
            raise ValueError("manual-record lineage content cannot be empty")


@dataclass(frozen=True, slots=True)
class ExtractionWorkAction:
    route_key: str
    source_key: str
    source_binding_key: str
    source_policy_digest: str
    retry_budget: int
    work_id: UUID
    semantic_key: str
    input_digest: str
    request_payload: bytes
    raw_artifact_id: UUID
    manual_record_lineage_artifact: ArtifactIdentity

    def __post_init__(self) -> None:
        _require_token("extraction route key", self.route_key)
        _require_token("extraction source key", self.source_key)
        _require_token("extraction source binding key", self.source_binding_key)
        _require_sha256("extraction source policy digest", self.source_policy_digest)
        if not 0 <= self.retry_budget <= 99:
            raise ValueError("extraction retry budget is outside the work engine limit")
        _require_sha256("extraction work semantic key", self.semantic_key)
        _require_sha256("extraction request digest", self.input_digest)
        if not self.request_payload or _digest_bytes(self.request_payload) != self.input_digest:
            raise ValueError("extraction request payload differs from its input digest")


@dataclass(frozen=True, slots=True)
class NormalizationWorkAction:
    source_binding_key: str
    source_policy_digest: str
    work_id: UUID
    semantic_key: str
    input_digest: str
    extracted_record_artifact_id: UUID
    profile_payload: bytes
    profile_digest: str
    manual_record_lineage_artifact: ArtifactIdentity

    def __post_init__(self) -> None:
        _require_token("normalization source binding key", self.source_binding_key)
        _require_sha256("normalization source policy digest", self.source_policy_digest)
        _require_sha256("normalization work semantic key", self.semantic_key)
        _require_sha256("normalization work input digest", self.input_digest)
        _require_sha256("normalization profile digest", self.profile_digest)
        if not self.profile_payload or _digest_bytes(self.profile_payload) != self.profile_digest:
            raise ValueError("normalization profile payload differs from its digest")
        if len(self.profile_payload) > _NORMALIZATION_MAX_PROFILE_BYTES:
            raise ValueError("normalization profile payload exceeds the worker input limit")


@dataclass(frozen=True, slots=True)
class SourceRecordMappingAction:
    """Bounded source-native mapping work derived from verified source provenance.

    Deliberately carries no manual-record lineage: a source-native record is proven by
    its own source identity, policy digest, dataset release and raw artifact, never by
    an HTML extraction artifact or a fabricated manual seed.
    """

    work_id: UUID
    source_key: str
    source_binding_key: str
    source_policy_digest: str
    source_record_id: str
    dataset_release: str
    source_record_artifact: ArtifactIdentity
    source_record_payload: bytes
    raw_response_artifact: ArtifactIdentity
    mapping_profile_payload: bytes
    mapping_profile_digest: str
    semantic_key: str
    input_digest: str
    # Elements the envelope carried but that could not be represented. They are
    # reported here instead of aborting the valid siblings.
    unrepresentable_source_record_ids: tuple[str, ...] = ()
    # Elements returned by the over-approximating polygon query that fall inside a
    # boundary hole. They are excluded from intake, never forwarded.
    excluded_by_boundary_record_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_record_payload:
            raise ValueError("mapping work requires the exact per-element source record bytes")
        if _digest_bytes(self.source_record_payload) != self.source_record_artifact.content_digest:
            raise ValueError("source record bytes differ from the declared artifact digest")
        if self.raw_response_artifact.role not in _SOURCE_RECORD_RAW_ROLES:
            raise ValueError("mapping work must keep the original raw response artifact")
        _require_token("mapping source key", self.source_key)
        _require_token("mapping source binding key", self.source_binding_key)
        _require_sha256("mapping source policy digest", self.source_policy_digest)
        _require_token("mapping source record id", self.source_record_id)
        _require_token("mapping dataset release", self.dataset_release)
        _require_sha256("mapping work semantic key", self.semantic_key)
        _require_sha256("mapping work input digest", self.input_digest)
        _require_sha256("mapping profile digest", self.mapping_profile_digest)
        if (
            not self.mapping_profile_payload
            or _digest_bytes(self.mapping_profile_payload) != self.mapping_profile_digest
        ):
            raise ValueError("mapping profile payload differs from its digest")
        if len(self.mapping_profile_payload) > _NORMALIZATION_MAX_PROFILE_BYTES:
            raise ValueError("mapping profile payload exceeds the worker input limit")
        if self.source_record_artifact.role != "source_record":
            raise ValueError("mapping work requires the canonical source_record artifact role")


@dataclass(frozen=True, slots=True)
class ManualRecordIntakeProvenance:
    """Intake provenance for HTML extraction bound to a verified manual seed record."""

    manual_record_artifact_id: UUID
    manual_record_lineage_artifact: ArtifactIdentity

    def __post_init__(self) -> None:
        if self.manual_record_lineage_artifact.role != "manual_record_lineage":
            raise ValueError("manual-record provenance requires the lineage artifact role")


@dataclass(frozen=True, slots=True)
class SourceRecordIntakeProvenance:
    """Intake provenance for a source-native record.

    Carries the source identity, policy and record artifact directly. It never
    references an extracted record or a manual-import lineage.
    """

    source_key: str
    source_binding_key: str
    campaign_key: str
    source_record_id: str
    source_policy_digest: str
    dataset_release: str
    mapping_revision: str
    # Digest of the acquisition evidence that produced this revision. The same acquisition
    # always resolves to the same digest, so a replay cannot append a second revision, while
    # a new fetch of the entity carries a new one.
    acquisition_identity: str
    source_record_artifact: ArtifactIdentity

    def __post_init__(self) -> None:
        _require_token("intake source key", self.source_key)
        _require_token("intake campaign key", self.campaign_key)
        _require_sha256("intake acquisition identity", self.acquisition_identity)
        _require_token("intake source binding key", self.source_binding_key)
        _require_token("intake source record id", self.source_record_id)
        _require_sha256("intake source policy digest", self.source_policy_digest)
        _require_token("intake dataset release", self.dataset_release)
        _require_token("intake mapping revision", self.mapping_revision)
        if self.source_record_artifact.role != "source_record":
            raise ValueError("source-record provenance requires the source_record artifact role")


CandidateIntakeProvenance = ManualRecordIntakeProvenance | SourceRecordIntakeProvenance


@dataclass(frozen=True, slots=True)
class CandidateIntakeAction:
    """Deterministic candidate intake identity derived from exact upstream provenance."""

    candidate_id: UUID
    run_id: UUID
    source_work_unit_id: UUID
    observation_batch_digest: str
    provenance: CandidateIntakeProvenance
    source_artifact_ids: tuple[UUID, ...]
    revision: int = 0

    def __post_init__(self) -> None:
        _require_sha256("candidate observation batch digest", self.observation_batch_digest)
        if self.revision != 0:
            raise ValueError("initial candidate intake revision must be zero")
        if not self.source_artifact_ids:
            raise ValueError("candidate intake requires source artifact provenance")
        if len(self.source_artifact_ids) != len(set(self.source_artifact_ids)):
            raise ValueError("candidate intake source artifacts must be unique")
        if (
            isinstance(self.provenance, SourceRecordIntakeProvenance)
            and self.provenance.source_record_artifact.artifact_id not in self.source_artifact_ids
        ):
            raise ValueError(
                "source-record provenance artifact must be among the intake source artifacts"
            )

    @property
    def manual_record_provenance(self) -> ManualRecordIntakeProvenance | None:
        if isinstance(self.provenance, ManualRecordIntakeProvenance):
            return self.provenance
        return None

    @property
    def source_record_provenance(self) -> SourceRecordIntakeProvenance | None:
        if isinstance(self.provenance, SourceRecordIntakeProvenance):
            return self.provenance
        return None


@dataclass(frozen=True, slots=True)
class ResolutionSnapshotAction:
    """The exact entity-resolution snapshot that becomes the run's current resolution."""

    batch_id: UUID
    batch_digest: str
    snapshot_digest: str
    cluster_ids: tuple[UUID, ...]
    member_candidate_ids: tuple[UUID, ...]
    export_eligible_cluster_count: int
    pending_review_pair_count: int

    def __post_init__(self) -> None:
        _require_sha256("resolution batch digest", self.batch_digest)
        _require_sha256("resolution snapshot digest", self.snapshot_digest)
        if not self.cluster_ids:
            raise ValueError("resolution snapshot requires at least one cluster")
        if tuple(sorted(set(self.cluster_ids), key=lambda value: value.hex)) != self.cluster_ids:
            raise ValueError("resolution cluster identities must be sorted and unique")
        if not self.member_candidate_ids:
            raise ValueError("resolution snapshot requires at least one cluster member")
        if (
            tuple(sorted(set(self.member_candidate_ids), key=lambda value: value.hex))
            != self.member_candidate_ids
        ):
            raise ValueError("resolution members must be sorted and unique")
        if not 0 <= self.export_eligible_cluster_count <= len(self.cluster_ids):
            raise ValueError("eligible cluster count is out of range")
        if self.pending_review_pair_count < 0:
            raise ValueError("pending review pair count cannot be negative")


@dataclass(frozen=True, slots=True)
class PipelineTransitionPlan:
    source_work_unit_id: UUID
    transition_key: str
    disposition: PipelineTransitionDisposition
    plan_digest: str
    blocker: PipelineBlocker | None
    website_actions: tuple[WebsiteRouteAction, ...] = ()
    extraction_actions: tuple[ExtractionWorkAction, ...] = ()
    normalization_actions: tuple[NormalizationWorkAction, ...] = ()
    source_record_mapping_actions: tuple[SourceRecordMappingAction, ...] = ()
    candidate_intake_actions: tuple[CandidateIntakeAction, ...] = ()
    resolution_snapshot_actions: tuple[ResolutionSnapshotAction, ...] = ()

    def __post_init__(self) -> None:
        _require_token("pipeline transition key", self.transition_key)
        _require_sha256("pipeline transition plan digest", self.plan_digest)
        if self.disposition is PipelineTransitionDisposition.APPLY and self.blocker is not None:
            raise ValueError("applicable pipeline transition cannot contain a blocker")
        if self.disposition is PipelineTransitionDisposition.BLOCK and self.blocker is None:
            raise ValueError("blocked pipeline transition requires a blocker")


@dataclass(frozen=True, slots=True)
class PipelineAdvancementLease:
    advancement_id: UUID
    source_work_unit_id: UUID
    lease_id: UUID
    lease_token: str
    worker_id: str
    dagster_execution_id: str
    dagster_build_id: str
    transition_key: str
    transition_plan_digest: str
    revision: int
    attempt_number: int
    issued_at_utc: datetime
    expires_at_utc: datetime

    def __post_init__(self) -> None:
        _require_token("pipeline lease token", self.lease_token)
        _require_token("pipeline worker id", self.worker_id)
        _require_token("Dagster execution id", self.dagster_execution_id)
        _require_token("Dagster build id", self.dagster_build_id)
        _require_token("pipeline transition key", self.transition_key)
        _require_sha256("pipeline transition plan digest", self.transition_plan_digest)
        if self.revision < 0:
            raise ValueError("pipeline advancement revision cannot be negative")
        if self.attempt_number < 1:
            raise ValueError("pipeline advancement attempt number must be positive")
        _require_aware_utc("pipeline lease issued_at_utc", self.issued_at_utc)
        _require_aware_utc("pipeline lease expires_at_utc", self.expires_at_utc)
        if self.expires_at_utc <= self.issued_at_utc:
            raise ValueError("pipeline lease expiry must follow issuance")


@dataclass(frozen=True, slots=True)
class PipelineAdvancementStatus:
    advancement_id: UUID
    source_work_unit_id: UUID
    run_id: UUID
    state: PipelineAdvancementState
    transition_key: str
    transition_plan_digest: str
    revision: int
    attempt_count: int
    result_digest: str | None
    blocker: PipelineBlocker | None
    created_at_utc: datetime
    updated_at_utc: datetime

    def __post_init__(self) -> None:
        _require_token("pipeline transition key", self.transition_key)
        _require_sha256("pipeline transition plan digest", self.transition_plan_digest)
        if self.revision < 0:
            raise ValueError("pipeline advancement revision cannot be negative")
        if self.attempt_count < 0:
            raise ValueError("pipeline advancement attempt count cannot be negative")
        if self.result_digest is not None:
            _require_sha256("pipeline advancement result digest", self.result_digest)
        _require_aware_utc("pipeline advancement created_at_utc", self.created_at_utc)
        _require_aware_utc("pipeline advancement updated_at_utc", self.updated_at_utc)
        if self.updated_at_utc < self.created_at_utc:
            raise ValueError("pipeline advancement update cannot precede creation")
        if self.state is PipelineAdvancementState.APPLIED:
            if self.result_digest is None or self.blocker is not None:
                raise ValueError("applied pipeline advancement requires only a result digest")
        elif self.state is PipelineAdvancementState.BLOCKED:
            if self.blocker is None or self.result_digest is not None:
                raise ValueError("blocked pipeline advancement requires only a blocker")
        elif self.result_digest is not None or self.blocker is not None:
            raise ValueError("non-terminal pipeline advancement cannot contain a terminal result")


@dataclass(frozen=True, slots=True)
class ClaimPipelineAdvancement:
    worker_id: str
    dagster_execution_id: str
    dagster_build_id: str
    lease_duration: timedelta
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("pipeline worker id", self.worker_id)
        _require_token("Dagster execution id", self.dagster_execution_id)
        _require_token("Dagster build id", self.dagster_build_id)
        _require_token("correlation_id", self.correlation_id)
        if not timedelta(seconds=30) <= self.lease_duration <= timedelta(minutes=30):
            raise ValueError("pipeline lease duration must be between 30 seconds and 30 minutes")


@dataclass(frozen=True, slots=True)
class ApplyPipelineAdvancement:
    advancement_id: UUID
    expected_revision: int
    lease_id: UUID
    lease_token: str
    dagster_execution_id: str
    transition_plan_digest: str
    result_digest: str
    correlation_id: str

    def __post_init__(self) -> None:
        if self.expected_revision < 0:
            raise ValueError("expected pipeline advancement revision cannot be negative")
        _require_token("pipeline lease token", self.lease_token)
        _require_token("Dagster execution id", self.dagster_execution_id)
        _require_sha256("pipeline transition plan digest", self.transition_plan_digest)
        _require_sha256("pipeline advancement result digest", self.result_digest)
        _require_token("correlation_id", self.correlation_id)


@dataclass(frozen=True, slots=True)
class BlockPipelineAdvancement:
    advancement_id: UUID
    expected_revision: int
    lease_id: UUID
    lease_token: str
    dagster_execution_id: str
    transition_plan_digest: str
    blocker: PipelineBlocker
    correlation_id: str

    def __post_init__(self) -> None:
        if self.expected_revision < 0:
            raise ValueError("expected pipeline advancement revision cannot be negative")
        _require_token("pipeline lease token", self.lease_token)
        _require_token("Dagster execution id", self.dagster_execution_id)
        _require_sha256("pipeline transition plan digest", self.transition_plan_digest)
        _require_token("correlation_id", self.correlation_id)


class PipelineAdvancementConflict(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: Mapping[str, object],
        required_action: str,
    ) -> None:
        if _CODE_PATTERN.fullmatch(code) is None:
            raise ValueError("pipeline conflict code must use canonical upper snake case")
        self.code = code
        self.message = message
        self.context = dict(context)
        self.required_action = required_action
        super().__init__(message)


class PipelineAdvancementPort(Protocol):
    def register(
        self,
        source: SucceededWorkOutput,
        plan: PipelineTransitionPlan,
        *,
        correlation_id: str,
    ) -> PipelineAdvancementStatus: ...

    def claim(self, command: ClaimPipelineAdvancement) -> PipelineAdvancementLease | None: ...

    def apply(self, command: ApplyPipelineAdvancement) -> PipelineAdvancementStatus: ...

    def block(self, command: BlockPipelineAdvancement) -> PipelineAdvancementStatus: ...


class PipelineTransitionRegistry:
    def plan(self, source: SucceededWorkOutput) -> PipelineTransitionPlan:
        if (
            source.stage is WorkStage.DISCOVERY
            and source.capability == "manual_import"
            and source.output_contract == "manual-import-plan@1"
        ):
            return self._manual_import_plan(source)
        if (
            source.stage is WorkStage.DISCOVERY
            and source.capability == "manual_record"
            and source.output_contract == "manual-import-record@1"
        ):
            return self._manual_record(source)
        if (
            source.stage is WorkStage.ACQUISITION
            and source.capability == "http_fetch"
            and source.output_contract == "official-http-acquisition@1"
        ):
            return self._official_http_transition(source)
        if (
            source.stage is WorkStage.EXTRACTION
            and source.capability == "extraction"
            and source.output_contract == "extracted-record@1"
        ):
            return self._extracted_record_normalization(source)
        if (
            source.stage is WorkStage.DISCOVERY
            and source.capability == WorkCapability.OSM_QUERY.value
            and source.output_contract == _OSM_RESULT_OUTPUT_CONTRACT
        ):
            return self._osm_source_record_mapping(source)
        if (
            source.stage is WorkStage.DISCOVERY
            and source.capability == WorkCapability.WIKIDATA_ENTITY_QUERY.value
            and source.output_contract == _WIKIDATA_RESULT_OUTPUT_CONTRACT
        ):
            return self._wikidata_source_record_mapping(source)
        if (
            source.stage is WorkStage.NORMALIZATION
            and source.capability == WorkCapability.SOURCE_RECORD_MAPPING.value
            and source.output_contract == _SOURCE_RECORD_MAPPING_OUTPUT_CONTRACT
        ):
            return self._source_record_intake(source)
        if (
            source.stage is WorkStage.NORMALIZATION
            and source.capability == "normalization"
            and source.output_contract == _CANDIDATE_INTAKE_OUTPUT_CONTRACT
        ):
            return self._candidate_intake(source)
        if (
            source.stage is WorkStage.ENTITY_RESOLUTION
            and source.capability == WorkCapability.ENTITY_RESOLUTION.value
            and source.output_contract == _ENTITY_RESOLUTION_OUTPUT_CONTRACT
        ):
            return self._entity_resolution_snapshot(source)
        return self._blocked(
            source,
            transition_key="unsupported-transition",
            code="PIPELINE_TRANSITION_UNSUPPORTED",
            message="No pipeline transition owns the successful work output contract.",
            required_action=(
                "Implement and register an exact owner transition before this output may advance."
            ),
            context={
                "stage": source.stage.value,
                "capability": source.capability,
                "outputContract": source.output_contract,
                "outputRole": source.output_artifact.role,
            },
        )

    def _manual_import_plan(self, source: SucceededWorkOutput) -> PipelineTransitionPlan:
        if source.output_artifact.role != "manual_import_plan":
            return self._blocked(
                source,
                transition_key="manual-import-plan-admission",
                code="PIPELINE_OUTPUT_ARTIFACT_CONFLICT",
                message="The manual-import plan output role does not match its contract.",
                required_action=(
                    "Re-run the exact manual-import work with the canonical output role."
                ),
                context={
                    "expectedRole": "manual_import_plan",
                    "actualRole": source.output_artifact.role,
                },
            )
        parents = tuple(
            artifact
            for artifact in source.input_artifacts
            if any(pattern.fullmatch(artifact.role) is not None for pattern in _MANUAL_SOURCE_ROLES)
        )
        if not parents:
            return self._blocked(
                source,
                transition_key="manual-import-plan-admission",
                code="PIPELINE_INPUT_ARTIFACT_MISSING",
                message="The manual-import plan has no canonical source artifact binding.",
                required_action=(
                    "Bind exactly one canonical manual source artifact and re-run the source work."
                ),
                context={"requiredRolePattern": "manual[_import]_source:<format>:<atomic|partial>"},
            )
        if len(parents) != 1:
            return self._blocked(
                source,
                transition_key="manual-import-plan-admission",
                code="PIPELINE_INPUT_ARTIFACT_CONFLICT",
                message="The manual-import plan has multiple canonical source artifact bindings.",
                required_action=(
                    "Repair the source work so exactly one canonical "
                    "manual source artifact is bound."
                ),
                context={"matchingArtifactIds": [str(item.artifact_id) for item in parents]},
            )
        return self._plan(
            source,
            transition_key="manual-import-plan-admission",
            disposition=PipelineTransitionDisposition.APPLY,
            blocker=None,
        )

    def _manual_record(self, source: SucceededWorkOutput) -> PipelineTransitionPlan:
        if source.output_artifact.role != "manual_import_record":
            return self._blocked(
                source,
                transition_key="manual-record-routing",
                code="PIPELINE_OUTPUT_ARTIFACT_CONFLICT",
                message="The manual-record output role does not match its contract.",
                required_action=(
                    "Re-run the exact manual-record work with the canonical output role."
                ),
                context={
                    "expectedRole": "manual_import_record",
                    "actualRole": source.output_artifact.role,
                },
            )
        if source.source_binding_key is None:
            return self._blocked_manual_record_routing(source)
        if source.campaign_bundle_content is None or source.output_content is None:
            return self._blocked(
                source,
                transition_key="manual-record-website-routing-v1",
                code="MANUAL_RECORD_ROUTE_INPUT_UNAVAILABLE",
                message="The exact run snapshot and manual-record bytes are required for routing.",
                required_action=(
                    "Read and verify the immutable snapshot and output artifact before routing."
                ),
                context={"campaignRunId": str(source.run_id)},
            )
        try:
            actions, routing_state = _plan_manual_record_website_actions(source)
        except _WebsiteRoutePlanningError as exc:
            return self._blocked(
                source,
                transition_key="manual-record-website-routing-v1",
                code=exc.code,
                message=exc.message,
                required_action=exc.required_action,
                context=exc.context,
            )
        if routing_state == "not_configured":
            return self._blocked_manual_record_routing(source)
        if not actions:
            return self._blocked(
                source,
                transition_key="manual-record-website-routing-v1",
                code="MANUAL_RECORD_ROUTE_UNAVAILABLE",
                message="No authorized website route matches the record's source binding.",
                required_action=(
                    "Add an approved, binding-scoped website route to the run's campaign snapshot."
                ),
                context={"sourceBindingKey": source.source_binding_key},
            )
        return self._plan(
            source,
            transition_key="manual-record-website-routing-v1",
            disposition=PipelineTransitionDisposition.APPLY,
            blocker=None,
            website_actions=actions,
        )

    def _blocked_manual_record_routing(
        self,
        source: SucceededWorkOutput,
    ) -> PipelineTransitionPlan:
        return self._blocked(
            source,
            transition_key="manual-record-routing",
            code="MANUAL_RECORD_DOWNSTREAM_SOURCE_UNAVAILABLE",
            message="The manual record has no approved downstream source owner in this campaign.",
            required_action=(
                "Publish exact website or OSM source bindings in a new campaign snapshot "
                "before creating acquisition work."
            ),
            context={"campaignRunId": str(source.run_id)},
        )

    def _official_http_transition(
        self,
        source: SucceededWorkOutput,
    ) -> PipelineTransitionPlan:
        transition_key = "official-http-acquisition-routing-v1"
        if source.output_content is None:
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_HTTP_MANIFEST_MISSING",
                message="The canonical official HTTP manifest is unavailable for dispatch.",
                required_action="Repair the successful HTTP output artifact before advancement.",
                context={},
            )
        try:
            document = json.loads(
                source.output_content.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
            )
            if not isinstance(document, dict) or not {
                "contract",
                "contractRevision",
            }.issubset(document):
                raise ValueError("HTTP manifest discriminators are missing")
            manifest = HttpAcquisitionManifest.model_validate_json(
                source.output_content, strict=True
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_HTTP_MANIFEST_INVALID",
                message="The canonical official HTTP manifest violates its typed contract.",
                required_action="Repair the manifest through the official HTTP producer owner.",
                context={"causeType": type(exc).__name__},
            )
        if source.http_request_content is None:
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_HTTP_REQUEST_MISSING",
                message="The exact successful HTTP request artifact is unavailable.",
                required_action="Repair the Worker Gateway input bindings before advancement.",
                context={},
            )
        try:
            request_document = json.loads(
                source.http_request_content.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_HTTP_REQUEST_INVALID",
                message="The exact successful HTTP request is not valid canonical JSON.",
                required_action="Repair the request artifact through its producer owner.",
                context={"causeType": type(exc).__name__},
            )
        if not isinstance(request_document, dict) or not {
            "contract",
            "contractRevision",
            "requestId",
            "requestKind",
            "sourceKey",
            "sourcePolicyDigest",
        }.issubset(request_document):
            request_identity_matches = False
        else:
            request_identity_matches = (
                request_document["contract"] == "official-http-request"
                and request_document["contractRevision"] == "official-http-request-v1"
                and request_document["requestId"] == manifest.request_id
                and request_document["requestKind"] == manifest.request_kind
                and request_document["sourceKey"] == manifest.source_key
                and request_document["sourcePolicyDigest"] == manifest.source_policy_digest
                and request_document.get("url") == manifest.requested_url
            )
        if not request_identity_matches:
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_HTTP_OUTPUT_IDENTITY_MISMATCH",
                message="The HTTP manifest does not belong to its exact typed request input.",
                required_action="Repair the producer-bound request and manifest artifacts.",
                context={"requestKind": manifest.request_kind},
            )
        if manifest.request_kind == "robots":
            return self._official_http_robots_route(source)
        if manifest.request_kind == "page":
            try:
                action = _plan_official_http_page_extraction(
                    source,
                    manifest=manifest,
                    request=request_document,
                )
            except _WebsiteRoutePlanningError as exc:
                return self._blocked(
                    source,
                    transition_key=_PAGE_EXTRACTION_TRANSITION_KEY,
                    code=exc.code,
                    message=exc.message,
                    required_action=exc.required_action,
                    context=exc.context,
                )
            return self._plan(
                source,
                transition_key=_PAGE_EXTRACTION_TRANSITION_KEY,
                disposition=PipelineTransitionDisposition.APPLY,
                blocker=None,
                extraction_actions=(action,),
            )
        return self._blocked(
            source,
            transition_key=f"official-http-{manifest.request_kind}-routing-v1",
            code="PIPELINE_TRANSITION_UNSUPPORTED",
            message="No downstream transition owns this official HTTP request kind yet.",
            required_action=(
                "Register the exact page or sitemap consumer before advancing this output."
            ),
            context={"requestKind": manifest.request_kind},
        )

    def _official_http_robots_route(
        self,
        source: SucceededWorkOutput,
    ) -> PipelineTransitionPlan:
        transition_key = "official-http-robots-routing-v1"
        context_artifacts = tuple(
            item for item in source.input_artifacts if item.role == "website_route_context"
        )
        request_artifacts = tuple(
            item for item in source.input_artifacts if item.role == "http_request"
        )
        if (
            source.output_artifact.role != "http_acquisition_manifest"
            or source.output_content is None
            or source.http_request_content is None
            or source.website_route_context_content is None
            or len(context_artifacts) != 1
            or len(request_artifacts) != 1
        ):
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_WEBSITE_ROUTE_EVIDENCE_MISSING",
                message="The exact HTTP request, manifest, and saved route context are required.",
                required_action=(
                    "Repair the canonical HTTP artifact bindings before route advancement."
                ),
                context={
                    "contextCount": len(context_artifacts),
                    "requestCount": len(request_artifacts),
                },
            )
        try:
            manifest = HttpAcquisitionManifest.model_validate_json(
                source.output_content, strict=True
            )
            context = decode_website_route_context(source.website_route_context_content)
            request = json.loads(
                source.http_request_content.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_WEBSITE_ROUTE_EVIDENCE_INVALID",
                message="The saved route context or official HTTP request violates its contract.",
                required_action="Repair the exact input artifacts through their producer owner.",
                context={"causeType": type(exc).__name__},
            )
        if not isinstance(request, dict):
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_WEBSITE_ROUTE_REQUEST_INVALID",
                message="The exact HTTP request artifact is not a JSON object.",
                required_action="Repair the request artifact through the Worker Gateway owner.",
                context={},
            )
        request_bytes = source.http_request_content
        request_digest = _digest_bytes(request_bytes)
        context_digest = _digest_bytes(source.website_route_context_content)
        request_origin, _ = _canonical_origin(str(context.source_url))
        identity_matches = (
            context.request_id == request.get("requestId") == manifest.request_id
            and context.request_digest == request_digest == request_artifacts[0].content_digest
            and context_artifacts[0].content_digest == context_digest
            and source.config_bundle_digest == context.campaign_bundle_digest
            and source.source_binding_key == context.target_binding_key
            and request.get("contract") == "official-http-request"
            and request.get("contractRevision") == "official-http-request-v1"
            and request.get("requestKind") == "robots"
            and request.get("url") == f"{request_origin}/robots.txt"
            and request.get("allowedOrigin") == request_origin
            and request.get("sourceKey") == context.source_key == manifest.source_key
            and request.get("sourcePolicyDigest")
            == context.source_policy_digest
            == manifest.source_policy_digest
            and manifest.request_kind == "robots"
            and manifest.requested_url == request.get("url")
            and manifest.robots_target_url == str(context.source_url)
            and context.target_binding_key == source.source_binding_key
        )
        if not identity_matches:
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_WEBSITE_ROUTE_IDENTITY_MISMATCH",
                message=(
                    "The HTTP result does not belong to this exact run route, request, and seed."
                ),
                required_action=(
                    "Repair the route/request provenance; do not infer target identity."
                ),
                context={"routeKey": context.route_key},
            )
        if (
            manifest.outcome not in {"fetched", "empty"}
            or manifest.robots_allowed is None
            or manifest.robots_decision_digest is None
            or manifest.robots_artifact_id is not None
        ):
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_WEBSITE_ROBOTS_DECISION_UNAVAILABLE",
                message="The robots result has no complete seed-specific typed decision.",
                required_action=(
                    "Require a successful target-specific robots evaluation before routing."
                ),
                context={"outcome": manifest.outcome},
            )
        if not manifest.robots_allowed:
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_WEBSITE_ROBOTS_DISALLOWED",
                message="The exact seed URL is disallowed by its target-specific robots decision.",
                required_action="Do not create page work for this seed URL.",
                context={
                    "routeKey": context.route_key,
                    "targetUrl": str(context.source_url),
                    "decisionDigest": manifest.robots_decision_digest,
                    "robotsArtifactId": str(source.output_artifact.artifact_id),
                },
            )

        page_request = dict(request)
        page_request_id = uuid5(
            _WEBSITE_ROUTE_NAMESPACE,
            f"page:{source.run_id}:{source.source_work_unit_id}:{context.route_key}:"
            f"{manifest.robots_decision_digest}",
        ).hex
        page_request.update(
            requestId=page_request_id,
            requestKind="page",
            url=str(context.source_url),
            robotsAllowed=True,
            robotsArtifactId=str(source.output_artifact.artifact_id),
            robotsDecisionDigest=manifest.robots_decision_digest,
            robotsManifestDigest=source.output_artifact.content_digest,
            pageInterests=[item.model_dump(mode="json") for item in context.page_interests],
        )
        page_request_bytes = _canonical_json_bytes(page_request)
        page_request_digest = _digest_bytes(page_request_bytes)
        page_context = context.model_copy(
            update={"request_id": page_request_id, "request_digest": page_request_digest}
        )
        page_context_bytes = _canonical_json_bytes(
            page_context.model_dump(mode="json", by_alias=True)
        )
        page_context_digest = _digest_bytes(page_context_bytes)
        raw_outputs = tuple(
            item for item in source.related_output_artifacts if item.role == "http_raw_response"
        )
        lineage_artifacts = tuple(
            item for item in source.input_artifacts if item.role == "manual_record_lineage"
        )
        if len(lineage_artifacts) != 1:
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_MANUAL_RECORD_LINEAGE_MISSING_OR_AMBIGUOUS",
                message="Routed robots work must carry exactly one manual-record lineage artifact.",
                required_action="Repair the exact lineage binding at the route-work producer.",
                context={"lineageArtifactCount": len(lineage_artifacts)},
            )
        lineage_artifact = lineage_artifacts[0]
        try:
            lineage_content = source.manual_record_lineage_content
            if (
                lineage_content is None
                or _digest_bytes(lineage_content) != lineage_artifact.content_digest
            ):
                raise ValueError(
                    "manual-record lineage bytes differ from the leased artifact digest"
                )
            lineage = ManualRecordLineage.model_validate_json(lineage_content, strict=True)
            if canonical_manual_record_lineage_bytes(lineage) != lineage_content:
                raise ValueError("manual-record lineage is not canonical")
            if (
                lineage.run_id != source.run_id
                or lineage.manual_record_content_digest != context.record_digest
            ):
                raise ValueError("manual-record lineage does not match the exact run and route")
        except (TypeError, ValueError) as exc:
            return self._blocked(
                source,
                transition_key=transition_key,
                code="PIPELINE_MANUAL_RECORD_LINEAGE_INVALID",
                message="Routed robots work has invalid or mismatched manual-record lineage.",
                required_action="Repair the lineage artifact at its manual-record producer.",
                context={"causeType": type(exc).__name__},
            )
        route_identity = {
            "runId": str(source.run_id),
            "manualRecordLineageArtifactId": str(lineage_artifact.artifact_id),
            "manualRecordLineageDigest": lineage_artifact.content_digest,
            "sourceWorkUnitId": str(source.source_work_unit_id),
            "configBundleDigest": context.campaign_bundle_digest,
            "routeKey": context.route_key,
            "targetBindingKey": context.target_binding_key,
            "seedUrl": str(context.source_url),
            "robotsManifestArtifactId": str(source.output_artifact.artifact_id),
            "robotsManifestDigest": source.output_artifact.content_digest,
            "robotsRawArtifactId": (str(raw_outputs[0].artifact_id) if raw_outputs else None),
            "robotsRawDigest": raw_outputs[0].content_digest if raw_outputs else None,
            "decisionDigest": manifest.robots_decision_digest,
        }
        semantic_key = _digest_bytes(_canonical_json_bytes(route_identity))
        action = WebsiteRouteAction(
            route_key=context.route_key,
            target_binding_key=context.target_binding_key,
            source_key=context.source_key,
            source_policy_digest=context.source_policy_digest,
            retry_budget=context.retry_budget,
            work_id=uuid5(_WEBSITE_ROUTE_NAMESPACE, semantic_key),
            semantic_key=semantic_key,
            input_digest=page_request_digest,
            request_payload=page_request_bytes,
            context_digest=page_context_digest,
            context_payload=page_context_bytes,
            robots_decision_artifact_id=source.output_artifact.artifact_id,
            robots_raw_artifact_id=raw_outputs[0].artifact_id if raw_outputs else None,
            manual_record_lineage_artifact=lineage_artifact,
        )
        return self._plan(
            source,
            transition_key=transition_key,
            disposition=PipelineTransitionDisposition.APPLY,
            blocker=None,
            website_actions=(action,),
        )

    def _extracted_record_normalization(
        self,
        source: SucceededWorkOutput,
    ) -> PipelineTransitionPlan:
        try:
            action = _plan_extracted_record_normalization(source)
        except _NormalizationPlanningError as exc:
            return self._blocked(
                source,
                transition_key=_NORMALIZATION_TRANSITION_KEY,
                code=exc.code,
                message=exc.message,
                required_action=exc.required_action,
                context=exc.context,
            )
        return self._plan(
            source,
            transition_key=_NORMALIZATION_TRANSITION_KEY,
            disposition=PipelineTransitionDisposition.APPLY,
            blocker=None,
            normalization_actions=(action,),
        )

    def _osm_source_record_mapping(
        self,
        source: SucceededWorkOutput,
    ) -> PipelineTransitionPlan:
        try:
            actions = _plan_osm_source_record_mapping(source)
        except _SourceRecordMappingPlanningError as exc:
            return self._blocked(
                source,
                transition_key=_OSM_RESULT_TRANSITION_KEY,
                code=exc.code,
                message=exc.message,
                required_action=exc.required_action,
                context=exc.context,
            )
        return self._plan(
            source,
            transition_key=_OSM_RESULT_TRANSITION_KEY,
            disposition=PipelineTransitionDisposition.APPLY,
            blocker=None,
            source_record_mapping_actions=actions,
        )

    def _wikidata_source_record_mapping(
        self,
        source: SucceededWorkOutput,
    ) -> PipelineTransitionPlan:
        try:
            actions = _plan_wikidata_source_record_mapping(source)
        except _SourceRecordMappingPlanningError as exc:
            return self._blocked(
                source,
                transition_key=_WIKIDATA_RESULT_TRANSITION_KEY,
                code=exc.code,
                message=exc.message,
                required_action=exc.required_action,
                context=exc.context,
            )
        return self._plan(
            source,
            transition_key=_WIKIDATA_RESULT_TRANSITION_KEY,
            disposition=PipelineTransitionDisposition.APPLY,
            blocker=None,
            source_record_mapping_actions=actions,
        )

    def _entity_resolution_snapshot(
        self,
        source: SucceededWorkOutput,
    ) -> PipelineTransitionPlan:
        try:
            action = _plan_entity_resolution_snapshot(source)
        except _ResolutionPlanningError as exc:
            return self._blocked(
                source,
                transition_key=_ENTITY_RESOLUTION_TRANSITION_KEY,
                code=exc.code,
                message=exc.message,
                required_action=exc.required_action,
                context=exc.context,
            )
        return self._plan(
            source,
            transition_key=_ENTITY_RESOLUTION_TRANSITION_KEY,
            disposition=PipelineTransitionDisposition.APPLY,
            blocker=None,
            resolution_snapshot_actions=(action,),
        )

    def _source_record_intake(
        self,
        source: SucceededWorkOutput,
    ) -> PipelineTransitionPlan:
        try:
            action = _plan_source_record_intake(source)
        except _SourceRecordMappingPlanningError as exc:
            return self._blocked(
                source,
                transition_key=_SOURCE_RECORD_INTAKE_TRANSITION_KEY,
                code=exc.code,
                message=exc.message,
                required_action=exc.required_action,
                context=exc.context,
            )
        return self._plan(
            source,
            transition_key=_SOURCE_RECORD_INTAKE_TRANSITION_KEY,
            disposition=PipelineTransitionDisposition.APPLY,
            blocker=None,
            candidate_intake_actions=(action,),
        )

    def _candidate_intake(
        self,
        source: SucceededWorkOutput,
    ) -> PipelineTransitionPlan:
        try:
            action = _plan_candidate_intake(source)
        except _CandidateIntakePlanningError as exc:
            return self._blocked(
                source,
                transition_key=_CANDIDATE_INTAKE_TRANSITION_KEY,
                code=exc.code,
                message=exc.message,
                required_action=exc.required_action,
                context=exc.context,
            )
        return self._plan(
            source,
            transition_key=_CANDIDATE_INTAKE_TRANSITION_KEY,
            disposition=PipelineTransitionDisposition.APPLY,
            blocker=None,
            candidate_intake_actions=(action,),
        )

    def _blocked(
        self,
        source: SucceededWorkOutput,
        *,
        transition_key: str,
        code: str,
        message: str,
        required_action: str,
        context: Mapping[str, object],
    ) -> PipelineTransitionPlan:
        blocker = PipelineBlocker(
            owner="PipelineAdvancement",
            code=code,
            message=message,
            required_action=required_action,
            context={"sourceWorkUnitId": str(source.source_work_unit_id), **dict(context)},
        )
        return self._plan(
            source,
            transition_key=transition_key,
            disposition=PipelineTransitionDisposition.BLOCK,
            blocker=blocker,
        )

    @staticmethod
    def _plan(
        source: SucceededWorkOutput,
        *,
        transition_key: str,
        disposition: PipelineTransitionDisposition,
        blocker: PipelineBlocker | None,
        website_actions: tuple[WebsiteRouteAction, ...] = (),
        extraction_actions: tuple[ExtractionWorkAction, ...] = (),
        normalization_actions: tuple[NormalizationWorkAction, ...] = (),
        candidate_intake_actions: tuple[CandidateIntakeAction, ...] = (),
        source_record_mapping_actions: tuple[SourceRecordMappingAction, ...] = (),
        resolution_snapshot_actions: tuple[ResolutionSnapshotAction, ...] = (),
    ) -> PipelineTransitionPlan:
        digest = _transition_plan_digest(
            source,
            transition_key=transition_key,
            disposition=disposition,
            blocker=blocker,
            website_actions=website_actions,
            extraction_actions=extraction_actions,
            normalization_actions=normalization_actions,
            candidate_intake_actions=candidate_intake_actions,
            source_record_mapping_actions=source_record_mapping_actions,
            resolution_snapshot_actions=resolution_snapshot_actions,
        )
        return PipelineTransitionPlan(
            source_work_unit_id=source.source_work_unit_id,
            transition_key=transition_key,
            disposition=disposition,
            plan_digest=digest,
            blocker=blocker,
            website_actions=website_actions,
            extraction_actions=extraction_actions,
            normalization_actions=normalization_actions,
            candidate_intake_actions=candidate_intake_actions,
            source_record_mapping_actions=source_record_mapping_actions,
            resolution_snapshot_actions=resolution_snapshot_actions,
        )


class PipelineAdvancementService:
    def __init__(
        self,
        port: PipelineAdvancementPort,
        registry: PipelineTransitionRegistry | None = None,
    ) -> None:
        self._port = port
        self._registry = registry or PipelineTransitionRegistry()

    def register(
        self,
        source: SucceededWorkOutput,
        *,
        correlation_id: str,
    ) -> PipelineAdvancementStatus:
        _require_token("correlation_id", correlation_id)
        plan = self._registry.plan(source)
        return self._invoke(
            correlation_id,
            lambda: self._port.register(source, plan, correlation_id=correlation_id),
        )

    def claim(self, command: ClaimPipelineAdvancement) -> PipelineAdvancementLease | None:
        return self._invoke(command.correlation_id, lambda: self._port.claim(command))

    def apply(self, command: ApplyPipelineAdvancement) -> PipelineAdvancementStatus:
        return self._invoke(command.correlation_id, lambda: self._port.apply(command))

    def block(self, command: BlockPipelineAdvancement) -> PipelineAdvancementStatus:
        return self._invoke(command.correlation_id, lambda: self._port.block(command))

    @staticmethod
    def _invoke(correlation_id: str, operation: Callable[[], _ResultT]) -> _ResultT:
        _require_token("correlation_id", correlation_id)
        try:
            return operation()
        except PipelineAdvancementConflict as exc:
            raise owner_error(
                error_type=f"collection/{exc.code.lower().replace('_', '-')}",
                owner="PipelineAdvancement",
                code=exc.code,
                message=exc.message,
                context=exc.context,
                required_action=exc.required_action,
                correlation_id=correlation_id,
            ) from exc


class _WebsiteRoutePlanningError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        required_action: str,
        context: Mapping[str, object],
    ) -> None:
        self.code = code
        self.message = message
        self.required_action = required_action
        self.context = dict(context)
        super().__init__(message)


class _SourceRecordMappingPlanningError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        required_action: str,
        context: Mapping[str, object],
    ) -> None:
        self.code = code
        self.message = message
        self.required_action = required_action
        self.context = dict(context)
        super().__init__(message)


def _source_record_mapping_error(
    code: str,
    reason: str,
    **context: object,
) -> _SourceRecordMappingPlanningError:
    return _SourceRecordMappingPlanningError(
        code=code,
        message="OSM source evidence does not authorize source-record mapping work.",
        required_action=(
            "Repair the exact OSM result, its query specification, and the run snapshot "
            "before scheduling source-record mapping."
        ),
        context={"reason": reason, **context},
    )


class _ResolutionPlanningError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        required_action: str,
        **context: object,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.required_action = required_action
        self.context = context


def _plan_entity_resolution_snapshot(source: SucceededWorkOutput) -> ResolutionSnapshotAction:
    """Prove the snapshot belongs to the exact batch the worker was leased to resolve."""
    if source.output_artifact.role != _RESOLUTION_SNAPSHOT_ROLE:
        raise _ResolutionPlanningError(
            "PIPELINE_RESOLUTION_OUTPUT_ROLE_CONFLICT",
            "The resolution output role does not match its contract.",
            "Re-run the exact resolution unit with the canonical snapshot role.",
            outputRole=source.output_artifact.role,
        )
    if len(source.input_artifacts) != 1 or (
        source.input_artifacts[0].role != _RESOLUTION_BATCH_ROLE
    ):
        raise _ResolutionPlanningError(
            "PIPELINE_RESOLUTION_INPUT_ROLE_CONFLICT",
            "The resolution unit must read exactly one canonical batch artifact.",
            "Re-enqueue resolution work with the exact batch input role.",
            inputRoles=[item.role for item in source.input_artifacts],
        )
    if source.output_content is None or source.resolution_batch_content is None:
        raise _ResolutionPlanningError(
            "PIPELINE_RESOLUTION_CONTENT_MISSING",
            "Resolution advancement requires the durable batch and snapshot bytes.",
            "Restore the immutable batch and snapshot artifacts before advancing.",
        )
    try:
        snapshot = decode_resolution_snapshot(source.output_content)
        batch = decode_resolution_batch(source.resolution_batch_content)
    except (TypeError, ValueError) as exc:
        raise _ResolutionPlanningError(
            "PIPELINE_RESOLUTION_CONTENT_INVALID",
            "The resolution batch or snapshot violates its canonical contract.",
            "Repair the immutable resolution artifacts at their producer.",
            causeType=type(exc).__name__,
        ) from exc
    if (
        batch.batch_id != snapshot.batch_id
        or batch.batch_digest != snapshot.batch_digest
        or batch.market_area_revision != snapshot.market_area_revision
        or batch.market_area_digest != snapshot.market_area_digest
    ):
        raise _ResolutionPlanningError(
            "PIPELINE_RESOLUTION_BATCH_BINDING_CONFLICT",
            "The snapshot was not produced from the batch bound to this work unit.",
            "Re-run resolution against the exact canonical batch.",
            batchId=str(batch.batch_id),
            snapshotBatchId=str(snapshot.batch_id),
        )
    batch_candidates = tuple(item.candidate_id for item in batch.candidates)
    members = tuple(
        sorted(
            {member for cluster in snapshot.clusters for member in cluster.member_candidate_ids},
            key=lambda value: value.hex,
        )
    )
    if members != tuple(sorted(batch_candidates, key=lambda value: value.hex)):
        raise _ResolutionPlanningError(
            "PIPELINE_RESOLUTION_MEMBERSHIP_INCOMPLETE",
            "Resolved clusters do not cover exactly the batch candidates.",
            "Repair constrained clustering so every candidate belongs to one cluster.",
        )
    eligible = sum(1 for item in snapshot.quality_assessments if item.export_eligible)
    return ResolutionSnapshotAction(
        batch_id=batch.batch_id,
        batch_digest=batch.batch_digest,
        snapshot_digest=snapshot.snapshot_digest,
        cluster_ids=tuple(
            sorted((item.cluster_id for item in snapshot.clusters), key=lambda value: value.hex)
        ),
        member_candidate_ids=members,
        export_eligible_cluster_count=eligible,
        pending_review_pair_count=len(snapshot.pending_review_pair_ids),
    )


def source_entity_candidate_id(
    *,
    campaign_key: str,
    source_binding_key: str,
    source_record_id: str,
) -> UUID:
    """The stable logical identity of one source entity inside one campaign.

    The source's own record ID is the canonical external identity (a Wikidata QID, an OSM
    element), so the same entity always resolves to the same candidate no matter which run
    retrieved it or when. The identity is derived, not stored, so concurrent runs agree on it
    without a coordination table and no stored UUID ever has to be rewritten.
    """

    return uuid5(
        _CANDIDATE_ID_NAMESPACE,
        "|".join(("source-entity", campaign_key, source_binding_key, source_record_id)),
    )


def source_acquisition_identity(
    *,
    source_record_id: str,
    dataset_release: str,
    source_policy_digest: str,
    mapping_revision: str,
    observation_profile_digest: str,
    source_record_artifact_digest: str,
) -> str:
    """The identity of one genuinely distinct acquisition of one source entity.

    The artifact digest covers the retrieved bytes together with the retrieval instant, so a
    replay of the same acquisition reproduces it and a later fetch does not. The run ID is
    deliberately absent: identity belongs to the source evidence, not to the orchestration
    that happened to carry it.
    """

    return sha256_json(
        {
            "datasetRelease": dataset_release,
            "mappingRevision": mapping_revision,
            "observationProfileDigest": observation_profile_digest,
            "sourcePolicyDigest": source_policy_digest,
            "sourceRecordArtifactDigest": source_record_artifact_digest,
            "sourceRecordId": source_record_id,
        }
    )


def _snapshot_campaign_key(source: SucceededWorkOutput) -> str:
    """The campaign key of the exact sealed run snapshot.

    Candidate identity is campaign-scoped, so the key must come from the snapshot the run was
    created with rather than from mutable authoring files.
    """

    if source.campaign_bundle_content is None:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_INTAKE_SNAPSHOT_MISSING",
            "source_record_intake_requires_run_snapshot",
            sourceWorkUnitId=str(source.source_work_unit_id),
        )
    try:
        bundle = json.loads(source.campaign_bundle_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_INTAKE_SNAPSHOT_INVALID",
            "run_snapshot_is_not_canonical_json",
        ) from exc
    campaign_key = bundle.get("campaignKey") if isinstance(bundle, dict) else None
    if not isinstance(campaign_key, str) or not campaign_key:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_INTAKE_SNAPSHOT_INVALID",
            "run_snapshot_has_no_campaign_key",
        )
    return campaign_key


def _plan_source_record_intake(source: SucceededWorkOutput) -> CandidateIntakeAction:
    if source.source_binding_key is None:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_INTAKE_BINDING_MISSING",
            "mapping_work_has_no_source_binding",
        )
    if source.output_content is None:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_INTAKE_BATCH_UNREADABLE",
            "mapping_output_content_unavailable",
        )
    try:
        batch = decode_source_record_observation_batch(source.output_content)
    except (TypeError, ValueError) as exc:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_INTAKE_BATCH_INVALID",
            type(exc).__name__,
        ) from exc
    # The batch content digest covers the batch payload; the succeeded output digest
    # covers the artifact bytes. Both must hold independently.
    if _digest_bytes(source.output_content) != source.output_digest:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_INTAKE_DIGEST_CONFLICT",
            "batch_bytes_differ_from_output_digest",
            outputDigest=source.output_digest,
        )
    if batch.source_binding_key != source.source_binding_key:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_INTAKE_BINDING_CONFLICT",
            "batch_binding_differs_from_work_binding",
            batchBindingKey=batch.source_binding_key,
        )
    record_bindings = tuple(
        item for item in source.input_artifacts if item.role == _SOURCE_RECORD_ROLE
    )
    if len(record_bindings) != 1:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_INTAKE_ARTIFACT_AMBIGUOUS",
            "mapping_input_source_record_missing_or_ambiguous",
            sourceRecordArtifactCount=len(record_bindings),
        )
    if record_bindings[0].artifact_id != batch.source_record_artifact_id:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_INTAKE_ARTIFACT_CONFLICT",
            "batch_names_another_source_record_artifact",
            batchArtifactId=str(batch.source_record_artifact_id),
            workArtifactId=str(record_bindings[0].artifact_id),
        )
    campaign_key = _snapshot_campaign_key(source)
    # One logical source entity is one candidate: the campaign, the exact source binding and
    # the source's own canonical record ID identify it, and nothing about the run or the
    # retrieval instant does. Acquiring the same entity again therefore addresses the same
    # candidate and appends a revision instead of creating a second, unrelated candidate.
    candidate_id = source_entity_candidate_id(
        campaign_key=campaign_key,
        source_binding_key=source.source_binding_key,
        source_record_id=batch.source_record_id,
    )
    return CandidateIntakeAction(
        candidate_id=candidate_id,
        run_id=source.run_id,
        source_work_unit_id=source.source_work_unit_id,
        observation_batch_digest=source.output_digest,
        provenance=SourceRecordIntakeProvenance(
            source_key=batch.source_key,
            source_binding_key=batch.source_binding_key,
            campaign_key=campaign_key,
            source_record_id=batch.source_record_id,
            source_policy_digest=batch.source_policy_digest,
            dataset_release=batch.dataset_release,
            mapping_revision=batch.mapping_revision,
            acquisition_identity=source_acquisition_identity(
                source_record_id=batch.source_record_id,
                dataset_release=batch.dataset_release,
                source_policy_digest=batch.source_policy_digest,
                mapping_revision=batch.mapping_revision,
                observation_profile_digest=batch.observation_profile_digest,
                source_record_artifact_digest=batch.source_record_artifact_digest,
            ),
            source_record_artifact=record_bindings[0],
        ),
        source_artifact_ids=tuple(
            sorted(
                {
                    *(item.artifact_id for item in source.input_artifacts),
                    batch.source_record_artifact_id,
                }
            )
        ),
    )


def _boundary_admitted_osm_elements(
    elements: tuple[OsmSourceRecord, ...],
    *,
    source: SucceededWorkOutput,
    bundle_content: bytes | None,
) -> tuple[tuple[OsmSourceRecord, ...], tuple[str, ...]]:
    """Apply the exact campaign boundary, because a polygon query cannot express holes.

    Returns the admitted elements and the identities excluded because they fall inside an
    interior hole. Elements are kept when the campaign declares no boundary, so the filter
    can never silently drop data it has no authority to judge.
    """

    if bundle_content is None:
        return elements, ()
    try:
        bundle = _decode_campaign_bundle(bundle_content)
    except (TypeError, ValueError):
        return elements, ()
    if "geography.yaml" not in bundle:
        return elements, ()
    try:
        geography = GeographyDocument.model_validate(bundle["geography.yaml"])
        # Bundle components carry decoded content objects, so the boundary arrives as the
        # GeoJSON document itself. Re-encoding it reproduces the canonical bytes exactly.
        boundary_payload = bundle[geography.boundary_artifact_path]
        if isinstance(boundary_payload, bytes):
            boundary_bytes = boundary_payload
        elif isinstance(boundary_payload, dict):
            boundary_bytes = _canonical_json_bytes(boundary_payload)
        else:
            return elements, ()
        boundary_artifact = decode_boundary_geojson(boundary_bytes)
        boundary = json.loads(boundary_artifact.canonical_geojson)
    except (KeyError, TypeError, ValueError):
        return elements, ()

    admitted: list[OsmSourceRecord] = []
    excluded: list[str] = []
    for element in elements:
        if boundary_contains(boundary, element.latitude, element.longitude):
            admitted.append(element)
        else:
            excluded.append(element.source_record_id)
    if not admitted and excluded:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_ALL_ELEMENTS_OUTSIDE_BOUNDARY",
            "every_element_fell_inside_a_boundary_hole",
            sourceRecordCount=len(excluded),
            boundaryPath=geography.boundary_artifact_path,
        )
    return tuple(admitted), tuple(excluded)


def _deduplicated_osm_elements(
    elements: tuple[OsmSourceRecord, ...],
) -> tuple[OsmSourceRecord, ...]:
    """Drop repeated element identities, keeping the first occurrence in envelope order."""

    seen: set[tuple[str, int]] = set()
    unique: list[OsmSourceRecord] = []
    for element in elements:
        identity = (str(element.element.element_type), int(element.element.osm_id))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(element)
    return tuple(unique)


def _plan_osm_source_record_mapping(
    source: SucceededWorkOutput,
) -> tuple[SourceRecordMappingAction, ...]:
    if source.source_binding_key is None:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_BINDING_MISSING",
            "osm_work_has_no_source_binding",
        )
    raw_bindings = tuple(
        item for item in source.related_output_artifacts if item.role == _OSM_RAW_RESPONSE_ROLE
    )
    if len(raw_bindings) != 1:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_RAW_AMBIGUOUS",
            "osm_observation_artifact_missing_or_ambiguous",
            observationArtifactCount=len(raw_bindings),
        )
    if source.output_content is None:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_RAW_UNREADABLE",
            "osm_observation_content_unavailable",
        )
    raw_artifact = raw_bindings[0]
    if _digest_bytes(source.output_content) != raw_artifact.content_digest:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_RAW_TAMPERED",
            "observation_bytes_differ_from_artifact_digest",
            observationArtifactId=str(raw_artifact.artifact_id),
        )
    query_bindings = tuple(
        item for item in source.input_artifacts if item.role == "overpass_query_spec"
    )
    if len(query_bindings) != 1:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_QUERY_MISSING",
            "osm_query_specification_missing_or_ambiguous",
            queryArtifactCount=len(query_bindings),
        )
    binding, policy_digest, campaign = _snapshot_source_binding(
        source,
        code="PIPELINE_SOURCE_RECORD_SNAPSHOT_INVALID",
        expected_capability="openstreetmap_query",
    )
    try:
        envelope = decode_osm_envelope(source.output_content)
    except (TypeError, ValueError) as exc:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_RAW_INVALID",
            type(exc).__name__,
            observationArtifactId=str(raw_artifact.artifact_id),
        ) from exc
    # Adjacent query partitions share a seam, so the same OSM element can be returned
    # by more than one unit. Identity is (elementType, osmId); a duplicate must map
    # once, never twice.
    elements, excluded_by_boundary = _boundary_admitted_osm_elements(
        _deduplicated_osm_elements(envelope.elements),
        source=source,
        bundle_content=source.campaign_bundle_content,
    )
    if not elements:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_NO_ELEMENTS",
            "osm_observations_contains_no_elements",
            observationArtifactId=str(raw_artifact.artifact_id),
        )
    if len(elements) > _OSM_MAX_MAPPING_UNITS:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_FANOUT_EXCEEDED",
            "osm_observations_exceeds_mapping_unit_limit",
            elementCount=len(elements),
            limit=_OSM_MAX_MAPPING_UNITS,
        )
    entity_kind, field_projection = _source_mapping_policy(binding)
    profile_payload = _mapping_profile_payload(
        run_id=str(source.run_id),
        binding_key=binding.key,
        source_key=binding.source_key,
        mapping_revision=_OSM_MAPPING_REVISION,
        source_policy_digest=policy_digest,
        campaign_key=campaign.campaign_key,
        entity_kind=entity_kind,
        field_projection=field_projection,
        field_aliases=_source_tag_aliases(binding),
    )
    profile_digest = _digest_bytes(profile_payload)
    actions: list[SourceRecordMappingAction] = []
    unrepresentable: list[str] = []
    for element in elements:
        try:
            actions.append(
                _osm_element_mapping_action(
                    source,
                    binding=binding,
                    policy_digest=policy_digest,
                    raw_artifact=raw_artifact,
                    query_artifact_id=query_bindings[0].artifact_id,
                    profile_payload=profile_payload,
                    profile_digest=profile_digest,
                    element=element,
                )
            )
        except ValueError:
            # One unrepresentable element must not discard its valid siblings; the
            # skipped records stay visible on the planned transition instead.
            unrepresentable.append(element.source_record_id)
    if not actions and unrepresentable:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_ALL_ELEMENTS_UNREPRESENTABLE",
            "ValueError",
            sourceRecordIds=",".join(unrepresentable),
        )
    if unrepresentable or excluded_by_boundary:
        actions[-1] = replace(
            actions[-1],
            unrepresentable_source_record_ids=tuple(unrepresentable),
            excluded_by_boundary_record_ids=tuple(excluded_by_boundary),
        )
    return tuple(actions)


def _osm_element_mapping_action(
    source: SucceededWorkOutput,
    *,
    binding: SourceBinding,
    policy_digest: str,
    raw_artifact: ArtifactIdentity,
    query_artifact_id: UUID,
    profile_payload: bytes,
    profile_digest: str,
    element: OsmSourceRecord,
) -> SourceRecordMappingAction:
    """One deterministic mapping unit per eligible OSM element.

    The unit identity is derived from the element identity itself, so replay of the
    same query result yields the same units and a duplicate element cannot fan out
    twice.
    """
    source_record_id = element.source_record_id
    record_bytes = canonical_osm_source_record_json(element).encode("utf-8")
    record_digest = _digest_bytes(record_bytes)
    work_id = uuid5(
        _SOURCE_RECORD_MAPPING_WORK_NAMESPACE,
        "|".join(
            (
                str(source.run_id),
                binding.key,
                source_record_id,
                raw_artifact.content_digest,
                record_digest,
                profile_digest,
            )
        ),
    )
    semantic_key = _digest_bytes(
        _canonical_json_bytes(
            {
                "capability": WorkCapability.SOURCE_RECORD_MAPPING.value,
                "runId": str(source.run_id),
                "sourceBindingKey": binding.key,
                "sourceRecordId": source_record_id,
                "sourceRecordArtifactDigest": record_digest,
                "querySpecArtifactId": str(query_artifact_id),
                "mappingProfileDigest": profile_digest,
            }
        )
    )
    record_artifact = ArtifactIdentity(
        artifact_id=uuid5(_SOURCE_RECORD_ARTIFACT_NAMESPACE, element.record_digest),
        role=_SOURCE_RECORD_ROLE,
        content_digest=record_digest,
        size_bytes=len(record_bytes),
        content_type="application/vnd.collection.osm-source-record+json",
    )
    return SourceRecordMappingAction(
        work_id=work_id,
        source_key=binding.source_key,
        source_binding_key=binding.key,
        source_policy_digest=policy_digest,
        source_record_id=source_record_id,
        dataset_release=_OSM_DATASET_RELEASE,
        source_record_artifact=record_artifact,
        mapping_profile_payload=profile_payload,
        source_record_payload=record_bytes,
        raw_response_artifact=raw_artifact,
        mapping_profile_digest=profile_digest,
        semantic_key=semantic_key,
        input_digest=semantic_key,
    )


def _plan_wikidata_source_record_mapping(
    source: SucceededWorkOutput,
) -> tuple[SourceRecordMappingAction, ...]:
    """One deterministic mapping unit per verified Wikidata entity.

    The result envelope is re-verified against the exact raw response and the declared
    query specification, so an entity document cannot enter the pipeline unless it came
    from the response this work actually acquired for the set it asked for.
    """

    if source.source_binding_key is None:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_BINDING_MISSING",
            "wikidata_work_has_no_source_binding",
        )
    raw_bindings = tuple(
        item for item in source.related_output_artifacts if item.role == _WIKIDATA_RAW_ROLE
    )
    if len(raw_bindings) != 1:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_RAW_AMBIGUOUS",
            "wikidata_raw_response_artifact_missing_or_ambiguous",
            rawArtifactCount=len(raw_bindings),
        )
    raw_artifact = raw_bindings[0]
    if source.output_content is None:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_RAW_UNREADABLE",
            "wikidata_result_envelope_unavailable",
        )
    if _digest_bytes(source.output_content) != source.output_artifact.content_digest:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_RAW_TAMPERED",
            "result_envelope_bytes_differ_from_artifact_digest",
            envelopeArtifactId=str(source.output_artifact.artifact_id),
        )
    spec_bindings = tuple(
        item for item in source.input_artifacts if item.role == _WIKIDATA_QUERY_ROLE
    )
    if len(spec_bindings) != 1:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_QUERY_MISSING",
            "wikidata_query_specification_missing_or_ambiguous",
            queryArtifactCount=len(spec_bindings),
        )
    spec_artifact = spec_bindings[0]
    if source.wikidata_query_spec_content is None:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_QUERY_UNREADABLE",
            "wikidata_query_specification_content_unavailable",
            queryArtifactId=str(spec_artifact.artifact_id),
        )
    if _digest_bytes(source.wikidata_query_spec_content) != spec_artifact.content_digest:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_QUERY_TAMPERED",
            "query_specification_bytes_differ_from_artifact_digest",
            queryArtifactId=str(spec_artifact.artifact_id),
        )
    try:
        spec = decode_wikidata_query_spec(source.wikidata_query_spec_content)
    except (TypeError, ValueError) as exc:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_QUERY_INVALID",
            type(exc).__name__,
            queryArtifactId=str(spec_artifact.artifact_id),
        ) from exc
    binding, policy_digest, campaign = _snapshot_source_binding(
        source,
        code="PIPELINE_SOURCE_RECORD_SNAPSHOT_INVALID",
        expected_capability="wikidata_entity_query",
    )
    try:
        envelope = decode_wikidata_entity_result(source.output_content)
    except ValueError as exc:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_RAW_INVALID",
            type(exc).__name__,
            envelopeArtifactId=str(source.output_artifact.artifact_id),
        ) from exc
    if envelope.response_digest != raw_artifact.content_digest:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_RAW_TAMPERED",
            "result_envelope_describes_another_response",
            rawArtifactId=str(raw_artifact.artifact_id),
        )
    if envelope.requested_entity_ids != spec.entity_ids:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_ENTITY_SET_CONFLICT",
            "wikidata_result_answers_another_entity_set",
            declaredEntityCount=len(spec.entity_ids),
            returnedEntityCount=len(envelope.requested_entity_ids),
        )
    if spec.binding_key != binding.key or spec.run_id != source.run_id:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_QUERY_CONFLICT",
            "wikidata_query_specification_belongs_to_another_run_binding",
            sourceBindingKey=binding.key,
        )
    if envelope.records[0].campaign_category != spec.classification.campaign_category or any(
        item.campaign_category != spec.classification.campaign_category for item in envelope.records
    ):
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_CLASSIFICATION_CONFLICT",
            "wikidata_records_use_another_campaign_category",
            sourceBindingKey=binding.key,
        )
    entity_kind, field_projection = _source_mapping_policy(binding)
    profile_payload = _mapping_profile_payload(
        run_id=str(source.run_id),
        binding_key=binding.key,
        source_key=binding.source_key,
        mapping_revision=_WIKIDATA_MAPPING_REVISION,
        source_policy_digest=policy_digest,
        campaign_key=campaign.campaign_key,
        entity_kind=entity_kind,
        field_projection=field_projection,
        field_aliases=_source_tag_aliases(binding),
    )
    profile_digest = _digest_bytes(profile_payload)
    if len(envelope.records) > _WIKIDATA_MAX_MAPPING_UNITS:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_FANOUT_EXCEEDED",
            "wikidata_result_exceeds_mapping_unit_limit",
            recordCount=len(envelope.records),
            limit=_WIKIDATA_MAX_MAPPING_UNITS,
        )
    actions = tuple(
        _wikidata_entity_mapping_action(
            source,
            binding=binding,
            policy_digest=policy_digest,
            raw_artifact=raw_artifact,
            query_artifact_id=spec_artifact.artifact_id,
            profile_payload=profile_payload,
            profile_digest=profile_digest,
            record=record,
        )
        for record in envelope.records
    )
    if envelope.rejected_entities:
        # An entity the acquisition itself refused stays visible on the planned
        # transition instead of vanishing from the run.
        actions = (
            *actions[:-1],
            replace(
                actions[-1],
                unrepresentable_source_record_ids=tuple(
                    f"wikidata:{item.entity_id}" for item in envelope.rejected_entities
                ),
            ),
        )
    return actions


def _wikidata_entity_mapping_action(
    source: SucceededWorkOutput,
    *,
    binding: SourceBinding,
    policy_digest: str,
    raw_artifact: ArtifactIdentity,
    query_artifact_id: UUID,
    profile_payload: bytes,
    profile_digest: str,
    record: WikidataAdaptedRecordEntry,
) -> SourceRecordMappingAction:
    """One deterministic mapping unit per entity identity and accepted revision."""

    source_record_id = record.source_record_id
    record_digest = record.record_digest
    work_id = uuid5(
        _SOURCE_RECORD_MAPPING_WORK_NAMESPACE,
        "|".join(
            (
                str(source.run_id),
                binding.key,
                source_record_id,
                raw_artifact.content_digest,
                record_digest,
                profile_digest,
            )
        ),
    )
    semantic_key = _digest_bytes(
        _canonical_json_bytes(
            {
                "capability": WorkCapability.SOURCE_RECORD_MAPPING.value,
                "runId": str(source.run_id),
                "sourceBindingKey": binding.key,
                "sourceRecordId": source_record_id,
                "sourceRecordArtifactDigest": record_digest,
                "querySpecArtifactId": str(query_artifact_id),
                "mappingProfileDigest": profile_digest,
            }
        )
    )
    record_artifact = ArtifactIdentity(
        # The producer sealed this identity into the record itself, so publishing it under
        # any other id would break the mapping owner's own binding check.
        artifact_id=record.artifact_record_id,
        role=_SOURCE_RECORD_ROLE,
        content_digest=record_digest,
        size_bytes=len(record.document_bytes),
        content_type="application/vnd.collection.wikidata-source-record+json",
    )
    return SourceRecordMappingAction(
        work_id=work_id,
        source_key=binding.source_key,
        source_binding_key=binding.key,
        source_policy_digest=policy_digest,
        source_record_id=source_record_id,
        dataset_release=record.dataset_release,
        source_record_artifact=record_artifact,
        mapping_profile_payload=profile_payload,
        source_record_payload=record.document_bytes,
        raw_response_artifact=raw_artifact,
        mapping_profile_digest=profile_digest,
        semantic_key=semantic_key,
        input_digest=semantic_key,
    )


class _CandidateIntakePlanningError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        required_action: str,
        context: Mapping[str, object],
    ) -> None:
        self.code = code
        self.message = message
        self.required_action = required_action
        self.context = dict(context)
        super().__init__(message)


def _candidate_intake_input_error(
    code: str,
    reason: str,
    **context: object,
) -> _CandidateIntakePlanningError:
    return _CandidateIntakePlanningError(
        code=code,
        message="Exact observation batch evidence does not authorize candidate intake.",
        required_action=(
            "Repair the normalization output or its manual-record lineage in the owning "
            "stage before retrying candidate intake."
        ),
        context={"reason": reason, **context},
    )


class _NormalizationPlanningError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        required_action: str,
        context: Mapping[str, object],
    ) -> None:
        self.code = code
        self.message = message
        self.required_action = required_action
        self.context = dict(context)
        super().__init__(message)


def _normalization_input_error(
    code: str,
    reason: str,
    **context: object,
) -> _NormalizationPlanningError:
    return _NormalizationPlanningError(
        code=code,
        message="Exact extraction evidence and run configuration do not authorize normalization.",
        required_action=(
            "Correct the extraction result or publish a compatible normalization profile in a "
            "new campaign snapshot."
        ),
        context={"reason": reason, **context},
    )


def _plan_candidate_intake(source: SucceededWorkOutput) -> CandidateIntakeAction:
    lineage_bindings = tuple(
        item for item in source.input_artifacts if item.role == "manual_record_lineage"
    )
    if len(lineage_bindings) != 1 or source.manual_record_lineage_content is None:
        raise _candidate_intake_input_error(
            "PIPELINE_CANDIDATE_LINEAGE_MISSING_OR_AMBIGUOUS",
            "manual_record_lineage_missing_or_ambiguous",
            lineageArtifactCount=len(lineage_bindings),
        )
    lineage_artifact = lineage_bindings[0]
    try:
        lineage_content = source.manual_record_lineage_content
        lineage = ManualRecordLineage.model_validate_json(lineage_content, strict=True)
    except (TypeError, ValueError) as exc:
        raise _candidate_intake_input_error(
            "PIPELINE_CANDIDATE_LINEAGE_INVALID",
            type(exc).__name__,
        ) from exc
    if (
        _digest_bytes(lineage_content) != lineage_artifact.content_digest
        or canonical_manual_record_lineage_bytes(lineage) != lineage_content
    ):
        raise _candidate_intake_input_error(
            "PIPELINE_CANDIDATE_LINEAGE_TAMPERED",
            "lineage_bytes_differ_from_artifact_digest",
            lineageArtifactId=str(lineage_artifact.artifact_id),
        )
    if lineage.run_id != source.run_id:
        raise _candidate_intake_input_error(
            "PIPELINE_CANDIDATE_LINEAGE_RUN_CONFLICT",
            "lineage_run_differs_from_succeeded_work_run",
            lineageRunId=str(lineage.run_id),
            workRunId=str(source.run_id),
        )
    if (
        lineage.manual_record_artifact_id == lineage.source_artifact_id
        or not lineage.manual_record_artifact_digest
    ):
        raise _candidate_intake_input_error(
            "PIPELINE_CANDIDATE_LINEAGE_UNBOUND",
            "lineage_manual_record_artifact_not_in_work_inputs",
            manualRecordArtifactId=str(lineage.manual_record_artifact_id),
        )
    candidate_id = uuid5(
        _CANDIDATE_ID_NAMESPACE,
        "|".join(
            (
                str(source.run_id),
                str(lineage.manual_record_work_id),
                source.output_digest,
            )
        ),
    )
    # A manual seed record names no external canonical identifier, so its candidate stays
    # bound to the authored record and its verified lineage instead of a source record ID.
    source_artifact_ids = tuple(
        sorted(
            {
                *(item.artifact_id for item in source.input_artifacts),
                lineage.manual_record_artifact_id,
                lineage.source_artifact_id,
            }
        )
    )
    return CandidateIntakeAction(
        candidate_id=candidate_id,
        run_id=source.run_id,
        source_work_unit_id=source.source_work_unit_id,
        observation_batch_digest=source.output_digest,
        provenance=ManualRecordIntakeProvenance(
            manual_record_artifact_id=lineage.manual_record_artifact_id,
            manual_record_lineage_artifact=lineage_artifact,
        ),
        source_artifact_ids=source_artifact_ids,
        revision=0,
    )


def _plan_extracted_record_normalization(
    source: SucceededWorkOutput,
) -> NormalizationWorkAction:
    record, request, raw_artifact = _read_normalization_evidence(source)
    lineage_bindings = tuple(
        item for item in source.input_artifacts if item.role == "manual_record_lineage"
    )
    if len(lineage_bindings) != 1 or source.manual_record_lineage_content is None:
        raise _normalization_input_error(
            "PIPELINE_MANUAL_RECORD_LINEAGE_MISSING_OR_AMBIGUOUS",
            "manual_record_lineage_missing_or_ambiguous",
            lineageArtifactCount=len(lineage_bindings),
        )
    lineage_artifact = lineage_bindings[0]
    try:
        lineage_content = source.manual_record_lineage_content
        lineage = ManualRecordLineage.model_validate_json(lineage_content, strict=True)
        if (
            _digest_bytes(lineage_content) != lineage_artifact.content_digest
            or canonical_manual_record_lineage_bytes(lineage) != lineage_content
            or lineage.run_id != source.run_id
            or lineage.manual_record_content_digest != record.source_record_id
        ):
            raise ValueError("manual-record lineage differs from the exact extracted record")
    except (TypeError, ValueError) as exc:
        raise _normalization_input_error(
            "PIPELINE_MANUAL_RECORD_LINEAGE_INVALID",
            type(exc).__name__,
        ) from exc
    _verify_normalization_record_binding(source, record, request, raw_artifact)
    binding, profile, policy_digest = _snapshot_normalization_profile(source, request)
    profile_payload = _canonical_json_bytes(profile.model_dump(mode="json", by_alias=True))
    if len(profile_payload) > _NORMALIZATION_MAX_PROFILE_BYTES:
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_PROFILE_TOO_LARGE",
            "profile_exceeds_worker_input_limit",
            profileKey=binding.normalization_profile,
        )
    profile_digest = _digest_bytes(profile_payload)
    input_digest = _digest_bytes(
        _canonical_json_bytes(
            {
                "extractedRecordArtifactId": str(source.output_artifact.artifact_id),
                "extractedRecordDigest": record.content_digest,
                "normalizationProfileDigest": profile_digest,
                "manualRecordLineageArtifactId": str(lineage_artifact.artifact_id),
                "manualRecordLineageDigest": lineage_artifact.content_digest,
            }
        )
    )
    semantic_key = _digest_bytes(
        _canonical_json_bytes(
            {
                "transitionKey": _NORMALIZATION_TRANSITION_KEY,
                "runId": str(source.run_id),
                "sourceWorkUnitId": str(source.source_work_unit_id),
                "configBundleDigest": source.config_bundle_digest,
                "sourceBindingKey": binding.key,
                "sourcePolicyDigest": policy_digest,
                "extractedRecordArtifactId": str(source.output_artifact.artifact_id),
                "extractedRecordDigest": record.content_digest,
                "normalizationProfileKey": binding.normalization_profile,
                "normalizationProfileDigest": profile_digest,
                "inputDigest": input_digest,
            }
        )
    )
    return NormalizationWorkAction(
        source_binding_key=binding.key,
        source_policy_digest=policy_digest,
        work_id=uuid5(_NORMALIZATION_WORK_NAMESPACE, semantic_key),
        semantic_key=semantic_key,
        input_digest=input_digest,
        extracted_record_artifact_id=source.output_artifact.artifact_id,
        profile_payload=profile_payload,
        profile_digest=profile_digest,
        manual_record_lineage_artifact=lineage_artifact,
    )


def _require_normalization_source(source: SucceededWorkOutput) -> None:
    if (
        source.stage is not WorkStage.EXTRACTION
        or source.capability != "extraction"
        or source.output_contract != "extracted-record@1"
        or source.output_artifact.role != "extracted_record"
        or source.output_artifact.content_type != "application/vnd.collection.extracted-record+json"
        or source.output_content is None
        or source.extraction_request_content is None
        or source.campaign_bundle_content is None
        or source.config_bundle_digest is None
        or source.source_binding_key is None
    ):
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_EVIDENCE_MISSING",
            "normalization_evidence_missing",
        )


def _read_normalization_evidence(
    source: SucceededWorkOutput,
) -> tuple[ExtractedRecord, ExtractionRequest, ArtifactIdentity]:
    _require_normalization_source(source)
    if source.output_content is None or source.extraction_request_content is None:
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_EVIDENCE_MISSING",
            "normalization_payload_missing",
        )
    try:
        record = decode_extracted_record(source.output_content)
        request = ExtractionRequest.model_validate_json(
            source.extraction_request_content,
            strict=True,
        )
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_EVIDENCE_INVALID",
            type(exc).__name__,
        ) from exc
    raw_artifacts = tuple(
        item for item in source.input_artifacts if item.role == "raw_source_document"
    )
    request_artifacts = tuple(
        item for item in source.input_artifacts if item.role == "extraction_request"
    )
    if (
        len(source.input_artifacts) != 2
        or len(raw_artifacts) != 1
        or len(request_artifacts) != 1
        or _digest_bytes(source.extraction_request_content) != request_artifacts[0].content_digest
        or _canonical_json_bytes(request.model_dump(mode="json", by_alias=True))
        != source.extraction_request_content
    ):
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_INPUT_BINDING_INVALID",
            "extraction_input_artifacts_mismatch",
        )
    return record, request, raw_artifacts[0]


def _verify_normalization_record_binding(
    source: SucceededWorkOutput,
    record: ExtractedRecord,
    request: ExtractionRequest,
    raw_artifact: ArtifactIdentity,
) -> None:
    if (
        canonical_extracted_record_json(record).encode("utf-8") != source.output_content
        or record.content_digest != source.output_digest
    ):
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_OUTPUT_MISMATCH",
            "extracted_record_identity_mismatch",
        )
    if not all(
        (
            record.source_record_id == request.source_record_id,
            record.raw_artifact_digest == raw_artifact.content_digest,
            request.raw_artifact_digest == raw_artifact.content_digest,
            record.source_url == request.source_url,
            record.content_type == request.content_type,
            record.source_policy_digest == request.source_policy_digest,
            record.extractor_revision == request.extractor_revision,
            record.observed_at_utc == request.observed_at_utc,
            all(field.field_key in request.allowed_fields for field in record.fields),
            all(field.field_key not in request.prohibited_fields for field in record.fields),
            all(item.field_key in request.prohibited_fields for item in record.prohibited_fields),
        )
    ):
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_OUTPUT_IDENTITY_MISMATCH",
            "record_does_not_match_exact_request",
        )
    evidence = (
        *(item.evidence for item in record.fields),
        *(item.evidence for item in record.prohibited_fields),
        *(item.evidence for item in record.text_blocks),
        *(item.evidence for item in record.issues if item.evidence is not None),
    )
    if any(
        item.raw_artifact_digest != record.raw_artifact_digest
        or item.source_url != record.source_url
        or item.extractor_revision != record.extractor_revision
        or item.observed_at_utc != record.observed_at_utc
        for item in evidence
    ):
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_EVIDENCE_IDENTITY_MISMATCH",
            "extracted_evidence_does_not_match_record",
        )


def _snapshot_normalization_profile(
    source: SucceededWorkOutput,
    request: ExtractionRequest,
) -> tuple[SourceBinding, NormalizationProfile, str]:
    if source.campaign_bundle_content is None or source.source_binding_key is None:
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_EVIDENCE_MISSING",
            "run_snapshot_or_binding_provenance_missing",
        )
    try:
        bundle = _decode_campaign_bundle(source.campaign_bundle_content)
    except (TypeError, ValueError) as exc:
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_SNAPSHOT_INVALID",
            type(exc).__name__,
            sourceBindingKey=source.source_binding_key,
        ) from exc
    binding, policy, policy_digest = _snapshot_normalization_binding(
        bundle,
        source.source_binding_key,
        request,
    )
    profile = _snapshot_profile_for_binding(bundle, binding, policy)
    return binding, profile, policy_digest


def _snapshot_normalization_binding(
    bundle: Mapping[str, object],
    source_binding_key: str,
    request: ExtractionRequest,
) -> tuple[SourceBinding, SourcePolicy, str]:
    try:
        campaign = CampaignDocument.model_validate(bundle["campaign.yaml"])
        bindings_document = SourceBindingsDocument.model_validate(bundle["source_bindings.yaml"])
        bindings = {item.key: item for item in bindings_document.items}
        binding = bindings[source_binding_key]
        policy = _snapshot_policy(bundle, binding.source_policy_key)
    except (KeyError, TypeError, ValueError) as exc:
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_SNAPSHOT_INVALID",
            type(exc).__name__,
            sourceBindingKey=source_binding_key,
        ) from exc
    policy_digest = sha256_json(policy.model_dump(mode="json"))
    if not all(
        (
            campaign.campaign_key == bundle["campaignKey"],
            binding.key in campaign.enabled_source_bindings,
            binding.source_key == policy.source_key,
            binding.source_policy_key == policy.policy_key,
            binding.extraction_profile == request.extractor_revision,
            policy_digest == request.source_policy_digest,
            policy.allowed_fields == request.allowed_fields,
            policy.prohibited_fields == request.prohibited_fields,
        )
    ):
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_POLICY_INVALID",
            "extraction_request_differs_from_run_snapshot_policy",
            sourceBindingKey=binding.key,
        )
    return binding, policy, policy_digest


def _snapshot_profile_for_binding(
    bundle: Mapping[str, object],
    binding: SourceBinding,
    policy: SourcePolicy,
) -> NormalizationProfile:
    profile_key = binding.normalization_profile
    if profile_key is None:
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_PROFILE_MISSING",
            "binding_has_no_normalization_profile",
            sourceBindingKey=binding.key,
        )
    try:
        profiles = NormalizationProfilesDocument.model_validate(
            bundle["normalization_profiles.yaml"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_PROFILE_MISSING",
            "snapshot_has_no_valid_normalization_profiles_document",
            sourceBindingKey=binding.key,
            profileKey=profile_key,
        ) from exc
    matching_profiles = tuple(item for item in profiles.items if item.key == profile_key)
    if len(matching_profiles) != 1:
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_PROFILE_MISSING",
            "binding_profile_reference_not_resolved",
            sourceBindingKey=binding.key,
            profileKey=profile_key,
        )
    profile = matching_profiles[0].profile
    source_fields = {rule.source_field for rule in profile.field_rules}
    target_fields = {
        *(rule.target_field for rule in profile.field_rules),
        *(rule.target_field for rule in profile.boolean_pattern_rules),
    }
    if (
        not source_fields.issubset(policy.allowed_fields)
        or not target_fields.issubset(policy.allowed_fields)
        or target_fields.intersection(policy.prohibited_fields)
        or target_fields.intersection(profile.prohibited_fields)
    ):
        raise _normalization_input_error(
            "PIPELINE_NORMALIZATION_PROFILE_POLICY_INVALID",
            "profile_fields_not_permitted_by_snapshot_policy",
            sourceBindingKey=binding.key,
            profileKey=profile_key,
        )
    return profile


class _PageResultBinding(NamedTuple):
    request: ArtifactIdentity
    context: ArtifactIdentity
    robots: ArtifactIdentity
    raw: ArtifactIdentity


def _page_result_binding(
    source: SucceededWorkOutput,
    manifest: HttpAcquisitionManifest,
) -> _PageResultBinding:
    request_artifacts = tuple(
        item for item in source.input_artifacts if item.role == "http_request"
    )
    context_artifacts = tuple(
        item for item in source.input_artifacts if item.role == "website_route_context"
    )
    robots_artifacts = tuple(
        item for item in source.input_artifacts if item.role == "robots_decision"
    )
    raw_outputs = tuple(
        item for item in source.related_output_artifacts if item.role == "http_raw_response"
    )
    previous_raw_artifacts = tuple(
        item for item in source.input_artifacts if item.role == "previous_raw_artifact"
    )
    expected_raw_counts = {"fetched": (1, 0), "unchanged": (0, 1)}.get(manifest.outcome)
    if (
        source.output_artifact.role != "http_acquisition_manifest"
        or len(request_artifacts) != 1
        or len(context_artifacts) != 1
        or len(robots_artifacts) != 1
        or expected_raw_counts != (len(raw_outputs), len(previous_raw_artifacts))
    ):
        raise _route_input_error(
            code="PIPELINE_PAGE_EXTRACTION_ARTIFACT_BINDING_INVALID",
            message="The page result must bind one request, route, robots decision, and raw body.",
            reason="page_extraction_artifact_binding_invalid",
            requestCount=len(request_artifacts),
            contextCount=len(context_artifacts),
            robotsCount=len(robots_artifacts),
            rawCount=len(raw_outputs) + len(previous_raw_artifacts),
        )
    return _PageResultBinding(
        request=request_artifacts[0],
        context=context_artifacts[0],
        robots=robots_artifacts[0],
        raw=(raw_outputs + previous_raw_artifacts)[0],
    )


def _page_result_identity_mismatch(
    source: SucceededWorkOutput,
    *,
    manifest: HttpAcquisitionManifest,
    request: Mapping[str, object],
    binding: _PageResultBinding,
    route: WebsiteRouteContext,
    request_digest: str,
    context_digest: str,
) -> bool:
    page_interests = [item.model_dump(mode="json") for item in route.page_interests]
    if not (
        route.request_id == request.get("requestId") == manifest.request_id
        and route.request_digest == request_digest == binding.request.content_digest
        and binding.context.content_digest == context_digest
        and source.config_bundle_digest == route.campaign_bundle_digest
        and source.source_binding_key == route.target_binding_key
        and request.get("contract") == "official-http-request"
        and request.get("contractRevision") == "official-http-request-v1"
        and request.get("requestKind") == "page"
        and request.get("url") == str(route.source_url)
        and request.get("sourceKey") == route.source_key == manifest.source_key
        and request.get("sourcePolicyDigest")
        == route.source_policy_digest
        == manifest.source_policy_digest
        and request.get("pageInterests") == page_interests
        and manifest.request_kind == "page"
        and manifest.requested_url == request.get("url")
        and manifest.robots_allowed is True
        and manifest.robots_target_url == str(route.source_url)
        and manifest.robots_artifact_id == binding.robots.artifact_id
        and manifest.robots_decision_digest is not None
        and request.get("robotsAllowed") is True
        and request.get("robotsArtifactId") == str(binding.robots.artifact_id)
        and request.get("robotsManifestDigest") == binding.robots.content_digest
        and request.get("robotsDecisionDigest") == manifest.robots_decision_digest
    ):
        return True
    return _page_raw_body_mismatch(manifest, binding.raw)


def _page_raw_body_mismatch(manifest: HttpAcquisitionManifest, raw: ArtifactIdentity) -> bool:
    if manifest.outcome == "fetched":
        return (
            manifest.raw_artifact_digest != raw.content_digest
            or manifest.decoded_size_bytes != raw.size_bytes
        )
    return (
        manifest.reused_artifact_id != raw.artifact_id
        or manifest.reused_content_digest != raw.content_digest
    )


def _page_content_type_mismatch(manifest: HttpAcquisitionManifest, content_type: str) -> bool:
    if content_type not in {"text/html", "application/xhtml+xml"}:
        return True
    response_content_types = tuple(
        header.value.split(";", maxsplit=1)[0].strip().lower()
        for header in manifest.response_headers
        if header.name == "content-type"
    )
    if manifest.outcome == "fetched":
        return response_content_types != (content_type,)
    return response_content_types not in {(), (content_type,)}


def _plan_official_http_page_extraction(
    source: SucceededWorkOutput,
    *,
    manifest: HttpAcquisitionManifest,
    request: Mapping[str, object],
) -> ExtractionWorkAction:
    if (
        source.output_content is None
        or source.http_request_content is None
        or source.website_route_context_content is None
        or source.campaign_bundle_content is None
        or source.config_bundle_digest is None
        or source.source_binding_key is None
    ):
        raise _route_input_error(
            code="PIPELINE_PAGE_EXTRACTION_EVIDENCE_MISSING",
            message="The exact page result, route, request, and run snapshot are required.",
            reason="page_extraction_evidence_missing",
        )
    binding = _page_result_binding(source, manifest)
    try:
        route = decode_website_route_context(source.website_route_context_content)
    except ValueError as exc:
        raise _route_input_error(
            code="PIPELINE_PAGE_EXTRACTION_EVIDENCE_INVALID",
            message="The page route context violates its owner contract.",
            reason=type(exc).__name__,
        ) from exc
    request_digest = _digest_bytes(source.http_request_content)
    context_digest = _digest_bytes(source.website_route_context_content)
    if _page_result_identity_mismatch(
        source,
        manifest=manifest,
        request=request,
        binding=binding,
        route=route,
        request_digest=request_digest,
        context_digest=context_digest,
    ):
        raise _route_input_error(
            code="PIPELINE_PAGE_EXTRACTION_IDENTITY_MISMATCH",
            message="The page result differs from its routed seed or robots evidence.",
            reason="page_extraction_identity_mismatch",
            routeKey=route.route_key,
        )
    lineage_bindings = tuple(
        item for item in source.input_artifacts if item.role == "manual_record_lineage"
    )
    if len(lineage_bindings) != 1 or source.manual_record_lineage_content is None:
        raise _route_input_error(
            code="PIPELINE_MANUAL_RECORD_LINEAGE_MISSING_OR_AMBIGUOUS",
            message="Page extraction requires exactly one verified manual-record lineage input.",
            reason="manual_record_lineage_missing_or_ambiguous",
            lineageArtifactCount=len(lineage_bindings),
        )
    lineage_artifact = lineage_bindings[0]
    try:
        lineage_content = source.manual_record_lineage_content
        if _digest_bytes(lineage_content) != lineage_artifact.content_digest:
            raise ValueError("manual-record lineage bytes differ from the leased artifact digest")
        lineage = ManualRecordLineage.model_validate_json(lineage_content, strict=True)
        if (
            canonical_manual_record_lineage_bytes(lineage) != lineage_content
            or lineage.run_id != source.run_id
            or lineage.manual_record_content_digest != route.record_digest
        ):
            raise ValueError("manual-record lineage does not match the exact page run and route")
    except (TypeError, ValueError) as exc:
        raise _route_input_error(
            code="PIPELINE_MANUAL_RECORD_LINEAGE_INVALID",
            message="Page extraction has invalid or mismatched manual-record lineage.",
            reason=type(exc).__name__,
        ) from exc
    raw = binding.raw
    content_type = raw.content_type.split(";", maxsplit=1)[0].strip().lower()
    if _page_content_type_mismatch(manifest, content_type):
        raise _route_input_error(
            code="PIPELINE_PAGE_CONTENT_TYPE_UNSUPPORTED",
            message="The page result has no single, supported HTML content type.",
            reason="page_content_type_unsupported",
            contentType=content_type,
        )

    bundle = _decode_campaign_bundle(source.campaign_bundle_content)
    try:
        campaign = CampaignDocument.model_validate(bundle["campaign.yaml"])
        bindings_doc = SourceBindingsDocument.model_validate(bundle["source_bindings.yaml"])
        bindings = {item.key: item for item in bindings_doc.items}
        target_binding = bindings[route.target_binding_key]
        source_binding = bindings[route.source_binding_key]
        policy = _snapshot_policy(bundle, target_binding.source_policy_key)
    except (KeyError, TypeError, ValueError) as exc:
        raise _route_input_error(
            code="PIPELINE_PAGE_EXTRACTION_SNAPSHOT_INVALID",
            message="The exact run snapshot has no valid page route or extraction policy.",
            reason=type(exc).__name__,
        ) from exc
    routing = bindings_doc.seed_routing
    if not isinstance(routing, SeedRoutingConfigured):
        raise _route_input_error(
            code="PIPELINE_PAGE_EXTRACTION_ROUTE_MISSING",
            message="The exact run snapshot has no configured website route.",
            reason="seed_routing_not_configured",
        )
    routes = routing.routes_for_source(source_binding.key, "website")
    matching_routes = tuple(
        item
        for item in routes
        if item.key == route.route_key and item.target_binding == target_binding.key
    )
    policy_digest = sha256_json(policy.model_dump(mode="json"))
    access = policy.access
    if not isinstance(access, NetworkAccess):
        raise _route_input_error(
            code="PIPELINE_PAGE_EXTRACTION_POLICY_INVALID",
            message="The page extraction policy does not authorize network access.",
            reason="page_network_access_missing",
            routeKey=route.route_key,
        )
    if (
        campaign.campaign_key != bundle["campaignKey"]
        or source_binding.capability != "manual_import"
        or source_binding.key not in campaign.enabled_source_bindings
        or target_binding.key not in campaign.enabled_source_bindings
        or target_binding.capability != "http_fetch"
        or target_binding.source_key != route.source_key
        or target_binding.extraction_profile is None
        or len(matching_routes) != 1
        or target_binding.source_policy_key != policy.policy_key
        or policy.source_key != route.source_key
        or policy.source_type != "official_website"
        or policy.legal_status != "approved"
        or policy_digest != route.source_policy_digest
    ):
        raise _route_input_error(
            code="PIPELINE_PAGE_EXTRACTION_POLICY_INVALID",
            message="The page extraction is not authorized by its exact run snapshot.",
            reason="page_extraction_policy_invalid",
            routeKey=route.route_key,
            targetBindingKey=route.target_binding_key,
        )

    if (
        content_type not in access.content_type_allowlist
        or raw.size_bytes > access.max_decoded_bytes
    ):
        raise _route_input_error(
            code="PIPELINE_PAGE_EXTRACTION_POLICY_INVALID",
            message="The page content type or size is not authorized by its exact run snapshot.",
            reason="page_content_outside_policy",
            routeKey=route.route_key,
            contentType=content_type,
            sizeBytes=raw.size_bytes,
        )
    maximum_document_bytes = min(access.max_decoded_bytes, 64 * 1024 * 1024)
    if maximum_document_bytes < 1_024:
        raise _route_input_error(
            code="PIPELINE_PAGE_EXTRACTION_LIMIT_INVALID",
            message="The source policy limit cannot form a valid extraction request.",
            reason="page_extraction_limit_invalid",
            routeKey=route.route_key,
        )
    extraction_request = ExtractionRequest(
        source_record_id=route.record_digest,
        raw_artifact_digest=raw.content_digest,
        source_url=manifest.final_url,
        content_type=cast(Literal["text/html", "application/xhtml+xml"], content_type),
        source_policy_digest=policy_digest,
        extractor_revision=target_binding.extraction_profile,
        observed_at_utc=manifest.observed_at_utc,
        allowed_fields=policy.allowed_fields,
        prohibited_fields=policy.prohibited_fields,
        maximum_document_bytes=maximum_document_bytes,
    )
    request_payload = _canonical_json_bytes(
        extraction_request.model_dump(mode="json", by_alias=True)
    )
    input_digest = _digest_bytes(request_payload)
    semantic_key = _digest_bytes(
        _canonical_json_bytes(
            {
                "transitionKey": _PAGE_EXTRACTION_TRANSITION_KEY,
                "runId": str(source.run_id),
                "sourceWorkUnitId": str(source.source_work_unit_id),
                "configBundleDigest": route.campaign_bundle_digest,
                "recordDigest": route.record_digest,
                "routeKey": route.route_key,
                "sourceBindingKey": target_binding.key,
                "sourcePolicyDigest": policy_digest,
                "rawArtifactId": str(raw.artifact_id),
                "rawArtifactDigest": raw.content_digest,
                "manualRecordLineageArtifactId": str(lineage_artifact.artifact_id),
                "manualRecordLineageDigest": lineage_artifact.content_digest,
                "extractionRequestDigest": input_digest,
                "extractorRevision": target_binding.extraction_profile,
            }
        )
    )
    return ExtractionWorkAction(
        route_key=route.route_key,
        source_key=target_binding.source_key,
        source_binding_key=target_binding.key,
        source_policy_digest=policy_digest,
        retry_budget=policy.retry_budget,
        work_id=uuid5(_WEBSITE_ROUTE_NAMESPACE, semantic_key),
        semantic_key=semantic_key,
        input_digest=input_digest,
        request_payload=request_payload,
        raw_artifact_id=raw.artifact_id,
        manual_record_lineage_artifact=lineage_artifact,
    )


def _plan_manual_record_website_actions(
    source: SucceededWorkOutput,
) -> tuple[tuple[WebsiteRouteAction, ...], str]:
    if source.campaign_bundle_content is None or source.output_content is None:
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_INPUT_UNAVAILABLE",
            message="The exact snapshot and manual-record bytes are required for routing.",
            reason="route_input_content_missing",
        )
    bundle = _decode_campaign_bundle(source.campaign_bundle_content)
    try:
        campaign = CampaignDocument.model_validate(bundle["campaign.yaml"])
        bindings_doc = SourceBindingsDocument.model_validate(bundle["source_bindings.yaml"])
        document = decode_canonical_manual_import_record(
            source.output_content,
            expected_content_digest=source.output_digest,
            maximum_bytes=4 * 1024 * 1024,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_INPUT_INVALID",
            message="The exact snapshot or manual-record artifact violates its owner contract.",
            reason=type(exc).__name__,
        ) from exc

    parent_source = tuple(
        artifact
        for artifact in source.input_artifacts
        if any(pattern.fullmatch(artifact.role) for pattern in _MANUAL_SOURCE_ROLES)
    )
    if (
        len(parent_source) != 1
        or parent_source[0].role != document.source_artifact_role
        or parent_source[0].content_digest != document.source_digest
    ):
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_PROVENANCE_MISMATCH",
            message="The record provenance differs from the exact source artifact binding.",
            reason="source_artifact_binding_mismatch",
        )

    if source.config_bundle_digest is None:
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_SNAPSHOT_MISSING",
            message="The successful work has no exact run snapshot identity.",
            reason="config_bundle_digest_missing",
        )
    if campaign.campaign_key != bundle["campaignKey"]:
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_SNAPSHOT_INVALID",
            message="The campaign document differs from its exact bundle identity.",
            reason="campaign_key_mismatch",
        )
    bindings = {binding.key: binding for binding in bindings_doc.items}
    source_binding_key = source.source_binding_key
    source_binding = bindings.get(source_binding_key or "")
    if (
        source_binding is None
        or source_binding.capability != "manual_import"
        or source_binding_key not in campaign.enabled_source_bindings
    ):
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_PROVENANCE_MISMATCH",
            message="The record source binding is not enabled by its exact campaign snapshot.",
            reason="source_binding_not_enabled",
            sourceBindingKey=source_binding_key,
        )
    source_policy = _snapshot_policy(bundle, source_binding.source_policy_key)
    if (
        source_policy.source_key != source_binding.source_key
        or source_policy.access.kind != "manual"
        or "website" not in source_policy.allowed_fields
    ):
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_SOURCE_FIELD_FORBIDDEN",
            message="The exact source policy does not authorize the website seed field.",
            reason="source_policy_field_not_allowed",
            sourceBindingKey=source_binding.key,
        )

    routing = bindings_doc.seed_routing
    if routing.state != "configured":
        return (), "not_configured"
    routes = routing.routes_for_source(source_binding.key, "website")
    if not routes:
        return (), "configured"
    if document.record.website is None:
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_FIELD_UNAVAILABLE",
            message="The manual record has no website URL for its configured route.",
            reason="website_value_missing",
            sourceBindingKey=source_binding.key,
        )
    if len(routes) > 1 and any(route.selection != "fan_out" for route in routes):
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_AMBIGUOUS",
            message="Multiple website routes match without explicit fan-out permission.",
            reason="route_selection_ambiguous",
            routeKeys=[route.key for route in routes],
        )

    lineage_content = canonical_manual_record_lineage_bytes(
        ManualRecordLineage(
            runId=source.run_id,
            manualRecordWorkId=source.source_work_unit_id,
            manualRecordArtifactId=source.output_artifact.artifact_id,
            manualRecordArtifactDigest=source.output_artifact.content_digest,
            manualRecordContentDigest=document.content_digest,
            recordDigest=document.record_digest,
            expectedEntityKind=document.record.expected_entity_kind,
            sourceArtifactId=parent_source[0].artifact_id,
            sourceArtifactDigest=parent_source[0].content_digest,
        )
    )
    lineage_artifact = ArtifactIdentity(
        artifact_id=uuid5(
            _WEBSITE_ROUTE_NAMESPACE,
            f"manual-record-lineage:{source.output_artifact.artifact_id}",
        ),
        role="manual_record_lineage",
        content_digest=_digest_bytes(lineage_content),
        size_bytes=len(lineage_content),
        content_type="application/vnd.collection.manual-record-lineage+json",
    )
    actions: list[WebsiteRouteAction] = []
    for route in sorted(routes, key=lambda value: value.key):
        target = bindings.get(route.target_binding)
        if target is None or target.key not in campaign.enabled_source_bindings:
            raise _route_input_error(
                code="MANUAL_RECORD_ROUTE_TARGET_UNAVAILABLE",
                message="The configured website route target is not enabled in its snapshot.",
                reason="target_binding_not_enabled",
                routeKey=route.key,
            )
        policy = _snapshot_policy(bundle, target.source_policy_key)
        access = policy.access
        if not isinstance(access, NetworkAccess):
            raise _route_input_error(
                code="MANUAL_RECORD_ROUTE_TARGET_POLICY_INVALID",
                message="The route target policy does not permit network acquisition.",
                reason="target_access_not_network",
                routeKey=route.key,
                targetBindingKey=target.key,
            )
        user_agent = access.user_agent
        if not isinstance(user_agent, UserAgentConfigured):
            raise _route_input_error(
                code="MANUAL_RECORD_ROUTE_TARGET_POLICY_INVALID",
                message="The route target policy has no configured HTTP User-Agent.",
                reason="target_user_agent_not_configured",
                routeKey=route.key,
                targetBindingKey=target.key,
            )
        if (
            target.capability != "http_fetch"
            or policy.source_key != target.source_key
            or policy.source_type != "official_website"
            or policy.legal_status != "approved"
            or "GET" not in access.allowed_methods
        ):
            raise _route_input_error(
                code="MANUAL_RECORD_ROUTE_TARGET_POLICY_INVALID",
                message="The route target lacks an approved official HTTP request policy.",
                reason="target_policy_not_usable",
                routeKey=route.key,
                targetBindingKey=target.key,
            )
        origin, host = _canonical_origin(str(document.record.website))
        if not _host_allowed(host, access.allowed_host_patterns):
            raise _route_input_error(
                code="MANUAL_RECORD_ROUTE_HOST_FORBIDDEN",
                message="The seed website host is outside the route target's allowlist.",
                reason="host_not_allowed",
                routeKey=route.key,
                host=host,
            )
        if not (
            1_024 <= access.max_encoded_bytes <= 64 * 1024 * 1024
            and access.max_encoded_bytes <= access.max_decoded_bytes <= 128 * 1024 * 1024
        ):
            raise _route_input_error(
                code="MANUAL_RECORD_ROUTE_POLICY_LIMIT_INVALID",
                message="The target policy byte limits cannot form a valid HTTP request.",
                reason="byte_limit_outside_request_contract",
                routeKey=route.key,
            )
        timeout_seconds = math.ceil(access.connect_timeout_seconds + access.read_timeout_seconds)
        if not 1 <= timeout_seconds <= 180:
            raise _route_input_error(
                code="MANUAL_RECORD_ROUTE_POLICY_LIMIT_INVALID",
                message="The target policy timeout cannot form a valid HTTP request.",
                reason="timeout_outside_request_contract",
                routeKey=route.key,
            )
        policy_digest = sha256_json(policy.model_dump(mode="json"))
        request_id = uuid5(
            _WEBSITE_ROUTE_NAMESPACE,
            f"request:{source.source_work_unit_id}:{source.config_bundle_digest}:{route.key}",
        ).hex
        request: dict[str, object] = {
            "contract": "official-http-request",
            "contractRevision": "official-http-request-v1",
            "requestId": request_id,
            "sourceKey": target.source_key,
            "sourcePolicyDigest": policy_digest,
            "requestKind": "robots",
            "url": f"{origin}/robots.txt",
            "allowedOrigin": origin,
            "userAgent": user_agent.value,
            "timeoutSeconds": timeout_seconds,
            "maximumEncodedBytes": access.max_encoded_bytes,
            "maximumDecodedBytes": access.max_decoded_bytes,
            "depth": 0,
            "maximumDiscoveredUrls": 100,
            "trackingQueryParameters": [],
            "pageInterests": [],
            "robotsAllowed": None,
            "robotsArtifactId": None,
            "robotsDecisionDigest": None,
            "ifNoneMatch": None,
            "ifModifiedSince": None,
            "priorArtifactId": None,
            "priorContentDigest": None,
        }
        request_bytes = _canonical_json_bytes(request)
        input_digest = _digest_bytes(request_bytes)
        context = {
            "contract": "collector-website-route-context",
            "contractRevision": "website-route-context-v1",
            "requestId": request_id,
            "requestDigest": input_digest,
            "campaignBundleDigest": source.config_bundle_digest,
            "recordDigest": document.content_digest,
            "routeKey": route.key,
            "sourceBindingKey": source_binding.key,
            "targetBindingKey": target.key,
            "sourceKey": target.source_key,
            "sourcePolicyDigest": policy_digest,
            "sourceUrl": str(document.record.website),
            "retryBudget": policy.retry_budget,
            "pageInterests": [
                {"key": item.key, "tokens": list(item.tokens), "priority": item.priority}
                for item in route.page_interests
            ],
        }
        context_bytes = _canonical_json_bytes(context)
        context_digest = _digest_bytes(context_bytes)
        identity = {
            "runId": str(source.run_id),
            "sourceWorkUnitId": str(source.source_work_unit_id),
            "configBundleDigest": source.config_bundle_digest,
            "recordDigest": document.content_digest,
            "routeKey": route.key,
            "targetBindingKey": target.key,
            "sourceKey": target.source_key,
            "sourcePolicyDigest": policy_digest,
            "requestDigest": input_digest,
            "routeContextDigest": context_digest,
            "manualRecordLineageArtifactId": str(lineage_artifact.artifact_id),
            "manualRecordLineageDigest": lineage_artifact.content_digest,
        }
        semantic_key = _digest_bytes(
            json.dumps(
                identity,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        work_id = uuid5(_WEBSITE_ROUTE_NAMESPACE, semantic_key)
        actions.append(
            WebsiteRouteAction(
                route_key=route.key,
                target_binding_key=target.key,
                source_key=target.source_key,
                source_policy_digest=policy_digest,
                retry_budget=policy.retry_budget,
                work_id=work_id,
                semantic_key=semantic_key,
                input_digest=input_digest,
                request_payload=request_bytes,
                context_digest=context_digest,
                context_payload=context_bytes,
                manual_record_lineage_artifact=lineage_artifact,
                manual_record_lineage_content=lineage_content,
            )
        )
    return tuple(actions), "configured"


def _decode_campaign_bundle(content: bytes) -> Mapping[str, object]:
    try:
        bundle = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_SNAPSHOT_INVALID",
            message="The exact campaign bundle is not valid canonical JSON.",
            reason=type(exc).__name__,
        ) from exc
    if not isinstance(bundle, dict) or set(bundle) != {"campaignKey", "components"}:
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_SNAPSHOT_INVALID",
            message="The exact campaign bundle has an unsupported owner shape.",
            reason="bundle_shape_invalid",
        )
    if _canonical_json_bytes(bundle) != content:
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_SNAPSHOT_INVALID",
            message="The exact campaign bundle is not canonical JSON.",
            reason="bundle_not_canonical",
        )
    components = bundle["components"]
    if not isinstance(components, list):
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_SNAPSHOT_INVALID",
            message="The exact campaign bundle has no component ledger.",
            reason="component_ledger_invalid",
        )
    campaign_key = bundle["campaignKey"]
    if not isinstance(campaign_key, str):
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_SNAPSHOT_INVALID",
            message="The exact campaign bundle key is invalid.",
            reason="campaign_key_invalid",
        )
    result: dict[str, object] = {"campaignKey": campaign_key}
    paths: list[str] = []
    for component in components:
        if not isinstance(component, dict) or set(component) != {"path", "digest", "content"}:
            raise _route_input_error(
                code="MANUAL_RECORD_ROUTE_SNAPSHOT_INVALID",
                message="The exact campaign component has an invalid shape.",
                reason="component_shape_invalid",
            )
        path = component["path"]
        digest = component["digest"]
        if not isinstance(path, str) or not isinstance(digest, str):
            raise _route_input_error(
                code="MANUAL_RECORD_ROUTE_SNAPSHOT_INVALID",
                message="The exact campaign component identity is invalid.",
                reason="component_identity_invalid",
            )
        if path in result or sha256_json(component["content"]) != digest:
            raise _route_input_error(
                code="MANUAL_RECORD_ROUTE_SNAPSHOT_INVALID",
                message="A campaign component differs from its declared digest.",
                reason="component_digest_mismatch",
                path=path,
            )
        paths.append(path)
        result[path] = component["content"]
    if paths != sorted(paths):
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_SNAPSHOT_INVALID",
            message="Campaign components are not in canonical order.",
            reason="component_order_invalid",
        )
    return result


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _snapshot_policy(bundle: Mapping[str, object], policy_key: str) -> SourcePolicy:
    value = bundle.get(f"source_policies/{policy_key}.yaml")
    if not isinstance(value, Mapping):
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_POLICY_MISSING",
            message="The exact campaign snapshot lacks the route's source policy.",
            reason="policy_component_missing",
            policyKey=policy_key,
        )
    try:
        return SourcePolicy.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_POLICY_INVALID",
            message="The exact route source policy violates its owner contract.",
            reason=type(exc).__name__,
            policyKey=policy_key,
        ) from exc


def _canonical_origin(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_URL_INVALID",
            message="The manual record website URL has no valid HTTP origin.",
            reason="website_origin_invalid",
        )
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise _route_input_error(
            code="MANUAL_RECORD_ROUTE_URL_INVALID",
            message="The manual record website URL has an invalid host or port.",
            reason="website_origin_invalid",
        ) from exc
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{parsed.scheme.lower()}://{authority}", host


def _host_allowed(host: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.lower()
        if normalized == host:
            return True
        if normalized.startswith("*."):
            suffix = normalized[2:]
            if host != suffix and host.endswith(f".{suffix}"):
                return True
    return False


def _route_input_error(
    *,
    code: str,
    message: str,
    reason: str,
    **context: object,
) -> _WebsiteRoutePlanningError:
    return _WebsiteRoutePlanningError(
        code=code,
        message=message,
        required_action=(
            "Correct the exact seed, snapshot, or approved route configuration; "
            "do not infer a route."
        ),
        context={"reason": reason, **context},
    )


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _transition_plan_digest(
    source: SucceededWorkOutput,
    *,
    transition_key: str,
    disposition: PipelineTransitionDisposition,
    blocker: PipelineBlocker | None,
    website_actions: tuple[WebsiteRouteAction, ...] = (),
    extraction_actions: tuple[ExtractionWorkAction, ...] = (),
    normalization_actions: tuple[NormalizationWorkAction, ...] = (),
    candidate_intake_actions: tuple[CandidateIntakeAction, ...] = (),
    source_record_mapping_actions: tuple[SourceRecordMappingAction, ...] = (),
    resolution_snapshot_actions: tuple[ResolutionSnapshotAction, ...] = (),
) -> str:
    payload = {
        "sourceWorkUnitId": str(source.source_work_unit_id),
        "runId": str(source.run_id),
        "stageRunId": str(source.stage_run_id),
        "stage": source.stage.value,
        "capability": source.capability,
        "outputContract": source.output_contract,
        "outputDigest": source.output_digest,
        "outputArtifact": _artifact_payload(source.output_artifact),
        "inputArtifacts": [
            _artifact_payload(item)
            for item in sorted(
                source.input_artifacts,
                key=lambda value: (value.role, str(value.artifact_id)),
            )
        ],
        "transitionKey": transition_key,
        "disposition": disposition.value,
        "blocker": (
            None
            if blocker is None
            else {
                "owner": blocker.owner,
                "code": blocker.code,
                "message": blocker.message,
                "requiredAction": blocker.required_action,
                "context": _canonical_mapping(blocker.context),
            }
        ),
    }
    if transition_key in {
        "manual-record-website-routing-v1",
        "official-http-robots-routing-v1",
    }:
        payload["sourceBindingKey"] = source.source_binding_key
        payload["configBundleDigest"] = source.config_bundle_digest
        payload["websiteActions"] = [_website_action_payload(action) for action in website_actions]
    if transition_key == _PAGE_EXTRACTION_TRANSITION_KEY:
        payload["extractionActions"] = [
            _extraction_action_payload(action) for action in extraction_actions
        ]
    if transition_key == _NORMALIZATION_TRANSITION_KEY:
        payload["normalizationActions"] = [
            _normalization_action_payload(action) for action in normalization_actions
        ]
    if transition_key == _CANDIDATE_INTAKE_TRANSITION_KEY:
        payload["candidateIntakeActions"] = [
            _candidate_intake_action_payload(action) for action in candidate_intake_actions
        ]
    if transition_key in _SOURCE_RECORD_MAPPING_TRANSITION_KEYS:
        payload["sourceBindingKey"] = source.source_binding_key
        payload["configBundleDigest"] = source.config_bundle_digest
        payload["sourceRecordMappingActions"] = [
            _source_record_mapping_action_payload(action)
            for action in source_record_mapping_actions
        ]
    if transition_key == _ENTITY_RESOLUTION_TRANSITION_KEY:
        payload["resolutionSnapshotActions"] = [
            _resolution_snapshot_action_payload(action) for action in resolution_snapshot_actions
        ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def source_record_mapping_result_digest(plan: PipelineTransitionPlan) -> str:
    if (
        plan.transition_key not in _SOURCE_RECORD_MAPPING_TRANSITION_KEYS
        or not plan.source_record_mapping_actions
        or plan.disposition is not PipelineTransitionDisposition.APPLY
    ):
        raise ValueError("source-record mapping result requires a non-empty apply plan")
    return _digest_bytes(
        _canonical_json_bytes(
            {
                "transitionPlanDigest": plan.plan_digest,
                "sourceRecordMappingActions": [
                    _source_record_mapping_action_payload(action)
                    for action in plan.source_record_mapping_actions
                ],
            }
        )
    )


_CANDIDATE_INTAKE_TRANSITION_KEYS = frozenset(
    {_CANDIDATE_INTAKE_TRANSITION_KEY, _SOURCE_RECORD_INTAKE_TRANSITION_KEY}
)


def candidate_intake_result_digest(plan: PipelineTransitionPlan) -> str:
    if (
        plan.transition_key not in _CANDIDATE_INTAKE_TRANSITION_KEYS
        or not plan.candidate_intake_actions
        or plan.disposition is not PipelineTransitionDisposition.APPLY
    ):
        raise ValueError("candidate intake result requires a non-empty apply plan")
    return _digest_bytes(
        _canonical_json_bytes(
            {
                "transitionPlanDigest": plan.plan_digest,
                "candidateIntakeActions": [
                    _candidate_intake_action_payload(action)
                    for action in plan.candidate_intake_actions
                ],
            }
        )
    )


def _candidate_intake_action_payload(action: CandidateIntakeAction) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidateId": str(action.candidate_id),
        "runId": str(action.run_id),
        "sourceWorkUnitId": str(action.source_work_unit_id),
        "observationBatchDigest": action.observation_batch_digest,
        "sourceArtifactIds": [str(item) for item in sorted(action.source_artifact_ids)],
        "revision": action.revision,
    }
    manual = action.manual_record_provenance
    source_record = action.source_record_provenance
    if manual is not None:
        payload["provenanceKind"] = "manual_record"
        payload["manualRecordArtifactId"] = str(manual.manual_record_artifact_id)
        payload["manualRecordLineageArtifact"] = _artifact_payload(
            manual.manual_record_lineage_artifact
        )
    elif source_record is not None:
        payload["provenanceKind"] = "source_record"
        payload["sourceKey"] = source_record.source_key
        payload["sourceBindingKey"] = source_record.source_binding_key
        payload["sourceRecordId"] = source_record.source_record_id
        payload["sourcePolicyDigest"] = source_record.source_policy_digest
        payload["datasetRelease"] = source_record.dataset_release
        payload["mappingRevision"] = source_record.mapping_revision
        payload["sourceRecordArtifact"] = _artifact_payload(source_record.source_record_artifact)
    else:
        raise ValueError("candidate intake action has no provenance")
    return payload


def website_route_result_digest(plan: PipelineTransitionPlan) -> str:
    if (
        plan.transition_key
        not in {
            "manual-record-website-routing-v1",
            "official-http-robots-routing-v1",
        }
        or not plan.website_actions
        or plan.disposition is not PipelineTransitionDisposition.APPLY
    ):
        raise ValueError("website routing result requires a non-empty apply plan")
    return _digest_bytes(
        _canonical_json_bytes(
            {
                "transitionPlanDigest": plan.plan_digest,
                "websiteActions": [
                    _website_action_payload(action) for action in plan.website_actions
                ],
            }
        )
    )


def extraction_work_result_digest(plan: PipelineTransitionPlan) -> str:
    if (
        plan.transition_key != _PAGE_EXTRACTION_TRANSITION_KEY
        or not plan.extraction_actions
        or plan.disposition is not PipelineTransitionDisposition.APPLY
    ):
        raise ValueError("page extraction result requires a non-empty apply plan")
    return _digest_bytes(
        _canonical_json_bytes(
            {
                "transitionPlanDigest": plan.plan_digest,
                "extractionActions": [
                    _extraction_action_payload(action) for action in plan.extraction_actions
                ],
            }
        )
    )


def normalization_work_result_digest(plan: PipelineTransitionPlan) -> str:
    if (
        plan.transition_key != _NORMALIZATION_TRANSITION_KEY
        or not plan.normalization_actions
        or plan.disposition is not PipelineTransitionDisposition.APPLY
    ):
        raise ValueError("normalization result requires a non-empty apply plan")
    return _digest_bytes(
        _canonical_json_bytes(
            {
                "transitionPlanDigest": plan.plan_digest,
                "normalizationActions": [
                    _normalization_action_payload(action) for action in plan.normalization_actions
                ],
            }
        )
    )


def _website_action_payload(value: WebsiteRouteAction) -> dict[str, object]:
    return {
        "routeKey": value.route_key,
        "targetBindingKey": value.target_binding_key,
        "sourceKey": value.source_key,
        "sourcePolicyDigest": value.source_policy_digest,
        "retryBudget": value.retry_budget,
        "workId": str(value.work_id),
        "semanticKey": value.semantic_key,
        "inputDigest": value.input_digest,
        "contextDigest": value.context_digest,
        "robotsDecisionArtifactId": (
            str(value.robots_decision_artifact_id)
            if value.robots_decision_artifact_id is not None
            else None
        ),
        "robotsRawArtifactId": (
            str(value.robots_raw_artifact_id) if value.robots_raw_artifact_id is not None else None
        ),
        "manualRecordLineageArtifactId": (
            str(value.manual_record_lineage_artifact.artifact_id)
            if value.manual_record_lineage_artifact is not None
            else None
        ),
        "manualRecordLineageDigest": (
            value.manual_record_lineage_artifact.content_digest
            if value.manual_record_lineage_artifact is not None
            else None
        ),
        "manualRecordLineageContentDigest": (
            _digest_bytes(value.manual_record_lineage_content)
            if value.manual_record_lineage_content is not None
            else None
        ),
    }


def _extraction_action_payload(value: ExtractionWorkAction) -> dict[str, object]:
    return {
        "routeKey": value.route_key,
        "sourceKey": value.source_key,
        "sourceBindingKey": value.source_binding_key,
        "sourcePolicyDigest": value.source_policy_digest,
        "retryBudget": value.retry_budget,
        "workId": str(value.work_id),
        "semanticKey": value.semantic_key,
        "inputDigest": value.input_digest,
        "requestDigest": _digest_bytes(value.request_payload),
        "rawArtifactId": str(value.raw_artifact_id),
    }


def _normalization_action_payload(value: NormalizationWorkAction) -> dict[str, object]:
    return {
        "sourceBindingKey": value.source_binding_key,
        "sourcePolicyDigest": value.source_policy_digest,
        "workId": str(value.work_id),
        "semanticKey": value.semantic_key,
        "inputDigest": value.input_digest,
        "extractedRecordArtifactId": str(value.extracted_record_artifact_id),
        "profileDigest": value.profile_digest,
    }


def _snapshot_source_binding(
    source: SucceededWorkOutput,
    *,
    code: str,
    expected_capability: str,
) -> tuple[SourceBinding, str, CampaignDocument]:
    """Resolve the exact run snapshot binding and policy digest for a source record."""

    if source.campaign_bundle_content is None or source.source_binding_key is None:
        raise _source_record_mapping_error(
            code,
            "run_snapshot_or_binding_provenance_missing",
        )
    try:
        bundle = _decode_campaign_bundle(source.campaign_bundle_content)
        campaign = CampaignDocument.model_validate(bundle["campaign.yaml"])
        bindings_document = SourceBindingsDocument.model_validate(bundle["source_bindings.yaml"])
        bindings = {item.key: item for item in bindings_document.items}
        binding = bindings[source.source_binding_key]
        policy = SourcePolicy.model_validate(
            bundle[f"source_policies/{binding.source_policy_key}.yaml"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _source_record_mapping_error(
            code,
            type(exc).__name__,
            sourceBindingKey=source.source_binding_key,
        ) from exc
    policy_digest = sha256_json(policy.model_dump(mode="json"))
    if not all(
        (
            campaign.campaign_key == bundle["campaignKey"],
            binding.key in campaign.enabled_source_bindings,
            binding.capability == expected_capability,
            binding.source_key == policy.source_key,
            binding.source_policy_key == policy.policy_key,
        )
    ):
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_BINDING_INVALID",
            "source_binding_differs_from_run_snapshot",
            sourceBindingKey=binding.key,
            sourceCapability=binding.capability,
        )
    if binding.seed_provider.kind != "not_applicable":
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_BINDING_INVALID",
            "discovery_binding_depends_on_a_seed_file",
            sourceBindingKey=binding.key,
        )
    return binding, policy_digest, CampaignDocument.model_validate(bundle["campaign.yaml"])


def _mapping_profile_payload(
    *,
    run_id: str,
    binding_key: str,
    source_key: str,
    mapping_revision: str,
    source_policy_digest: str,
    campaign_key: str,
    entity_kind: str | None,
    field_projection: Mapping[str, str],
    field_aliases: Mapping[str, tuple[str, ...]] | None = None,
) -> bytes:
    payload: dict[str, object] = {
        "campaignKey": campaign_key,
        "entityKind": entity_kind,
        "fieldProjection": dict(sorted(field_projection.items())),
        "mappingRevision": mapping_revision,
        "runId": run_id,
        "sourceBindingKey": binding_key,
        "sourceKey": source_key,
        "sourcePolicyDigest": source_policy_digest,
    }
    if field_aliases:
        # Additive: a campaign that declares no aliases keeps the exact original profile
        # shape, so existing profile digests and therefore candidate identities hold.
        payload["fieldAliases"] = {key: list(value) for key, value in sorted(field_aliases.items())}
    return _canonical_json_bytes(payload)


def _source_mapping_policy(binding: SourceBinding) -> tuple[str | None, Mapping[str, str]]:
    """Resolve the source-native mapping decisions from the immutable binding.

    Ambiguity stays ambiguous: an unset entity kind resolves to None so intake leaves
    the candidate unresolved rather than defaulting to a kind the source never proved.
    """
    projection = {
        item.source_field: item.candidate_field for item in binding.source_field_projection
    }
    if not projection:
        raise _source_record_mapping_error(
            "PIPELINE_SOURCE_RECORD_PROJECTION_MISSING",
            "binding_declares_no_source_field_projection",
            sourceBindingKey=binding.key,
        )
    return binding.source_entity_kind, projection


def _source_tag_aliases(binding: SourceBinding) -> Mapping[str, tuple[str, ...]]:
    """Declared source tags per projected field, primary first.

    Only fields that declare aliases appear here; a projection without aliases reads its
    own source tag only, so existing campaigns keep their exact behaviour.
    """

    return {
        item.source_field: item.source_tags
        for item in binding.source_field_projection
        if item.source_aliases
    }


def _source_record_mapping_action_payload(
    value: SourceRecordMappingAction,
) -> dict[str, object]:
    return {
        "workId": str(value.work_id),
        "sourceKey": value.source_key,
        "sourceBindingKey": value.source_binding_key,
        "sourcePolicyDigest": value.source_policy_digest,
        "sourceRecordId": value.source_record_id,
        "datasetRelease": value.dataset_release,
        "sourceRecordArtifact": _artifact_payload(value.source_record_artifact),
        "mappingProfileDigest": value.mapping_profile_digest,
        "semanticKey": value.semantic_key,
        "inputDigest": value.input_digest,
        # Records the envelope carried but could not represent. They are part of the
        # durable result digest so a partial fetch can never be replayed as complete
        # coverage.
        "unrepresentableSourceRecordIds": list(value.unrepresentable_source_record_ids),
        "excludedByBoundaryRecordIds": list(value.excluded_by_boundary_record_ids),
    }


def _resolution_snapshot_action_payload(
    value: ResolutionSnapshotAction,
) -> dict[str, object]:
    return {
        "batchId": str(value.batch_id),
        "batchDigest": value.batch_digest,
        "snapshotDigest": value.snapshot_digest,
        "clusterIds": [str(item) for item in value.cluster_ids],
        "memberCandidateIds": [str(item) for item in value.member_candidate_ids],
        "exportEligibleClusterCount": value.export_eligible_cluster_count,
        "pendingReviewPairCount": value.pending_review_pair_count,
    }


def resolution_snapshot_result_digest(plan: PipelineTransitionPlan) -> str:
    if (
        plan.transition_key != _ENTITY_RESOLUTION_TRANSITION_KEY
        or not plan.resolution_snapshot_actions
        or plan.disposition is not PipelineTransitionDisposition.APPLY
    ):
        raise ValueError("resolution snapshot result requires a non-empty apply plan")
    return _digest_bytes(
        _canonical_json_bytes(
            {
                "transitionPlanDigest": plan.plan_digest,
                "resolutionSnapshotActions": [
                    _resolution_snapshot_action_payload(action)
                    for action in plan.resolution_snapshot_actions
                ],
            }
        )
    )


def _artifact_payload(value: ArtifactIdentity) -> dict[str, object]:
    return {
        "artifactId": str(value.artifact_id),
        "role": value.role,
        "contentDigest": value.content_digest,
        "sizeBytes": value.size_bytes,
        "contentType": value.content_type,
    }


def _canonical_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {key: _canonical_value(item) for key, item in sorted(value.items())}


def _canonical_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return _canonical_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical pipeline value: {type(value).__name__}")


def _require_token(name: str, value: str) -> None:
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid token format")


def _require_sha256(name: str, value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be canonical SHA-256")


def _require_plain_text(name: str, value: str, *, maximum: int) -> None:
    if not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} must contain between 1 and {maximum} characters")
    if "<" in value or ">" in value:
        raise ValueError(f"{name} must not contain markup delimiters")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ValueError(f"{name} contains a forbidden control character")


def _require_aware_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
