from __future__ import annotations

import json

import pytest

from collection_application.geography import (
    GeographyBoundaryError,
    GeographyCoverage,
    GeographyCoverageService,
    GeographyPoint,
    decode_boundary_geojson,
)


def square() -> bytes:
    return json.dumps(
        {
            "type": "Polygon",
            "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        },
        separators=(",", ":"),
    ).encode()


def test_boundary_geojson_is_canonical_and_digest_stable() -> None:
    first = decode_boundary_geojson(square())
    second = decode_boundary_geojson(
        b'{ "coordinates" : [ [ [0,0], [10,0], [10,10], [0,10], [0,0] ] ], "type" : "Polygon" }'
    )
    assert first.geometry_digest == second.geometry_digest
    assert first.canonical_geojson == second.canonical_geojson
    assert first.position_count == 5
    assert first.source_digest != second.source_digest


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (
            {"type": "Point", "coordinates": [1, 1]},
            "GEOGRAPHY_BOUNDARY_TYPE_INVALID",
        ),
        (
            {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]],
            },
            "GEOGRAPHY_BOUNDARY_RING_OPEN",
        ),
        (
            {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 0], [0, 0]]],
            },
            "GEOGRAPHY_BOUNDARY_RING_DEGENERATE",
        ),
        (
            {
                "type": "Polygon",
                "coordinates": [[[181, 0], [1, 0], [1, 1], [181, 0]]],
            },
            "GEOGRAPHY_BOUNDARY_COORDINATE_INVALID",
        ),
    ],
)
def test_invalid_boundary_contracts_fail_closed(
    document: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(GeographyBoundaryError) as error:
        decode_boundary_geojson(json.dumps(document).encode())
    assert error.value.code == code


class Port:
    def __init__(self, results: tuple[GeographyCoverage, ...]) -> None:
        self.results = results

    def evaluate(self, boundary: object, points: object):
        del boundary, points
        return self.results


def test_service_preserves_exact_point_order_and_boundary_digest() -> None:
    boundary = decode_boundary_geojson(square())
    points = (
        GeographyPoint("inside", 5, 5),
        GeographyPoint("edge", 5, 0),
    )
    expected = (
        GeographyCoverage("inside", boundary.geometry_digest, "inside"),
        GeographyCoverage("edge", boundary.geometry_digest, "boundary"),
    )
    assert GeographyCoverageService(Port(expected)).evaluate(boundary, points) == expected


def test_service_rejects_duplicate_point_keys_before_postgis() -> None:
    boundary = decode_boundary_geojson(square())
    service = GeographyCoverageService(Port(()))
    with pytest.raises(ValueError, match="unique"):
        service.evaluate(
            boundary,
            (
                GeographyPoint("same", 1, 1),
                GeographyPoint("same", 2, 2),
            ),
        )
