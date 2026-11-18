from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid5

from collection_application.artifacts import ArtifactKind
from collection_application.campaign_publication import (
    CampaignSnapshotPublicationService,
    PublishCampaignSnapshot,
)
from collection_application.canonicalization import canonical_json_bytes, sha256_json
from collection_application.compiled_campaign import CompiledCampaignBundle
from collection_application.owned_artifacts import (
    OwnedArtifactPublisherService,
    PublishOwnedArtifact,
)
from collection_application.work_artifacts import WorkInputArtifact
from collection_application.work_engine import (
    CollectionRunSpec,
    SourceCapacitySpec,
    StageRunSpec,
    WorkUnitSpec,
)
from collection_contracts import SourceBinding, SourceBindingsDocument, SourcePolicy, owner_error
from collection_contracts.osm_query_spec import (
    OverpassQuerySpecDocument,
    OverpassTagFilter,
    canonical_query_spec_document,
    partition_boundary,
    query_spec_digest,
)
from collection_contracts.wikidata_entity_query_spec import (
    WikidataClassificationSpec,
    WikidataEntityQuerySpec,
    canonical_wikidata_query_spec_document,
    wikidata_query_spec_digest,
)
from collection_contracts.wikidata_entity_result import (
    RESULT_CONTRACT as WIKIDATA_RESULT_CONTRACT,
)
from collection_domain import (
    CollectionRunState,
    RetryPolicy,
    SourceOperationalState,
    StageRunState,
    WorkCapability,
    WorkStage,
)

_ACQUISITION_AUTHORIZED_STATUSES = frozenset({"approved", "reference_only", "research_only"})
# Capabilities with a concrete initial-work owner in this service.
_BOOTSTRAP_CAPABILITIES = frozenset(
    {"manual_import", "openstreetmap_query", "wikidata_entity_query"}
)
_RUN_NAMESPACE = UUID("51f52f00-9c4c-5c37-b1f8-e76df6291910")
_ARTIFACT_NAMESPACE = UUID("bf82877b-4113-5fe6-b1c8-923a7d995c8e")


@dataclass(frozen=True, slots=True)
class CreateCampaignRun:
    run_id: UUID
    campaign_key: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class CampaignRunBootstrapPlan:
    run: CollectionRunSpec
    sources: tuple[SourceCapacitySpec, ...]
    stages: tuple[StageRunSpec, ...]
    initial_work: tuple[WorkUnitSpec, ...]


@dataclass(frozen=True, slots=True)
class CampaignRunCreated:
    run_id: UUID
    campaign_key: str
    config_bundle_digest: str
    initial_work_ids: tuple[UUID, ...]


class CampaignRunStore(Protocol):
    def create(self, plan: CampaignRunBootstrapPlan) -> CampaignRunCreated: ...


class CampaignRunService:
    def __init__(
        self,
        snapshot_publication: CampaignSnapshotPublicationService,
        artifact_publisher: OwnedArtifactPublisherService,
        store: CampaignRunStore,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._snapshot_publication = snapshot_publication
        self._artifact_publisher = artifact_publisher
        self._store = store
        self._clock = clock

    def create(self, command: CreateCampaignRun) -> CampaignRunCreated:
        published = self._snapshot_publication.publish(
            PublishCampaignSnapshot(
                campaign_key=command.campaign_key,
                correlation_id=command.correlation_id,
            )
        )
        compiled = published.compiled
        snapshot = compiled.snapshot
        if snapshot.readiness != "ready":
            raise owner_error(
                error_type="collection/campaign-run-blocked",
                owner="CampaignRun",
                code="CAMPAIGN_RUN_BLOCKED",
                message="The campaign snapshot is not ready for a production run.",
                context={
                    "campaignKey": command.campaign_key,
                    "configBundleDigest": snapshot.bundle_digest,
                    "blockerCodes": [blocker.code for blocker in snapshot.blockers],
                },
                required_action="Resolve every campaign blocker and create a new snapshot.",
                correlation_id=command.correlation_id,
            )
        now_utc = self._now_utc()
        stage_specs = tuple(
            StageRunSpec(
                stage_run_id=uuid5(_RUN_NAMESPACE, f"{command.run_id}:stage:{stage.value}"),
                run_id=command.run_id,
                stage=stage,
                initial_state=(
                    StageRunState.RUNNING if stage is WorkStage.DISCOVERY else StageRunState.PENDING
                ),
                correlation_id=command.correlation_id,
            )
            for stage in WorkStage
        )
        stage_by_name = {item.stage: item for item in stage_specs}
        sources: list[SourceCapacitySpec] = []
        initial_work: list[WorkUnitSpec] = []
        enabled = set(compiled.documents.campaign.enabled_source_bindings)
        routed_http_bindings = _routed_http_bindings(
            compiled.documents.source_bindings,
            enabled=enabled,
            campaign_key=command.campaign_key,
            correlation_id=command.correlation_id,
        )
        for binding in compiled.documents.source_bindings.items:
            if binding.key not in enabled:
                continue
            policy = compiled.documents.source_policies[binding.source_policy_key]
            policy_digest = sha256_json(policy.model_dump(mode="json"))
            sources.append(_source_spec(policy, policy_digest, command.correlation_id))
            if binding.capability == "manual_import":
                if binding.seed_provider.kind != "file":
                    raise owner_error(
                        error_type="collection/manual-source-provider-invalid",
                        owner="CampaignRun",
                        code="MANUAL_SOURCE_PROVIDER_INVALID",
                        message="Manual import requires a file seed provider.",
                        context={
                            "campaignKey": command.campaign_key,
                            "sourceBinding": binding.key,
                            "providerKind": binding.seed_provider.kind,
                        },
                        required_action=(
                            "Configure a CSV, JSON, or JSONL file seed provider for the binding."
                        ),
                        correlation_id=command.correlation_id,
                    )
                initial_work.append(
                    self._manual_import_work(
                        command,
                        binding,
                        policy,
                        policy_digest,
                        compiled.raw_bundle.files[binding.seed_provider.path],
                        stage_by_name[WorkStage.DISCOVERY].stage_run_id,
                        now_utc,
                    )
                )
            elif binding.capability == "openstreetmap_query":
                initial_work.extend(
                    self._osm_discovery_work(
                        command,
                        binding,
                        policy,
                        policy_digest,
                        compiled,
                        stage_by_name[WorkStage.DISCOVERY].stage_run_id,
                        now_utc,
                    )
                )
            elif binding.capability == "wikidata_entity_query":
                initial_work.extend(
                    self._wikidata_discovery_work(
                        command,
                        binding,
                        policy,
                        policy_digest,
                        stage_by_name[WorkStage.DISCOVERY].stage_run_id,
                        now_utc,
                    )
                )
            elif binding.capability != "http_fetch" or binding.key not in routed_http_bindings:
                raise owner_error(
                    error_type="collection/source-bootstrap-unsupported",
                    owner="CampaignRun",
                    code="SOURCE_BOOTSTRAP_UNSUPPORTED",
                    message="The enabled source binding has no run bootstrap owner.",
                    context={
                        "campaignKey": command.campaign_key,
                        "sourceBinding": binding.key,
                        "capability": binding.capability,
                    },
                    required_action=(
                        "Implement the typed source bootstrap before enabling this binding."
                    ),
                    correlation_id=command.correlation_id,
                )
        plan = CampaignRunBootstrapPlan(
            run=CollectionRunSpec(
                run_id=command.run_id,
                campaign_key=command.campaign_key,
                config_bundle_digest=snapshot.bundle_digest,
                initial_state=CollectionRunState.RUNNING,
                correlation_id=command.correlation_id,
            ),
            sources=tuple(sources),
            stages=stage_specs,
            initial_work=tuple(initial_work),
        )
        return self._store.create(plan)

    def _osm_discovery_work(
        self,
        command: CreateCampaignRun,
        binding: SourceBinding,
        policy: SourcePolicy,
        policy_digest: str,
        compiled: CompiledCampaignBundle,
        stage_run_id: UUID,
        now_utc: datetime,
    ) -> tuple[WorkUnitSpec, ...]:
        """Bootstrap one deterministic OSM query spec per boundary partition."""
        if binding.capability != "openstreetmap_query" or policy.access.kind != "network":
            raise ValueError("OSM binding does not satisfy its owner contracts")
        if binding.osm_query is None:
            raise owner_error(
                error_type="collection/osm-query-spec-missing",
                owner="CampaignRun",
                code="OSM_QUERY_SPEC_MISSING",
                message="The OSM source binding declares no Overpass query.",
                context={
                    "campaignKey": command.campaign_key,
                    "sourceBinding": binding.key,
                },
                required_action="Declare osm_query in the source binding.",
                correlation_id=command.correlation_id,
            )
        geography = compiled.documents.geography
        if geography is None:
            raise owner_error(
                error_type="collection/osm-geography-missing",
                owner="CampaignRun",
                code="OSM_GEOGRAPHY_MISSING",
                message="OSM discovery requires the verified campaign boundary.",
                context={
                    "campaignKey": command.campaign_key,
                    "sourceBinding": binding.key,
                },
                required_action="Declare geography with a verified boundary artifact.",
                correlation_id=command.correlation_id,
            )
        boundary = compiled.documents.geography_artifacts.get(geography.boundary_artifact_path)
        try:
            partitions = partition_boundary(boundary)
        except (TypeError, ValueError) as exc:
            raise owner_error(
                error_type="collection/osm-boundary-unrepresentable",
                owner="CampaignRun",
                code="OSM_BOUNDARY_UNREPRESENTABLE",
                message="The verified boundary cannot be expressed as Overpass polygons.",
                context={
                    "campaignKey": command.campaign_key,
                    "sourceBinding": binding.key,
                    "boundaryPath": geography.boundary_artifact_path,
                    "cause": str(exc),
                },
                required_action=(
                    "Publish a boundary whose rings are Overpass-representable, or "
                    "partition it explicitly in the campaign."
                ),
                correlation_id=command.correlation_id,
            ) from exc
        work: list[WorkUnitSpec] = []
        for index, partition in enumerate(partitions):
            try:
                spec = OverpassQuerySpecDocument(
                    element_types=tuple(sorted(binding.osm_query.element_types)),
                    tag_filters=tuple(
                        OverpassTagFilter(key=item.key, values=tuple(sorted(item.values)))
                        for item in sorted(
                            binding.osm_query.tag_filters, key=lambda entry: entry.key
                        )
                    ),
                    polygon=partition.ring,
                    timeout_seconds=binding.osm_query.timeout_seconds,
                    maximum_elements=binding.osm_query.maximum_elements,
                )
            except ValueError as exc:
                raise owner_error(
                    error_type="collection/osm-query-spec-invalid",
                    owner="CampaignRun",
                    code="OSM_QUERY_SPEC_INVALID",
                    message="The declared Overpass query violates the connector contract.",
                    context={
                        "campaignKey": command.campaign_key,
                        "sourceBinding": binding.key,
                        "partitionIndex": index,
                        "cause": str(exc),
                    },
                    required_action="Correct osm_query in the source binding.",
                    correlation_id=command.correlation_id,
                ) from exc
            content = canonical_query_spec_document(spec)
            operation_id = uuid5(
                _ARTIFACT_NAMESPACE,
                f"{command.run_id}:{binding.key}:osm-query-spec:{index}",
            )
            artifact_id = uuid5(_ARTIFACT_NAMESPACE, f"artifact:{operation_id}")
            published = self._artifact_publisher.publish(
                PublishOwnedArtifact(
                    artifact_id=artifact_id,
                    operation_id=operation_id,
                    producer_identity="campaign-run",
                    artifact_kind=ArtifactKind.RAW_ARTIFACT,
                    content=content,
                    content_type="application/json",
                    source_policy_digest=policy_digest,
                    correlation_id=command.correlation_id,
                )
            )
            identity = {
                "contract": "osm-discovery-work-identity@2",
                "runId": str(command.run_id),
                "bindingKey": binding.key,
                "sourcePolicyDigest": policy_digest,
                "querySpecDigest": query_spec_digest(spec),
                "boundaryDigest": geography.boundary_digest,
                "partitionIndex": index,
                "partitionCount": len(partitions),
                "partitionCoversExactly": partition.covers_exactly,
                "excludedHoleCount": len(partition.excluded_holes),
                "extractionProfile": binding.extraction_profile,
            }
            semantic_bytes = canonical_json_bytes(identity)
            semantic_key = f"sha256:{sha256(semantic_bytes).hexdigest()}"
            work.append(
                WorkUnitSpec(
                    work_id=uuid5(_RUN_NAMESPACE, f"{command.run_id}:{semantic_key}"),
                    run_id=command.run_id,
                    stage_run_id=stage_run_id,
                    stage=WorkStage.DISCOVERY,
                    capability=WorkCapability.OSM_QUERY,
                    source_key=binding.source_key,
                    source_binding_key=binding.key,
                    semantic_key=semantic_key,
                    input_digest=semantic_key,
                    expected_output_contract="osm-overpass-result@1",
                    priority=index,
                    retry_policy=RetryPolicy(
                        max_attempts=policy.retry_budget + 1,
                        initial_delay_seconds=30,
                        multiplier=2,
                        max_delay_seconds=900,
                    ),
                    available_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                    input_artifacts=(
                        WorkInputArtifact(
                            artifact_id=published.artifact_id,
                            role="overpass_query_spec",
                        ),
                    ),
                )
            )
        return tuple(work)

    def _wikidata_discovery_work(
        self,
        command: CreateCampaignRun,
        binding: SourceBinding,
        policy: SourcePolicy,
        policy_digest: str,
        stage_run_id: UUID,
        now_utc: datetime,
    ) -> tuple[WorkUnitSpec, ...]:
        """Bootstrap one deterministic bounded entity read for the declared set.

        The declared set is the operator's reviewed list, so one run reads exactly those
        entities once. A replayed run publishes the same specification bytes and
        therefore the same work identity.
        """

        if binding.capability != "wikidata_entity_query" or policy.access.kind != "network":
            raise ValueError("wikidata binding does not satisfy its owner contracts")
        if binding.wikidata_query is None:
            raise owner_error(
                error_type="collection/wikidata-query-spec-missing",
                owner="CampaignRun",
                code="WIKIDATA_QUERY_SPEC_MISSING",
                message="The Wikidata source binding declares no entity query.",
                context={
                    "campaignKey": command.campaign_key,
                    "sourceBinding": binding.key,
                },
                required_action="Declare wikidata_query in the source binding.",
                correlation_id=command.correlation_id,
            )
        try:
            declaration = binding.wikidata_classification
            if declaration is None:
                raise ValueError("the Wikidata binding declares no classification map")
            spec = WikidataEntityQuerySpec(
                run_id=command.run_id,
                binding_key=binding.key,
                entity_ids=tuple(sorted(binding.wikidata_query.entity_ids)),
                label_languages=binding.wikidata_query.label_languages,
                classification=WikidataClassificationSpec(
                    studio_class_id=declaration.studio_class_id,
                    campaign_category=declaration.campaign_category,
                    compatible_class_ids=tuple(sorted(declaration.compatible_class_ids)),
                ),
                source_policy_digest=policy_digest,
            )
        except ValueError as exc:
            raise owner_error(
                error_type="collection/wikidata-query-spec-invalid",
                owner="CampaignRun",
                code="WIKIDATA_QUERY_SPEC_INVALID",
                message="The declared Wikidata query violates its bounded contract.",
                context={
                    "campaignKey": command.campaign_key,
                    "sourceBinding": binding.key,
                    "cause": str(exc),
                },
                required_action="Correct wikidata_query in the source binding.",
                correlation_id=command.correlation_id,
            ) from exc
        content = canonical_wikidata_query_spec_document(spec)
        operation_id = uuid5(
            _ARTIFACT_NAMESPACE,
            f"{command.run_id}:{binding.key}:wikidata-entity-query-spec",
        )
        artifact_id = uuid5(_ARTIFACT_NAMESPACE, f"artifact:{operation_id}")
        published = self._artifact_publisher.publish(
            PublishOwnedArtifact(
                artifact_id=artifact_id,
                operation_id=operation_id,
                producer_identity="campaign-run",
                artifact_kind=ArtifactKind.RAW_ARTIFACT,
                content=content,
                content_type="application/json",
                source_policy_digest=policy_digest,
                correlation_id=command.correlation_id,
            )
        )
        identity = {
            "contract": "wikidata-discovery-work-identity@1",
            "runId": str(command.run_id),
            "bindingKey": binding.key,
            "sourcePolicyDigest": policy_digest,
            "querySpecDigest": wikidata_query_spec_digest(spec),
            "entityCount": len(spec.entity_ids),
            "extractionProfile": binding.extraction_profile,
        }
        semantic_bytes = canonical_json_bytes(identity)
        semantic_key = f"sha256:{sha256(semantic_bytes).hexdigest()}"
        return (
            WorkUnitSpec(
                work_id=uuid5(_RUN_NAMESPACE, f"{command.run_id}:{semantic_key}"),
                run_id=command.run_id,
                stage_run_id=stage_run_id,
                stage=WorkStage.DISCOVERY,
                capability=WorkCapability.WIKIDATA_ENTITY_QUERY,
                source_key=binding.source_key,
                source_binding_key=binding.key,
                semantic_key=semantic_key,
                input_digest=semantic_key,
                expected_output_contract=WIKIDATA_RESULT_CONTRACT,
                priority=0,
                retry_policy=RetryPolicy(
                    max_attempts=policy.retry_budget + 1,
                    initial_delay_seconds=30,
                    multiplier=2,
                    max_delay_seconds=900,
                ),
                available_at_utc=now_utc,
                correlation_id=command.correlation_id,
                input_artifacts=(
                    WorkInputArtifact(
                        artifact_id=published.artifact_id,
                        role="wikidata_entity_query_spec",
                    ),
                ),
            ),
        )

    def _manual_import_work(
        self,
        command: CreateCampaignRun,
        binding: SourceBinding,
        policy: SourcePolicy,
        policy_digest: str,
        content: bytes,
        stage_run_id: UUID,
        now_utc: datetime,
    ) -> WorkUnitSpec:
        if binding.seed_provider.kind != "file" or policy.access.kind != "manual":
            raise ValueError("manual source binding does not satisfy its owner contracts")
        operation_id = uuid5(_ARTIFACT_NAMESPACE, f"{command.run_id}:{binding.key}:source")
        artifact_id = uuid5(_ARTIFACT_NAMESPACE, f"artifact:{operation_id}")
        published = self._artifact_publisher.publish(
            PublishOwnedArtifact(
                artifact_id=artifact_id,
                operation_id=operation_id,
                producer_identity="campaign-run",
                artifact_kind=ArtifactKind.RAW_ARTIFACT,
                content=content,
                content_type=_seed_content_type(binding.seed_provider.format),
                source_policy_digest=policy_digest,
                correlation_id=command.correlation_id,
            )
        )
        mode = "partial" if policy.access.partial_mode_allowed else "atomic"
        identity = {
            "contract": "manual-import-work-identity@1",
            "runId": str(command.run_id),
            "bindingKey": binding.key,
            "sourcePolicyDigest": policy_digest,
            "sourceDigest": published.content_digest,
            "format": binding.seed_provider.format,
            "mode": mode,
            "extractionProfile": binding.extraction_profile,
        }
        semantic_bytes = canonical_json_bytes(identity)
        semantic_key = f"sha256:{sha256(semantic_bytes).hexdigest()}"
        work_id = uuid5(_RUN_NAMESPACE, f"{command.run_id}:{semantic_key}")
        return WorkUnitSpec(
            work_id=work_id,
            run_id=command.run_id,
            stage_run_id=stage_run_id,
            stage=WorkStage.DISCOVERY,
            capability=WorkCapability.MANUAL_IMPORT,
            source_key=binding.source_key,
            source_binding_key=binding.key,
            semantic_key=semantic_key,
            input_digest=semantic_key,
            expected_output_contract="manual-import-plan@1",
            priority=0,
            retry_policy=RetryPolicy(
                max_attempts=policy.retry_budget + 1,
                initial_delay_seconds=30,
                multiplier=2,
                max_delay_seconds=900,
            ),
            available_at_utc=now_utc,
            correlation_id=command.correlation_id,
            input_artifacts=(
                WorkInputArtifact(
                    artifact_id=published.artifact_id,
                    role=f"manual_source:{binding.seed_provider.format}:{mode}",
                ),
            ),
        )

    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("campaign run clock must return timezone-aware UTC")
        return value


def _routed_http_bindings(
    source_bindings: SourceBindingsDocument,
    *,
    enabled: set[str],
    campaign_key: str,
    correlation_id: str,
) -> frozenset[str]:
    routing = source_bindings.seed_routing
    routes = routing.routes if routing.state == "configured" else ()
    target_keys = {route.target_binding for route in routes if route.source_binding in enabled}
    routed_http = frozenset(
        binding.key
        for binding in source_bindings.items
        if binding.key in enabled
        and binding.key in target_keys
        and binding.capability == "http_fetch"
    )
    unsupported = next(
        (
            binding
            for binding in source_bindings.items
            if binding.key in enabled
            and binding.capability not in _BOOTSTRAP_CAPABILITIES
            and binding.key not in routed_http
        ),
        None,
    )
    if unsupported is not None:
        raise owner_error(
            error_type="collection/source-bootstrap-unsupported",
            owner="CampaignRun",
            code="SOURCE_BOOTSTRAP_UNSUPPORTED",
            message="The enabled source binding has no run bootstrap owner.",
            context={
                "campaignKey": campaign_key,
                "sourceBinding": unsupported.key,
                "capability": unsupported.capability,
            },
            required_action=(
                "Configure a supported manual import or route the HTTP binding from a source "
                "that has a run bootstrap owner."
            ),
            correlation_id=correlation_id,
        )
    return routed_http


def _source_spec(
    policy: SourcePolicy,
    policy_digest: str,
    correlation_id: str,
) -> SourceCapacitySpec:
    # Acquisition authority only. Redistribution is gated separately at export, so a
    # research_only policy is collectable while remaining non-redistributable.
    if policy.legal_status not in _ACQUISITION_AUTHORIZED_STATUSES:
        state = SourceOperationalState.SUSPENDED
    else:
        state = SourceOperationalState.ACTIVE
    if policy.access.kind == "manual":
        max_active = 1
        minimum_interval_ms = 0
    else:
        max_active = policy.access.max_concurrency
        minimum_interval_ms = max(0, int(1000 / policy.access.max_requests_per_second))
    return SourceCapacitySpec(
        source_key=policy.source_key,
        policy_digest=policy_digest,
        state=state,
        max_active_requests=max_active,
        minimum_interval_milliseconds=minimum_interval_ms,
        correlation_id=correlation_id,
    )


def _seed_content_type(format_value: str) -> str:
    return {
        "csv": "text/csv",
        "json": "application/json",
        "jsonl": "application/x-ndjson",
    }[format_value]
