"""Bootstrap behaviour for the Wikidata entity source.

Bootstrap must publish one immutable, canonical query specification for the operator-declared
entity set and must keep the Wikidata work independent of manual import and Overpass.

The shipped campaign keeps `wikidata_studios_berlin` disabled until the owner authorizes it,
so this module enables it on its own copy of `campaign.yaml` — the post-authorization shape.
The policy bytes are the shipped ones and stay `legal_status: research_only`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
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
from collection_contracts import (
    canonical_wikidata_query_spec_document,
    decode_wikidata_query_spec,
)
from collection_infrastructure import FilesystemCampaignBundleSource

_NOW = datetime(2026, 12, 1, 9, 30, tzinfo=UTC)
_CAMPAIGN_SOURCE = Path(__file__).resolve().parents[3] / "campaigns" / "berlin_recording_services"
_CAMPAIGN_KEY = "wikidata_bootstrap_campaign"
_PILOT_ENTITY_IDS = ["Q105762301", "Q1372342", "Q56298345", "Q667023", "Q7695694"]


class _ArtifactPort:
    def __init__(self) -> None:
        self.published: dict[UUID, PublishOwnedArtifact] = {}

    def publish(self, command: PublishOwnedArtifact) -> PublishedOwnedArtifact:
        self.published[command.artifact_id] = command
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


class _SnapshotStore:
    def publish(self, snapshot: object, **_: object) -> None:
        self.snapshot = snapshot


class _RunStore:
    def __init__(self) -> None:
        self.plans: list[CampaignRunBootstrapPlan] = []

    def create(self, plan: CampaignRunBootstrapPlan) -> CampaignRunCreated:
        self.plans.append(plan)
        return CampaignRunCreated(
            run_id=plan.run.run_id,
            campaign_key=plan.run.campaign_key,
            config_bundle_digest=plan.run.config_bundle_digest,
            initial_work_ids=tuple(item.work_id for item in plan.initial_work),
        )


def _copy_campaign(tmp_path: Path) -> Path:
    campaign = tmp_path / _CAMPAIGN_KEY
    for path in _CAMPAIGN_SOURCE.rglob("*"):
        if not path.is_file():
            continue
        target = campaign / path.relative_to(_CAMPAIGN_SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    campaign_yaml = (campaign / "campaign.yaml").read_text(encoding="utf-8")
    campaign_yaml = campaign_yaml.replace("berlin_recording_services", _CAMPAIGN_KEY)
    campaign_yaml = campaign_yaml.replace(
        "enabled_source_bindings:\n- manual_seed_import\n- osm_overpass_berlin\n",
        "enabled_source_bindings:\n- manual_seed_import\n- osm_overpass_berlin\n"
        "- wikidata_studios_berlin\n",
    )
    assert "- wikidata_studios_berlin\n" in campaign_yaml
    (campaign / "campaign.yaml").write_text(campaign_yaml, encoding="utf-8")
    (campaign / "discovery/manual_seeds.csv").write_text(
        "expected_entity_kind,display_name,category,website,osm_id,reference_urls,note,provenance\n"
        "place,Example Studio,recording_studio,https://example.invalid,,,,operator\n",
        encoding="utf-8",
    )
    return tmp_path


def _bootstrap(tmp_path: Path, run_id: UUID) -> tuple[_ArtifactPort, _RunStore]:
    artifacts = _ArtifactPort()
    store = _RunStore()
    CampaignRunService(
        CampaignSnapshotPublicationService(
            CampaignSnapshotService(FilesystemCampaignBundleSource(tmp_path)),
            OwnedArtifactPublisherService(artifacts),
            _SnapshotStore(),
        ),
        OwnedArtifactPublisherService(artifacts),
        store,
        clock=lambda: _NOW,
    ).create(
        CreateCampaignRun(
            run_id=run_id,
            campaign_key=_CAMPAIGN_KEY,
            correlation_id="wikidata-bootstrap",
        )
    )
    return artifacts, store


def _wikidata_work(plan: CampaignRunBootstrapPlan):
    return next(
        item for item in plan.initial_work if item.capability.value == "wikidata_entity_query"
    )


def test_bootstrap_emits_a_bounded_entity_discovery_unit(tmp_path: Path) -> None:
    _copy_campaign(tmp_path)
    artifacts, store = _bootstrap(tmp_path, UUID("00000000-0000-0000-0000-000000000311"))
    work = _wikidata_work(store.plans[0])

    assert work.stage.value == "discovery"
    assert work.source_binding_key == "wikidata_studios_berlin"
    assert work.expected_output_contract == "wikidata-entity-result@2"
    # One declared read, retried within the policy's own budget.
    assert work.retry_policy.max_attempts >= 2
    assert len(work.input_artifacts) == 1
    spec_artifact = work.input_artifacts[0]
    assert spec_artifact.role == "wikidata_entity_query_spec"

    published = artifacts.published[spec_artifact.artifact_id]
    document = json.loads(published.content)
    assert document["schemaRevision"] == "wikidata-entity-query-spec-v2"
    assert document["entityIds"] == _PILOT_ENTITY_IDS
    assert document["labelLanguages"] == ["de", "en"]
    assert document["classification"] == {
        "campaignCategory": "recording_studio",
        "compatibleClassIds": [],
        "studioClassId": "Q746369",
    }
    # The specification is bound to the run, the binding and the exact policy decision.
    assert document["bindingKey"] == "wikidata_studios_berlin"
    assert document["runId"] == str(store.plans[0].run.run_id)
    assert document["sourcePolicyDigest"].startswith("sha256:")
    assert len(document["sourcePolicyDigest"]) == len("sha256:") + 64

    # The published bytes are exactly the canonical contract, so a replay of the same
    # declaration publishes the same artifact digest.
    assert published.content == _canonical(published.content)
    assert published.source_policy_digest == document["sourcePolicyDigest"]


def test_replaying_the_declaration_publishes_the_same_work_identity(tmp_path: Path) -> None:
    _copy_campaign(tmp_path)
    run_id = UUID("00000000-0000-0000-0000-000000000312")
    _artifacts, first = _bootstrap(tmp_path, run_id)
    second_artifacts, second = _bootstrap(tmp_path, run_id)

    first_work = _wikidata_work(first.plans[0])
    second_work = _wikidata_work(second.plans[0])
    assert first_work.work_id == second_work.work_id
    assert first_work.semantic_key == second_work.semantic_key
    first_content = next(iter(_artifacts.published.values())).content
    second_content = next(iter(second_artifacts.published.values())).content
    assert first_content == second_content


def test_each_source_bootstraps_independently(tmp_path: Path) -> None:
    _copy_campaign(tmp_path)
    _artifacts, store = _bootstrap(tmp_path, UUID("00000000-0000-0000-0000-000000000313"))
    plan = store.plans[0]
    work = _wikidata_work(plan)
    manual = next(item for item in plan.initial_work if item.capability.value == "manual_import")
    osm = next(item for item in plan.initial_work if item.capability.value == "osm_query")

    assert len({work.work_id, manual.work_id, osm.work_id}) == 3
    assert {work.source_binding_key, manual.source_binding_key, osm.source_binding_key} == {
        "wikidata_studios_berlin",
        "manual_seed_import",
        "osm_overpass_berlin",
    }
    assert work.input_artifacts[0].role == "wikidata_entity_query_spec"


def _canonical(content: bytes) -> bytes:
    """Re-render the decoded specification so its canonical form proves the bytes."""

    return canonical_wikidata_query_spec_document(decode_wikidata_query_spec(content))
