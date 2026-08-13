from __future__ import annotations

import json

import pytest

from osm_overpass import (
    GeoPoint,
    OsmTagFilter,
    OverpassPolygon,
    OverpassQuerySpec,
    OverpassResponseError,
    parse_overpass_response,
)


def spec(*, maximum_elements: int = 100) -> OverpassQuerySpec:
    return OverpassQuerySpec(
        polygon=OverpassPolygon(
            points=(GeoPoint(1, 1), GeoPoint(1, 2), GeoPoint(2, 1))
        ),
        element_types=("node", "relation", "way"),
        tag_filters=(OsmTagFilter(key="amenity", values=("studio",)),),
        maximum_elements=maximum_elements,
    )


def response(elements: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "version": 0.6,
            "generator": "Overpass API",
            "osm3s": {"timestamp_osm_base": "2026-08-13T00:00:00Z"},
            "elements": elements,
        },
        separators=(",", ":"),
    ).encode()


def test_response_preserves_identity_coordinates_address_and_attribution() -> None:
    batch = parse_overpass_response(
        response(
            [
                {
                    "type": "way",
                    "id": 20,
                    "center": {"lat": 52.51, "lon": 13.41},
                    "tags": {"name": "B", "addr:street": "Street"},
                },
                {
                    "type": "node",
                    "id": 10,
                    "lat": 52.50,
                    "lon": 13.40,
                    "tags": {"name": "A", "addr:housenumber": "1"},
                },
            ]
        ),
        spec=spec(),
    )
    assert [(item.element_type, item.osm_id) for item in batch.observations] == [
        ("node", 10),
        ("way", 20),
    ]
    assert batch.observations[0].source_url.endswith("/node/10")
    assert batch.observations[0].address.house_number == "1"
    assert batch.observations[1].address.street == "Street"
    assert batch.attribution == "© OpenStreetMap contributors"
    assert batch.digest.startswith("sha256:")
    assert batch.to_bytes() == batch.to_bytes()


def test_duplicate_element_identity_is_rejected() -> None:
    payload = response(
        [
            {"type": "node", "id": 1, "lat": 1, "lon": 1},
            {"type": "node", "id": 1, "lat": 1, "lon": 1},
        ]
    )
    with pytest.raises(ValueError, match="duplicate"):
        parse_overpass_response(payload, spec=spec())


def test_element_limit_is_enforced_before_parsing() -> None:
    payload = response(
        [
            {"type": "node", "id": 1, "lat": 1, "lon": 1},
            {"type": "node", "id": 2, "lat": 1, "lon": 1},
        ]
    )
    with pytest.raises(OverpassResponseError) as error:
        parse_overpass_response(payload, spec=spec(maximum_elements=1))
    assert error.value.code == "OVERPASS_RESPONSE_ELEMENT_LIMIT_EXCEEDED"


def test_missing_center_fails_closed_with_element_context() -> None:
    with pytest.raises(OverpassResponseError) as error:
        parse_overpass_response(
            response([{"type": "relation", "id": 1, "tags": {}}]),
            spec=spec(),
        )
    assert error.value.code == "OVERPASS_RESPONSE_OBJECT_INVALID"
