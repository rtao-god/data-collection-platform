from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from tools.live_collection.berlin_boundary import (
    BoundaryMaterializationError,
    ResourceCandidate,
    _canonical_json,
    _score_resource,
    canonicalize_boundary,
    discover_candidates,
    update_campaign,
)


def _polygon() -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [13.0884, 52.3383],
                [13.7611, 52.3383],
                [13.7611, 52.6755],
                [13.0884, 52.6755],
                [13.0884, 52.3383],
            ]
        ],
    }


def test_resource_candidate_rejects_non_official_host() -> None:
    with pytest.raises(BoundaryMaterializationError):
        ResourceCandidate(
            dataset_title="Berlin boundary",
            dataset_identifier="berlin-boundary",
            resource_identifier="resource-1",
            resource_url="https://example.org/boundary.geojson",
            resource_format="geojson",
            license_identifier="dl-de-by-2.0",
            license_title="Data licence Germany attribution 2.0",
            score=100,
        )


def test_landesgrenze_geojson_is_ranked_above_generic_resource() -> None:
    dataset = {
        "title": "Landesgrenze Berlin",
        "name": "landesgrenze-berlin",
    }
    geojson = {
        "name": "Landesgrenze GeoJSON",
        "format": "GeoJSON",
        "url": "https://gdi.berlin.de/landesgrenze.geojson",
    }
    generic = {
        "name": "Dokumentation",
        "format": "PDF",
        "url": "https://gdi.berlin.de/dokumentation.pdf",
    }

    assert _score_resource(dataset, geojson) > _score_resource(dataset, generic)


def test_discovery_accepts_only_licensed_official_resources() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "results": [
                        {
                            "id": "dataset-1",
                            "name": "landesgrenze-berlin",
                            "title": "Landesgrenze Berlin",
                            "license_id": "dl-de-by-2.0",
                            "license_title": "Data licence Germany attribution 2.0",
                            "resources": [
                                {
                                    "id": "external",
                                    "name": "Mirror",
                                    "format": "GeoJSON",
                                    "url": "https://example.org/berlin.geojson",
                                },
                                {
                                    "id": "official",
                                    "name": "Landesgrenze GeoJSON",
                                    "format": "GeoJSON",
                                    "url": "https://gdi.berlin.de/berlin.geojson",
                                },
                            ],
                        }
                    ]
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        candidates = discover_candidates(client)

    assert {item.resource_identifier for item in candidates} == {"official"}


def test_canonical_boundary_is_one_stable_feature() -> None:
    value = {
        "type": "Feature",
        "properties": {"volatile": "ignored"},
        "geometry": _polygon(),
    }

    first = canonicalize_boundary(value)
    second = canonicalize_boundary(json.loads(_canonical_json(value)))

    assert first == second
    assert first["type"] == "Feature"
    assert first["id"] == "berlin-administrative-boundary"
    assert first["geometry"] == _polygon()


def test_boundary_outside_berlin_envelope_is_rejected() -> None:
    with pytest.raises(BoundaryMaterializationError, match="outside the Berlin"):
        canonicalize_boundary(
            {
                "type": "Polygon",
                "coordinates": [
                    [
                        [7.0, 50.0],
                        [8.0, 50.0],
                        [8.0, 51.0],
                        [7.0, 51.0],
                        [7.0, 50.0],
                    ]
                ],
            }
        )


def test_campaign_update_replaces_boundary_identity_and_only_matching_blocker(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / "campaign.yaml").write_text(
        yaml.safe_dump(
            {
                "readiness": {
                    "state": "blocked",
                    "blockers": [
                        {
                            "code": "BERLIN_BOUNDARY_ARTIFACT_MISSING",
                            "owner": "Geography",
                        },
                        {
                            "code": "SOURCE_CONSENT_MISSING",
                            "owner": "Sources",
                        },
                    ],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (campaign / "geography.yaml").write_text(
        yaml.safe_dump(
            {
                "boundary_artifact_path": "missing/berlin.geojson",
                "boundary_digest": "sha256:" + "0" * 64,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    modified = update_campaign(
        campaign,
        boundary_path="campaigns/example/geography/berlin-boundary.geojson",
        digest="sha256:" + "a" * 64,
    )

    assert {path.name for path in modified} == {"campaign.yaml", "geography.yaml"}
    campaign_value = yaml.safe_load((campaign / "campaign.yaml").read_text())
    assert campaign_value["readiness"]["state"] == "blocked"
    assert [item["code"] for item in campaign_value["readiness"]["blockers"]] == [
        "SOURCE_CONSENT_MISSING"
    ]
    geography_value = yaml.safe_load((campaign / "geography.yaml").read_text())
    assert geography_value == {
        "boundary_artifact_path": ("campaigns/example/geography/berlin-boundary.geojson"),
        "boundary_digest": "sha256:" + "a" * 64,
    }
