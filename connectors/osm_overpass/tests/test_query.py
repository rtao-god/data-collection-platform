from __future__ import annotations

import pytest

from osm_overpass import (
    GeoPoint,
    OsmTagFilter,
    OverpassPolygon,
    OverpassQuerySpec,
    build_overpass_query,
    query_digest,
)


def spec() -> OverpassQuerySpec:
    return OverpassQuerySpec(
        polygon=OverpassPolygon(
            points=(
                GeoPoint(52.50, 13.30),
                GeoPoint(52.60, 13.30),
                GeoPoint(52.55, 13.50),
            )
        ),
        element_types=("node", "relation", "way"),
        tag_filters=(
            OsmTagFilter(key="amenity", values=("music_venue",)),
            OsmTagFilter(key="studio", values=("recording",)),
        ),
        timeout_seconds=45,
        maximum_elements=1_000,
    )


def test_query_is_deterministic_and_contains_only_typed_selectors() -> None:
    first = build_overpass_query(spec())
    second = build_overpass_query(spec())
    assert first == second
    assert first.startswith("[out:json][timeout:45];(")
    assert first.endswith(");out body center qt;")
    assert 'node["amenity"~"^(?:music_venue)$"]' in first
    assert query_digest(spec()).startswith("sha256:")


def test_tag_value_is_regex_escaped_instead_of_becoming_query_syntax() -> None:
    value = "recording|live.*"
    query = build_overpass_query(
        OverpassQuerySpec(
            polygon=spec().polygon,
            element_types=("node",),
            tag_filters=(OsmTagFilter(key="studio", values=(value,)),),
        )
    )
    assert "recording\\|live\\.\\*" in query
    assert "recording|live.*" not in query


@pytest.mark.parametrize("key", ['amenity\"] ; out;', "a\nkey", ""])
def test_tag_key_cannot_inject_overpass_ql(key: str) -> None:
    with pytest.raises(ValueError):
        OsmTagFilter(key=key, values=("value",))
