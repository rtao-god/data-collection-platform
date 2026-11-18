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
        "expected_entity_kind,display_name,category,website,osm_id,reference_urls,note,provenance\n"
        "place,Example Studio,recording_studio,https://example.invalid,,,,operator\n",
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
    # Both enabled bindings bootstrap their own independent work. The manual binding
    # yields exactly one unit; the OSM binding yields one query spec per boundary
    # partition, so the total is entailed rather than a fixed number.
    assert len(store.plan.sources) == 2
    osm_units = tuple(
        item for item in store.plan.initial_work if item.source_binding_key == "osm_overpass_berlin"
    )
    manual_units = tuple(
        item for item in store.plan.initial_work if item.source_binding_key == "manual_seed_import"
    )
    assert len(manual_units) == 1
    assert len(osm_units) >= 1
    assert len(store.plan.initial_work) == len(manual_units) + len(osm_units)
    assert all(item.expected_output_contract == "osm-overpass-result@1" for item in osm_units)
    assert all(item.capability.value == "osm_query" for item in osm_units)
    work = next(
        item for item in store.plan.initial_work if item.capability.value == "manual_import"
    )
    assert work.expected_output_contract == "manual-import-plan@1"
    assert work.source_binding_key == "manual_seed_import"
    assert work.input_artifacts[0].role == "manual_source:csv:atomic"


def test_ready_routed_website_campaign_creates_capacity_without_http_work(tmp_path: Path) -> None:
    campaign = tmp_path / "ready_routed_campaign"
    campaign.mkdir()
    source = Path("campaigns/berlin_recording_services")
    for path in source.rglob("*"):
        if path.is_file():
            target = campaign / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())

    campaign_yaml = (campaign / "campaign.yaml").read_text(encoding="utf-8")
    campaign_yaml = campaign_yaml.replace(
        "berlin_recording_services", "ready_routed_campaign"
    ).replace(
        "enabled_source_bindings:\n- manual_seed_import\n- osm_overpass_berlin",
        "enabled_source_bindings:\n- manual_seed_import\n- official_website",
    )
    campaign_yaml = campaign_yaml.split("readiness:", 1)[0] + "readiness:\n  state: ready\n"
    (campaign / "campaign.yaml").write_text(campaign_yaml, encoding="utf-8")
    (campaign / "discovery/manual_seeds.csv").write_text(
        "expected_entity_kind,display_name,category,website,osm_id,reference_urls,note,provenance\n"
        "place,Example Studio,recording_studio,https://example.invalid,,,,operator\n",
        encoding="utf-8",
    )
    (campaign / "source_bindings.yaml").write_text(
        """schema_revision: source-bindings-v1
items:
  - key: manual_seed_import
    source_key: manual_seed
    connector_key: manual_seed
    source_policy_key: manual_seed_policy
    capability: manual_import
    seed_provider:
      kind: file
      path: discovery/manual_seeds.csv
      format: csv
    crawl_budget:
      state: not_applicable
    schedule_enabled: false
    extraction_profile: manual-seed-v1
  - key: official_website
    source_key: official_website
    connector_key: official_http
    source_policy_key: website_policy
    capability: http_fetch
    seed_provider:
      kind: not_applicable
    crawl_budget:
      state: defined
      max_work_units: 10
      max_total_bytes: 1000000
    schedule_enabled: false
    extraction_profile: website-test-v1
seed_routing:
  state: configured
  routes:
    - key: manual_website
      source_field: website
      source_binding: manual_seed_import
      target_binding: official_website
      route_kind: official_website_page
      selection: exclusive
      page_interests:
        - key: contact
          tokens: [contact, kontakt]
          priority: 80
""",
        encoding="utf-8",
    )
    (campaign / "source_policies/website_policy.yaml").write_text(
        """schema_revision: source-policy-v1
policy_key: website_policy
policy_revision: website-test-v1
source_key: official_website
source_type: official_website
legal_status: approved
terms_review:
  state: reviewed
  terms_url: https://example.invalid/terms
  reviewed_at: 2026-01-01
  expires_at: 2099-01-01
  reviewer: test-reviewer
access:
  kind: network
  allowed_methods: [GET]
  allowed_host_patterns: [example.invalid]
  max_requests_per_second: 1
  max_concurrency: 2
  connect_timeout_seconds: 10
  read_timeout_seconds: 20
  max_encoded_bytes: 1000000
  max_decoded_bytes: 2000000
  content_type_allowlist: [text/html]
  user_agent:
    state: configured
    value: CollectorTest/1.0 (+https://example.invalid/contact)
  redirect_limit: 3
storage_class: test-raw-v1
retention_days: 30
allowed_fields: [website]
prohibited_fields: [review_text]
attribution_required: false
browser: forbidden
screenshots: forbidden
retry_budget: 2
stop_on_status_codes: [401, 403, 429]
reviewer: test-reviewer
approver: test-reviewer
""",
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
    run_id = UUID("00000000-0000-0000-0000-000000000112")

    result = service.create(
        CreateCampaignRun(
            run_id=run_id,
            campaign_key="ready_routed_campaign",
            correlation_id="test-routed-run",
        )
    )

    assert result.run_id == run_id
    assert len(store.plan.sources) == 2
    website_capacity = next(
        item for item in store.plan.sources if item.source_key == "official_website"
    )
    assert website_capacity.max_active_requests == 2
    assert website_capacity.minimum_interval_milliseconds == 1000
    # This campaign enables manual seeds and a routed website binding only.
    assert len(store.plan.initial_work) == 1
    assert store.plan.initial_work[0].capability.value == "manual_import"
