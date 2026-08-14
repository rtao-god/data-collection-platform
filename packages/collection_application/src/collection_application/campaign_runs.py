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
from collection_contracts import SourceBinding, SourcePolicy, owner_error
from collection_domain import (
    CollectionRunState,
    RetryPolicy,
    SourceOperationalState,
    StageRunState,
    WorkCapability,
    WorkStage,
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
            else:
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


def _source_spec(
    policy: SourcePolicy,
    policy_digest: str,
    correlation_id: str,
) -> SourceCapacitySpec:
    if policy.legal_status not in {"approved", "reference_only"}:
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
