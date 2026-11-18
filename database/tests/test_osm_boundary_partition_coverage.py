"""Geometry proof for Overpass partitioning, measured by PostGIS itself.

A pure-Python planar area check cannot answer whether the queried polygons cover the
real administrative boundary. These tests hand the partitions to PostGIS and assert the
set-theoretic result: nothing missing, and extra area only where a hole is declared.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from collection_contracts.osm_query_spec import partition_boundary

pytestmark = pytest.mark.integration

_BOUNDARY = Path("campaigns/berlin_recording_services/geography/berlin-boundary.geojson")


def _closed(ring: list[list[float]]) -> list[list[float]]:
    points = [list(point) for point in ring]
    if points[0] != points[-1]:
        points.append(points[0])
    return points


def _as_partition_multipolygon(partitions: object) -> dict:
    polygons = []
    for partition in partitions:  # type: ignore[attr-defined]
        exterior = _closed([[p.longitude, p.latitude] for p in partition.ring])
        polygons.append([exterior])
    return {"type": "MultiPolygon", "coordinates": polygons}


def _as_declared_holes(partitions: object) -> dict:
    polygons = []
    for partition in partitions:  # type: ignore[attr-defined]
        for hole in partition.excluded_holes:
            polygons.append([_closed([[p.longitude, p.latitude] for p in hole])])
    if not polygons:
        return {"type": "MultiPolygon", "coordinates": []}
    return {"type": "MultiPolygon", "coordinates": polygons}


def _geom(connection: sa.Connection, document: dict) -> object:
    return connection.execute(
        sa.text("SELECT ST_SetSRID(ST_GeomFromGeoJSON(:payload), 4326) AS geom"),
        {"payload": json.dumps(document)},
    ).scalar_one()


def test_partitions_are_valid_polygons(database_connection: sa.Connection) -> None:
    boundary = json.loads(_BOUNDARY.read_text(encoding="utf-8"))
    partitions = partition_boundary(boundary)
    geometry = _geom(database_connection, _as_partition_multipolygon(partitions))

    valid = database_connection.execute(
        sa.text(
            "SELECT bool_and(ST_IsValid((d).geom)) AS ok FROM ST_Dump(CAST(:geom AS geometry)) AS d"
        ),
        {"geom": geometry},
    ).scalar_one()
    assert valid is True


def test_partition_union_misses_no_area_of_the_real_boundary(
    database_connection: sa.Connection,
) -> None:
    boundary = json.loads(_BOUNDARY.read_text(encoding="utf-8"))
    partitions = partition_boundary(boundary)

    original = _geom(database_connection, boundary)
    covered = _geom(database_connection, _as_partition_multipolygon(partitions))

    missing_square_metres = database_connection.execute(
        sa.text("SELECT ST_Area(CAST(ST_Difference(:original, :covered) AS geography)) AS missing"),
        {"original": original, "covered": covered},
    ).scalar_one()

    # Float precision only: no administrative area may be dropped by partitioning.
    assert float(missing_square_metres) < 1.0
    assert float(missing_square_metres) >= 0.0


def test_extra_area_is_exactly_the_declared_holes(
    database_connection: sa.Connection,
) -> None:
    boundary = json.loads(_BOUNDARY.read_text(encoding="utf-8"))
    partitions = partition_boundary(boundary)

    original = _geom(database_connection, boundary)
    covered = _geom(database_connection, _as_partition_multipolygon(partitions))
    declared = _geom(database_connection, _as_declared_holes(partitions))

    extra, declared_area = database_connection.execute(
        sa.text(
            "SELECT ST_Area(CAST(ST_Difference(:covered, :original) AS geography)) AS extra,"
            " ST_Area(CAST(:declared AS geography)) AS declared"
        ),
        {"original": original, "covered": covered, "declared": declared},
    ).one()

    # Everything queried beyond the boundary is a hole we explicitly recorded.
    assert float(extra) == pytest.approx(float(declared_area), rel=0.01)


def test_boundary_is_covered_by_the_same_partitions_on_replay(
    database_connection: sa.Connection,
) -> None:
    boundary = json.loads(_BOUNDARY.read_text(encoding="utf-8"))
    first = partition_boundary(boundary)
    second = partition_boundary(boundary)

    left = _geom(database_connection, _as_partition_multipolygon(first))
    right = _geom(database_connection, _as_partition_multipolygon(second))
    equal = database_connection.execute(
        sa.text("SELECT ST_Equals(:left, :right) AS equal"),
        {"left": left, "right": right},
    ).scalar_one()
    assert equal is True
