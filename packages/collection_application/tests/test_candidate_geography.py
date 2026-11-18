"""Candidate geography classification against the run's exact boundary artifact.

The tests pin three properties the export gate depends on: an observed point is
classified by the exact boundary including interior holes, a candidate without an
observed point stays unresolved rather than being defaulted into Berlin, and any
contradictory or unverified geography fails closed with owner context.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from collection_application.candidate_geography import (
    CandidateGeographyError,
    classify_candidate_geography,
    read_candidate_geography_boundary,
)
from collection_application.canonicalization import sha256_json

# Berlin-like square from (52.40, 13.30) to (52.60, 13.50) with a hole around Mitte.
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

_BOUNDARY_DIGEST = sha256_json(_BOUNDARY)


def _bundle(boundary: object = _BOUNDARY, *, declared_digest: str | None = None) -> dict:
    geography = {
        "schema_revision": "geography-config-v1",
        "geography_revision": "berlin_official_boundary_v1",
        "boundary_artifact_path": "geography/berlin-boundary.geojson",
        "boundary_digest": declared_digest or _BOUNDARY_DIGEST,
        "provenance_artifact_path": "geography/berlin-boundary.provenance.json",
        "provenance_digest": "sha256:" + "1" * 64,
    }
    components = [
        {
            "path": "geography.yaml",
            "digest": sha256_json(geography),
            "content": geography,
        },
        {
            "path": "geography/berlin-boundary.geojson",
            "digest": _BOUNDARY_DIGEST,
            "content": boundary,
        },
    ]
    return {"campaignKey": "berlin_recording_services", "components": components}


def _point(latitude: str, longitude: str, observation_id: str = "sha256:" + "a" * 64):
    return (
        "coordinates",
        observation_id,
        json.dumps({"latitude": latitude, "longitude": longitude}, sort_keys=True),
    )


def _boundary():
    boundary = read_candidate_geography_boundary(_bundle())
    assert boundary is not None
    return boundary


def test_observed_point_inside_boundary_is_classified_inside() -> None:
    decision = classify_candidate_geography(
        boundary=_boundary(),
        observations=[("name", "sha256:" + "b" * 64, "Studio"), _point("52.5", "13.35")],
    )

    assert decision.coverage == "inside"
    assert decision.point is not None
    assert decision.point.latitude == Decimal("52.5")
    assert decision.boundary.market_area_revision == "berlin_official_boundary_v1"
    assert decision.boundary.market_area_digest == _BOUNDARY_DIGEST


def test_point_inside_interior_hole_is_outside() -> None:
    decision = classify_candidate_geography(
        boundary=_boundary(),
        observations=[_point("52.50", "13.40")],
    )

    assert decision.coverage == "outside"


def test_candidate_without_observed_point_stays_unresolved() -> None:
    decision = classify_candidate_geography(
        boundary=_boundary(),
        observations=[("name", "sha256:" + "c" * 64, "Studio"), ("website", "", "")],
    )

    assert decision.coverage == "unknown"
    assert decision.point is None
    # The declared market area is still carried: unresolved coverage must name the
    # boundary that was consulted, never a placeholder digest.
    assert decision.boundary.market_area_digest == _BOUNDARY_DIGEST


def test_contradictory_points_fail_closed() -> None:
    with pytest.raises(CandidateGeographyError) as failure:
        classify_candidate_geography(
            boundary=_boundary(),
            observations=[_point("52.5", "13.35"), _point("52.45", "13.45")],
        )

    assert failure.value.code == "CANDIDATE_GEOGRAPHY_POINT_CONFLICT"


def test_boundary_digest_conflict_is_rejected() -> None:
    with pytest.raises(CandidateGeographyError) as failure:
        read_candidate_geography_boundary(_bundle(declared_digest="sha256:" + "9" * 64))

    assert failure.value.code == "CANDIDATE_GEOGRAPHY_BOUNDARY_DIGEST_CONFLICT"


def test_campaign_without_geography_declaration_is_unclassifiable() -> None:
    assert read_candidate_geography_boundary({"campaignKey": "x", "components": []}) is None


def test_out_of_range_point_is_rejected() -> None:
    with pytest.raises(CandidateGeographyError) as failure:
        classify_candidate_geography(
            boundary=_boundary(),
            observations=[_point("95", "13.35")],
        )

    assert failure.value.code == "CANDIDATE_GEOGRAPHY_POINT_OUT_OF_RANGE"
