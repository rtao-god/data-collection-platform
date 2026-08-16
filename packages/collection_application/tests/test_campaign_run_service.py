from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from collection_application import (
    CampaignRunBootstrapPlan,
    CampaignRunCreated,
    CampaignRunService,
    CampaignSnapshotPublicationService,
    CampaignSnapshotService,
    CreateCampaignRun,
    OwnedArtifactPublisherService,
    PublishedOwnedArtifact,
)
from collection_application.owned_artifacts import PublishOwnedArtifact
from collection_infrastructure import FilesystemCampaignBundleSource

_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


class ArtifactPort:
    def publish(self, command: PublishOwnedArtifact) -> PublishedOwnedArtifact:
        from hashlib import sha256

        return PublishedOwnedArtifact(
            artifact_id=command.artifact_id,
            operation_id=command.operation_id,
            producer_identity=command.producer_identity,
            artifact_kind=command.artifact_kind,
            content_digest=f"sha256:{sha256(command.content).hexdigest()}",
            size_bytes=len(command.content),
            content_type=command.content_type,
            storage_reference=f"{command.artifact_kind.value}/{command.artifact_id}",
            recorded_at_utc=_NOW,
        )


class SnapshotStore:
    def publish(self, snapshot: object, **_: object) -> None:
        self.snapshot = snapshot


class RunStore:
    def create(self, plan: CampaignRunBootstrapPlan) -> CampaignRunCreated:
        self.plan = plan
        return CampaignRunCreated(
            run_id=plan.run.run_id,
            campaign_key=plan.run.campaign_key,
            config_bundle_digest=plan.run.config_bundle_digest,
            initial_work_ids=tuple(item.work_id for item in plan.initial_work),
        )


def test_ready_manual_campaign_builds_one_atomic_bootstrap_plan(tmp_path: Path) -> None:
    campaign = tmp_path / "ready_campaign"
    campaign.mkdir()
    source = Path("campaigns/berlin_recording_services")
    for path in source.rglob("*"):
        if path.is_file():
            target = campaign / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    campaign_yaml = (campaign / "campaign.yaml").read_text(encoding="utf-8")
    campaign_yaml = campaign_yaml.replace("berlin_recording_services", "ready_campaign")
    campaign_yaml = campaign_yaml.split("readiness:", 1)[0]
    campaign_yaml += "readiness:\n  state: ready\n"
    (campaign / "campaign.yaml").write_text(campaign_yaml, encoding="utf-8")
    (campaign / "discovery/manual_seeds.csv").write_text(
        "expected_entity_kind,display_name,website,osm_id,reference_urls,note,provenance\n"
        "place,Example Studio,https://example.invalid,,,,operator\n",
        encoding="utf-8",
    )

    artifacts = OwnedArtifactPublisherService(ArtifactPort())
    publication = CampaignSnapshotPublicationService(
        CampaignSnapshotService(FilesystemCampaignBundleSource(tmp_path)),
        artifacts,
        SnapshotStore(),
    )
    store = RunStore()
    service = CampaignRunService(publication, artifacts, store, clock=lambda: _NOW)
    run_id = UUID("00000000-0000-0000-0000-000000000111")

    result = service.create(
        CreateCampaignRun(run_id=run_id, campaign_key="ready_campaign", correlation_id="test")
    )

    assert result.run_id == run_id
    assert len(store.plan.stages) == 8
    assert len(store.plan.sources) == 1
    assert len(store.plan.initial_work) == 1
    work = store.plan.initial_work[0]
    assert work.expected_output_contract == "manual-import-plan@1"
    assert work.input_artifacts[0].role == "manual_source:csv:atomic"
