"""Real geometry -> coverage -> sealed export, proven against the shipped campaign.

The point of these tests is that a studio's location is never invented: coverage comes
from the element's own coordinates against the exact boundary artifact in the campaign
bundle, a missing or ambiguous point stays unresolved, and only a verified decision may
become an exportable entry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

import pytest

from collection_application import CampaignSnapshotService, RawCampaignBundle
from collection_application.candidate_geography import (
    CandidateGeographyBoundary,
    CandidateGeographyError,
    classify_candidate_geography,
    read_candidate_geography_boundary,
)
from collection_application.geography import decode_boundary_geojson
from collection_contracts import OwnerContextError

_REPOSITORY_ROOT = Path(__file__).parents[3]
_CAMPAIGN_ROOT = _REPOSITORY_ROOT / "campaigns" / "berlin_recording_services"
_LONGITUDE, _LATITUDE = 13.4168436, 52.5007771


def _bundle() -> dict[str, object]:
    """The exact run bundle the snapshot owner produces for the shipped campaign."""

    return _compile(_shipped_files()).bundle()


def _shipped_files() -> dict[str, bytes]:
    return {
        path.relative_to(_CAMPAIGN_ROOT).as_posix(): path.read_bytes()
        for path in _CAMPAIGN_ROOT.rglob("*")
        if path.is_file()
    }


@dataclass(frozen=True)
class _Source:
    files: dict[str, bytes]

    def read(self, campaign_key: str, correlation_id: str) -> RawCampaignBundle:
        del correlation_id
        return RawCampaignBundle(campaign_key=campaign_key, files=MappingProxyType(self.files))


@dataclass(frozen=True)
class _Compiled:
    bundle_content: bytes

    def bundle(self) -> dict[str, object]:
        decoded = json.loads(self.bundle_content.decode("utf-8"))
        assert isinstance(decoded, dict)
        return decoded


def _compile(files: dict[str, bytes]) -> _Compiled:
    compiled = CampaignSnapshotService(_Source(files)).compile(
        "berlin_recording_services",
        "correlation-geography",
    )
    return _Compiled(bundle_content=compiled.canonical_content)


def _point(latitude: float, longitude: float, observation_id: str = "obs-1") -> str:
    return json.dumps(
        {"latitude": str(latitude), "longitude": str(longitude)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_a_real_berlin_point_is_classified_inside_the_shipped_boundary() -> None:
    boundary = read_candidate_geography_boundary(_bundle())

    assert boundary is not None
    decision = classify_candidate_geography(
        boundary=boundary,
        observations=(("coordinates", "obs-1", _point(_LATITUDE, _LONGITUDE)),),
    )

    assert decision.coverage == "inside"
    assert decision.point is not None
    assert decision.point.latitude == Decimal(str(_LATITUDE))
    assert decision.boundary.market_area_revision == "berlin_official_boundary_v1"


def test_a_point_outside_the_boundary_is_classified_outside_not_dropped() -> None:
    boundary = read_candidate_geography_boundary(_bundle())

    decision = classify_candidate_geography(
        boundary=boundary,
        # Munich, far outside the Berlin boundary.
        observations=(("coordinates", "obs-1", _point(48.1372, 11.5756)),),
    )

    assert decision.coverage == "outside"


def test_a_candidate_without_coordinates_stays_unknown_and_keeps_no_point() -> None:
    boundary = read_candidate_geography_boundary(_bundle())

    decision = classify_candidate_geography(
        boundary=boundary,
        # A legitimately remote-only entity: name and site, no location at all.
        observations=(
            ("display_name", "obs-1", "Remote Mastering"),
            ("website", "obs-2", "https://remote.example"),
        ),
    )

    assert decision.coverage == "unknown"
    assert decision.point is None


def test_a_holed_boundary_does_not_classify_a_point_in_the_hole_as_inside() -> None:
    """The exact boundary geometry decides, including its excluded areas.

    Proven at the classifier owner with a boundary artifact built through the canonical
    decoder, because the campaign snapshot refuses a boundary whose provenance does not
    describe it.
    """

    boundary = CandidateGeographyBoundary(
        market_area_revision="holed-fixture-v1",
        market_area_digest=decode_boundary_geojson(_boundary_with_hole()).geometry_digest,
        document=json.loads(decode_boundary_geojson(_boundary_with_hole()).canonical_geojson),
    )

    inside_ring = classify_candidate_geography(
        boundary=boundary,
        observations=(("coordinates", "obs-1", _point(52.60, 13.20)),),
    )
    in_hole = classify_candidate_geography(
        boundary=boundary,
        observations=(("coordinates", "obs-1", _point(52.50, 13.40)),),
    )

    assert inside_ring.coverage == "inside"
    assert in_hole.coverage == "outside"


def test_two_contradictory_points_are_a_conflict_not_a_vote() -> None:
    boundary = read_candidate_geography_boundary(_bundle())

    with pytest.raises(CandidateGeographyError) as captured:
        classify_candidate_geography(
            boundary=boundary,
            observations=(
                ("coordinates", "obs-1", _point(52.50, 13.40)),
                ("coordinates", "obs-2", _point(52.60, 13.30)),
            ),
        )

    assert captured.value.code == "CANDIDATE_GEOGRAPHY_POINT_CONFLICT"


def test_the_same_point_from_two_sources_is_one_point() -> None:
    boundary = read_candidate_geography_boundary(_bundle())

    decision = classify_candidate_geography(
        boundary=boundary,
        observations=(
            ("coordinates", "obs-1", _point(52.50, 13.40)),
            ("coordinates", "obs-2", _point(52.50, 13.40)),
        ),
    )

    # Agreement is not a conflict: one distinct point is one decision.
    assert decision.coverage == "inside"


def test_a_malformed_point_fails_instead_of_defaulting_to_unknown() -> None:
    boundary = read_candidate_geography_boundary(_bundle())

    with pytest.raises(CandidateGeographyError) as captured:
        classify_candidate_geography(
            boundary=boundary,
            observations=(("coordinates", "obs-1", "52.5,13.4"),),
        )

    assert captured.value.code == "CANDIDATE_GEOGRAPHY_POINT_INVALID"


def test_a_boundary_whose_bytes_do_not_match_its_declared_digest_is_rejected() -> None:
    bundle = _bundle()
    components = bundle["components"]
    assert isinstance(components, list)
    for component in components:
        if component["path"] == "geography/berlin-boundary.geojson":
            component["content"] = b'{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}'

    with pytest.raises(CandidateGeographyError) as captured:
        read_candidate_geography_boundary(bundle)

    assert captured.value.code == "CANDIDATE_GEOGRAPHY_BOUNDARY_DIGEST_CONFLICT"


def test_a_campaign_without_a_geography_declaration_declares_no_market_area() -> None:
    bundle = _bundle()
    components = bundle["components"]
    assert isinstance(components, list)
    bundle["components"] = [item for item in components if item["path"] != "geography.yaml"]

    assert read_candidate_geography_boundary(bundle) is None


def test_classification_without_a_boundary_is_a_hard_failure() -> None:
    with pytest.raises(CandidateGeographyError) as captured:
        classify_candidate_geography(boundary=None, observations=())

    assert captured.value.code == "CANDIDATE_GEOGRAPHY_DECLARATION_MISSING"


def test_the_os_redistribution_policy_for_osm_is_still_research_only() -> None:
    """Geography never legitimizes redistribution: the source policy decides that."""

    import yaml

    policy = yaml.safe_load(
        (_CAMPAIGN_ROOT / "source_policies" / "osm_berlin_studios_policy.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert policy["legal_status"] == "research_only"
    assert OwnerContextError is not None


def _boundary_with_hole() -> bytes:
    """A square Berlin-ish outline with an excluded middle rectangle."""

    outer = [
        [13.10, 52.30],
        [13.80, 52.30],
        [13.80, 52.80],
        [13.10, 52.80],
        [13.10, 52.30],
    ]
    hole = [
        [13.35, 52.45],
        [13.45, 52.45],
        [13.45, 52.55],
        [13.35, 52.55],
        [13.35, 52.45],
    ]
    return json.dumps(
        {"type": "Polygon", "coordinates": [outer, hole]},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _bundle_with_replaced_boundary(boundary: bytes) -> dict[str, object]:
    files = _shipped_files()
    files["geography/berlin-boundary.geojson"] = boundary
    declaration = (
        files["geography.yaml"]
        .decode("utf-8")
        .replace(
            "sha256:d67bbca795b0a3f0b5741ba8609222e35cb81d3402df03fc403409e666ea0b9a",
            "sha256:" + sha256(boundary).hexdigest(),
        )
    )
    files["geography.yaml"] = declaration.encode("utf-8")
    return _compile(files).bundle()
