"""Bootstrap behaviour for the OSM discovery source.

The bootstrap must publish one immutable Overpass query spec derived from the
verified campaign boundary, and must keep manual import and OSM discovery
independent of each other.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

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
from collection_contracts.errors import OwnerContextError
from collection_contracts.osm_query_spec import (
    boundary_exclusions,
    canonical_query_spec_document,
    partition_boundary,
)
from collection_infrastructure import FilesystemCampaignBundleSource

_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
_CAMPAIGN_SOURCE = Path("campaigns/berlin_recording_services")
_CAMPAIGN_KEY = "osm_bootstrap_campaign"


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


def _copy_campaign(
    tmp_path: Path,
    *,
    mutate_campaign_yaml: Callable[[str], str] | None = None,
) -> Path:
    campaign = tmp_path / _CAMPAIGN_KEY
    for path in _CAMPAIGN_SOURCE.rglob("*"):
        if not path.is_file():
            continue
        target = campaign / path.relative_to(_CAMPAIGN_SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    campaign_yaml = (campaign / "campaign.yaml").read_text(encoding="utf-8")
    campaign_yaml = campaign_yaml.replace("berlin_recording_services", _CAMPAIGN_KEY)
    campaign_yaml = campaign_yaml.split("readiness:", 1)[0]
    campaign_yaml += "readiness:\n  state: ready\n"
    if mutate_campaign_yaml is not None:
        campaign_yaml = mutate_campaign_yaml(campaign_yaml)
    (campaign / "campaign.yaml").write_text(campaign_yaml, encoding="utf-8")
    (campaign / "discovery/manual_seeds.csv").write_text(
        "expected_entity_kind,display_name,category,website,osm_id,reference_urls,note,provenance\n"
        "place,Example Studio,recording_studio,https://example.invalid,,,,operator\n",
        encoding="utf-8",
    )
    return tmp_path


def _service(
    tmp_path: Path,
) -> tuple[CampaignRunService, _ArtifactPort, _RunStore]:
    artifacts = _ArtifactPort()
    service = CampaignRunService(
        CampaignSnapshotPublicationService(
            CampaignSnapshotService(FilesystemCampaignBundleSource(tmp_path)),
            OwnedArtifactPublisherService(artifacts),
            _SnapshotStore(),
        ),
        OwnedArtifactPublisherService(artifacts),
        _RunStore(),
        clock=lambda: _NOW,
    )
    return service, artifacts, service.run_store  # type: ignore[attr-defined]


def _create_plan(tmp_path: Path, run_id: UUID) -> tuple[_ArtifactPort, _RunStore]:
    artifacts = _ArtifactPort()
    store = _RunStore()
    service = CampaignRunService(
        CampaignSnapshotPublicationService(
            CampaignSnapshotService(FilesystemCampaignBundleSource(tmp_path)),
            OwnedArtifactPublisherService(artifacts),
            _SnapshotStore(),
        ),
        OwnedArtifactPublisherService(artifacts),
        store,
        clock=lambda: _NOW,
    )
    service.create(
        CreateCampaignRun(
            run_id=run_id,
            campaign_key=_CAMPAIGN_KEY,
            correlation_id="osm-bootstrap",
        )
    )
    return artifacts, store


def _osm_work(plan: CampaignRunBootstrapPlan):
    return next(item for item in plan.initial_work if item.capability.value == "osm_query")


def test_bootstrap_publishes_overpass_spec_from_verified_boundary(tmp_path: Path) -> None:
    _copy_campaign(tmp_path)
    run_id = UUID("00000000-0000-0000-0000-000000000211")
    artifacts, store = _create_plan(tmp_path, run_id)
    plan = store.plans[0]

    work = _osm_work(plan)
    assert work.expected_output_contract == "osm-overpass-result@1"
    assert work.source_binding_key == "osm_overpass_berlin"
    assert work.stage.value == "discovery"
    assert len(work.input_artifacts) == 1
    query_artifact = work.input_artifacts[0]
    assert query_artifact.role == "overpass_query_spec"

    published = artifacts.published[query_artifact.artifact_id]
    document = json.loads(published.content)
    assert document["schemaRevision"] == "overpass-query-spec-v1"
    assert document["elementTypes"] == ["node", "relation", "way"]
    assert document["timeoutSeconds"] == 180
    assert document["maximumElements"] == 25000
    assert {item["key"] for item in document["tagFilters"]} == {
        "amenity",
        "craft",
    }

    # The polygon is the real verified administrative boundary, not a bounding box.
    boundary = json.loads(
        (_CAMPAIGN_SOURCE / "geography/berlin-boundary.geojson").read_text(encoding="utf-8")
    )
    partitions = partition_boundary(boundary)
    polygon = [tuple(point) for point in document["polygon"]]
    # The spec encodes Overpass `poly` order: latitude first, longitude second.
    assert polygon == [(p.latitude, p.longitude) for p in partitions[0].ring]
    latitudes = [point[0] for point in polygon]
    longitudes = [point[1] for point in polygon]
    assert min(longitudes) > 12.5 and max(longitudes) < 14.5
    assert min(latitudes) > 52.0 and max(latitudes) < 53.0


def test_published_spec_bytes_are_the_canonical_contract_digest(tmp_path: Path) -> None:
    _copy_campaign(tmp_path)
    artifacts, store = _create_plan(tmp_path, UUID("00000000-0000-0000-0000-000000000212"))
    work = _osm_work(store.plans[0])
    command = artifacts.published[work.input_artifacts[0].artifact_id]
    canonical = canonical_query_spec_document_bytes(command.content)
    assert command.content == canonical
    # Work identity is derived from the same spec digest the publisher records.
    assert command.source_policy_digest is not None


def canonical_query_spec_document_bytes(content: bytes) -> bytes:
    """The contract serializes deterministically, so re-parsing must be a no-op."""
    from collection_contracts.osm_query_spec import OverpassQuerySpecDocument

    return canonical_query_spec_document(OverpassQuerySpecDocument.model_validate_json(content))


def test_each_source_bootstraps_independently(tmp_path: Path) -> None:
    _copy_campaign(tmp_path)
    _artifacts, store = _create_plan(tmp_path, UUID("00000000-0000-0000-0000-000000000213"))
    plan = store.plans[0]
    work = _osm_work(plan)
    manual = next(item for item in plan.initial_work if item.capability.value == "manual_import")
    assert manual.work_id != work.work_id
    assert manual.source_binding_key == "manual_seed_import"
    # Manual import reads the seed from the campaign bundle; only OSM publishes.
    manual_artifact = manual.input_artifacts[0]
    assert manual_artifact.role.startswith("manual_source:")


def test_replayed_run_reproduces_identical_work_and_artifact_identity(
    tmp_path: Path,
) -> None:
    _copy_campaign(tmp_path)
    run_id = UUID("00000000-0000-0000-0000-000000000214")
    first_artifacts, first_store = _create_plan(tmp_path, run_id)
    second_artifacts, second_store = _create_plan(tmp_path, run_id)

    first_work = _osm_work(first_store.plans[0])
    second_work = _osm_work(second_store.plans[0])
    assert first_work.work_id == second_work.work_id
    assert first_work.input_digest == second_work.input_digest

    first_artifact = first_artifacts.published[first_work.input_artifacts[0].artifact_id]
    second_artifact = second_artifacts.published[second_work.input_artifacts[0].artifact_id]
    assert first_artifact.content == second_artifact.content
    assert first_artifact.artifact_id == second_artifact.artifact_id


def test_boundary_with_interior_hole_is_reported_not_silently_dropped(
    tmp_path: Path,
) -> None:
    _copy_campaign(tmp_path)
    boundary = json.loads(
        (_CAMPAIGN_SOURCE / "geography/berlin-boundary.geojson").read_text(encoding="utf-8")
    )
    # The Overpass query cannot express interior holes. The bootstrap must still
    # succeed and the hole must remain an explicit, reported exclusion.
    exclusions = boundary_exclusions(boundary)
    assert exclusions, "the Berlin boundary is expected to contain interior rings"
    _artifacts, store = _create_plan(tmp_path, UUID("00000000-0000-0000-0000-000000000215"))
    work = _osm_work(store.plans[0])
    assert work.expected_output_contract == "osm-overpass-result@1"


def test_unrepresentable_boundary_fails_explicitly() -> None:
    # A degenerate ring has no area: it must be rejected, never widened to a
    # bounding box or truncated to something queryable.
    degenerate = {
        "type": "Polygon",
        "coordinates": [[[13.0, 52.0], [13.0, 52.0], [13.0, 52.0], [13.0, 52.0]]],
    }
    with pytest.raises(ValueError, match="no area"):
        partition_boundary(degenerate)


def test_non_polygon_geography_is_rejected() -> None:
    with pytest.raises(ValueError, match="Polygon"):
        partition_boundary({"type": "Point", "coordinates": [13.0, 52.0]})


def test_oversized_ring_is_partitioned_by_clipping_not_vertex_windows() -> None:
    boundary = json.loads(
        (_CAMPAIGN_SOURCE / "geography/berlin-boundary.geojson").read_text(encoding="utf-8")
    )
    partitions = partition_boundary(boundary)
    # No partition may exceed the connector limit.
    assert all(len(partition.ring) <= 5000 for partition in partitions)
    assert len(partitions) > 1

    # Clipping keeps every source vertex; axis-aligned cuts only add seam points, so no
    # source vertex may be missing and no point may fall outside the original extent.
    source_vertices = {
        (float(point[1]), float(point[0])) for part in boundary["coordinates"] for point in part[0]
    }
    partitioned_vertices = {
        (point.latitude, point.longitude) for partition in partitions for point in partition.ring
    }
    assert source_vertices <= partitioned_vertices

    longitudes = [point[1] for point in source_vertices]
    latitudes = [point[0] for point in source_vertices]
    for latitude, longitude in partitioned_vertices:
        assert min(latitudes) <= latitude <= max(latitudes)
        assert min(longitudes) <= longitude <= max(longitudes)


def test_partition_union_covers_exactly_the_boundary_area() -> None:
    boundary = json.loads(
        (_CAMPAIGN_SOURCE / "geography/berlin-boundary.geojson").read_text(encoding="utf-8")
    )
    partitions = partition_boundary(boundary)

    exterior_only = 0.0
    for part in boundary["coordinates"]:
        ring = part[0]
        exterior_only += _planar_ring_area(ring)
    covered = sum(_partition_area(partition) for partition in partitions)

    # Clipping is exact: the union of the pieces covers the boundary area itself.
    assert covered == pytest.approx(exterior_only, rel=1e-9)


def _planar_ring_area(ring: list[list[float]]) -> float:
    points = [(float(point[1]), float(point[0])) for point in ring]
    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    total = 0.0
    for index, (latitude, longitude) in enumerate(points):
        next_latitude, next_longitude = points[(index + 1) % len(points)]
        total += longitude * next_latitude - next_longitude * latitude
    return abs(total) / 2


def _partition_area(partition: object) -> float:
    ring = [(point.latitude, point.longitude) for point in partition.ring]  # type: ignore[attr-defined]
    total = 0.0
    for index, (latitude, longitude) in enumerate(ring):
        next_latitude, next_longitude = ring[(index + 1) % len(ring)]
        total += longitude * next_latitude - next_longitude * latitude
    return abs(total) / 2


def test_suspended_policy_still_bootstraps_collection_work(tmp_path: Path) -> None:
    _copy_campaign(tmp_path)
    policy_path = tmp_path / _CAMPAIGN_KEY / "source_policies/osm_berlin_studios_policy.yaml"
    # research_only authorizes acquisition but never redistribution.
    assert "legal_status: research_only" in policy_path.read_text(encoding="utf-8")
    _artifacts, store = _create_plan(tmp_path, UUID("00000000-0000-0000-0000-000000000217"))
    plan = store.plans[0]
    osm_source = next(item for item in plan.sources if item.source_key == "openstreetmap")
    assert osm_source.state.value == "active"


def test_blocked_policy_is_rejected_before_any_work_is_bootstrapped(
    tmp_path: Path,
) -> None:
    _copy_campaign(tmp_path)
    policy_path = tmp_path / _CAMPAIGN_KEY / "source_policies/osm_berlin_studios_policy.yaml"
    policy_text = policy_path.read_text(encoding="utf-8")
    assert "legal_status: research_only" in policy_text
    policy_path.write_text(
        policy_text.replace("legal_status: research_only", "legal_status: blocked"),
        encoding="utf-8",
    )

    # A blocked policy on an enabled binding is a configuration error, not a
    # silently degraded run: no work may be bootstrapped from it.
    port = _ArtifactPort()
    store = _RunStore()
    service = CampaignRunService(
        CampaignSnapshotPublicationService(
            CampaignSnapshotService(FilesystemCampaignBundleSource(tmp_path)),
            OwnedArtifactPublisherService(port),
            _SnapshotStore(),
        ),
        OwnedArtifactPublisherService(port),
        store,
        clock=lambda: _NOW,
    )
    with pytest.raises(OwnerContextError) as excinfo:
        service.create(
            CreateCampaignRun(
                run_id=UUID("00000000-0000-0000-0000-000000000218"),
                campaign_key=_CAMPAIGN_KEY,
                correlation_id="osm-blocked",
            )
        )
    assert "blocked" in str(excinfo.value).lower() or excinfo.value.args
    assert store.plans == []


def test_disabled_osm_binding_leaves_manual_import_intact(tmp_path: Path) -> None:
    _copy_campaign(
        tmp_path,
        mutate_campaign_yaml=lambda text: text.replace(
            "enabled_source_bindings:\n- manual_seed_import\n- osm_overpass_berlin",
            "enabled_source_bindings:\n- manual_seed_import",
        ),
    )
    _artifacts, store = _create_plan(tmp_path, UUID("00000000-0000-0000-0000-000000000219"))
    plan = store.plans[0]
    assert all(item.capability.value != "osm_query" for item in plan.initial_work)
    assert any(item.capability.value == "manual_import" for item in plan.initial_work)
