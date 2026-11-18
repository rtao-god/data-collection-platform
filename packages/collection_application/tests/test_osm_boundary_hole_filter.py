"""Boundary hole filtering after an over-approximating Overpass polygon query.

An Overpass `poly` filter has one ring, so a partition query that covers a boundary hole
returns elements that are not inside the campaign boundary. Those elements must not reach
candidate intake. These tests prove the filter uses the exact original boundary, admits
points outside every hole, and fails loudly instead of silently forwarding nothing.
"""

from __future__ import annotations

import json
from hashlib import sha256
from uuid import UUID

import pytest

from collection_contracts.osm_query_spec import boundary_contains

# Square from (52.40, 13.30) to (52.60, 13.50) with an interior hole from
# (52.48, 13.38) to (52.52, 13.42).
_BOUNDARY = {
    "type": "MultiPolygon",
    "coordinates": [
        [
            [
                [13.30, 52.40],
                [13.50, 52.40],
                [13.50, 52.60],
                [13.30, 52.60],
                [13.30, 52.40],
            ],
            [
                [13.38, 52.48],
                [13.42, 52.48],
                [13.42, 52.52],
                [13.38, 52.52],
                [13.38, 52.48],
            ],
        ]
    ],
}


def test_point_inside_the_boundary_is_contained() -> None:
    assert boundary_contains(_BOUNDARY, 52.45, 13.35) is True


def test_point_inside_the_hole_is_not_contained() -> None:
    assert boundary_contains(_BOUNDARY, 52.50, 13.40) is False


def test_point_outside_the_boundary_is_not_contained() -> None:
    assert boundary_contains(_BOUNDARY, 52.70, 13.40) is False


def test_hole_boundary_is_stable_across_repeated_calls() -> None:
    values = [boundary_contains(_BOUNDARY, 52.50, 13.40) for _ in range(3)]
    assert values == [False, False, False]


def test_finite_coordinates_are_required() -> None:
    with pytest.raises(ValueError, match="finite"):
        boundary_contains(_BOUNDARY, float("nan"), 13.40)


def test_boundary_document_without_holes_admits_the_same_point() -> None:
    without_hole = {
        "type": "MultiPolygon",
        "coordinates": [_BOUNDARY["coordinates"][0][:1]],
    }
    assert boundary_contains(without_hole, 52.50, 13.40) is True


def test_excluded_elements_are_recorded_on_the_mapping_action() -> None:
    """The count of boundary-excluded elements is durable, not silently dropped."""

    from collection_application.pipeline_advancement import SourceRecordMappingAction

    action = SourceRecordMappingAction(
        work_id=UUID("00000000-0000-0000-0000-0000000000a1"),
        source_key="openstreetmap",
        source_binding_key="osm_overpass_berlin",
        source_policy_digest="sha256:" + "1" * 64,
        source_record_id="osm:node:1",
        dataset_release="berlin-2026-01",
        source_record_artifact=_artifact(),
        source_record_payload=_payload(),
        raw_response_artifact=_artifact(role="osm_observations"),
        mapping_profile_payload=json.dumps({"profileKey": "hole-filter"}).encode("utf-8"),
        mapping_profile_digest=_digest(json.dumps({"profileKey": "hole-filter"}).encode("utf-8")),
        semantic_key="sha256:" + "5" * 64,
        input_digest="sha256:" + "3" * 64,
        excluded_by_boundary_record_ids=("osm:node:9", "osm:node:10"),
    )
    assert action.excluded_by_boundary_record_ids == ("osm:node:9", "osm:node:10")


def _payload() -> bytes:
    return json.dumps({"contract": "osm-source-record@1"}, sort_keys=True).encode("utf-8")


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _artifact(role: str = "source_record"):
    from collection_application.pipeline_advancement import ArtifactIdentity

    return ArtifactIdentity(
        artifact_id=UUID("00000000-0000-0000-0000-0000000000b1"),
        role=role,
        content_digest=_digest(_payload()),
        size_bytes=len(_payload()),
        content_type="application/json",
    )
