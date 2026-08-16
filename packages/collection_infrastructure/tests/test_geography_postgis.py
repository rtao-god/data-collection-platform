from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import create_engine

from collection_application.geography import (
    GeographyCoverageService,
    GeographyEvaluationError,
    GeographyPoint,
    decode_boundary_geojson,
)
from collection_infrastructure.postgres.geography import PostgresGeographyCoverage

pytestmark = pytest.mark.integration


def database_url() -> str:
    for name in (
        "COLLECTOR_TEST_DATABASE_URL",
        "COLLECTOR_DATABASE_URL",
        "DATABASE_URL",
    ):
        value = os.getenv(name)
        if value:
            return value
    pytest.skip("PostgreSQL integration database is not configured")


def polygon(coordinates: list[list[list[float]]]) -> bytes:
    return json.dumps(
        {"type": "Polygon", "coordinates": coordinates},
        separators=(",", ":"),
    ).encode()


def test_postgis_covers_includes_edges_and_vertices() -> None:
    engine = create_engine(database_url(), pool_pre_ping=True)
    try:
        service = GeographyCoverageService(PostgresGeographyCoverage(engine))
        boundary = decode_boundary_geojson(polygon([[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]))
        results = service.evaluate(
            boundary,
            (
                GeographyPoint("inside", 5, 5),
                GeographyPoint("edge", 5, 0),
                GeographyPoint("vertex", 0, 0),
                GeographyPoint("outside", 5, -1),
            ),
        )
        assert [result.coverage for result in results] == [
            "inside",
            "boundary",
            "boundary",
            "outside",
        ]
    finally:
        engine.dispose()


def test_postgis_rejects_topologically_invalid_polygon() -> None:
    engine = create_engine(database_url(), pool_pre_ping=True)
    try:
        service = GeographyCoverageService(PostgresGeographyCoverage(engine))
        bow_tie = decode_boundary_geojson(polygon([[[0, 0], [10, 10], [0, 10], [10, 0], [0, 0]]]))
        with pytest.raises(GeographyEvaluationError) as error:
            service.evaluate(bow_tie, (GeographyPoint("point", 5, 5),))
        assert error.value.code == "GEOGRAPHY_BOUNDARY_TOPOLOGY_INVALID"
    finally:
        engine.dispose()
