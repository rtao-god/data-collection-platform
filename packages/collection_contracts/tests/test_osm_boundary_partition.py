"""Topology-preserving boundary partitioning.

An Overpass ``poly`` filter is one hole-free ring within a point limit. Partitioning
must therefore produce pieces whose union is the original geometry: no missing area, no
area invented across a chord. These tests pin that geometry property directly, including
the concave, holed and multipart cases that a vertex-window split gets wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collection_contracts.osm_query_spec import (
    OverpassPartition,
    partition_boundary,
)

_LIMIT = 5_000


def _polygon(*rings: list[list[float]]) -> dict:
    return {"type": "Polygon", "coordinates": list(rings)}


def _multipolygon(*parts: list[list[list[float]]]) -> dict:
    return {"type": "MultiPolygon", "coordinates": list(parts)}


def _square(left: float, bottom: float, size: float) -> list[list[float]]:
    return [
        [left, bottom],
        [left + size, bottom],
        [left + size, bottom + size],
        [left, bottom + size],
        [left, bottom],
    ]


def _ring_area(ring: list[list[float]]) -> float:
    points = [(float(latitude), float(longitude)) for longitude, latitude in ring]
    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    total = 0.0
    for index, (latitude, longitude) in enumerate(points):
        next_latitude, next_longitude = points[(index + 1) % len(points)]
        total += longitude * next_latitude - next_longitude * latitude
    return total / 2


def _partition_area(partition: OverpassPartition) -> float:
    ring = [[point.longitude, point.latitude] for point in partition.ring]
    return _ring_area(ring) - sum(
        _ring_area([[point.longitude, point.latitude] for point in hole])
        for hole in partition.excluded_holes
    )


def _covered_area(partitions: tuple[OverpassPartition, ...]) -> float:
    # Exterior-only pieces query their hole, so the covered area is the ring area and
    # the hole is reported separately as a declared exclusion.
    return sum(
        _ring_area([[point.longitude, point.latitude] for point in partition.ring])
        for partition in partitions
    )


def _boundary_area(document: dict) -> float:
    parts = [document["coordinates"]] if document["type"] == "Polygon" else document["coordinates"]
    return sum(_ring_area(part[0]) - sum(_ring_area(hole) for hole in part[1:]) for part in parts)


def test_convex_small_polygon_is_one_exact_partition() -> None:
    partitions = partition_boundary(_polygon(_square(13.0, 52.0, 0.01)))

    assert len(partitions) == 1
    assert partitions[0].covers_exactly
    assert _covered_area(partitions) == pytest.approx(
        _boundary_area(_polygon(_square(13.0, 52.0, 0.01))), rel=1e-12
    )


def test_concave_polygon_is_not_split_into_chords() -> None:
    # A large U shape (deep concave notch). Vertex-window splitting would close each
    # window with a chord across the notch and query area the boundary excludes.
    notch_ring: list[list[float]] = []
    left, right, bottom, top = 13.0, 13.10, 52.0, 52.10
    notch_left, notch_right, notch_bottom = 13.04, 13.06, 52.04
    steps = 600
    for index in range(steps):
        notch_ring.append([left + (right - left) * index / steps, bottom])
    for index in range(steps):
        notch_ring.append([right, bottom + (top - bottom) * index / steps])
    for index in range(steps):
        notch_ring.append([right - (right - left) * index / steps, top])
    for index in range(steps):
        notch_ring.append([notch_right, top - (top - notch_bottom) * index / steps])
    for index in range(200):
        notch_ring.append([notch_right - (notch_right - notch_left) * index / 200, notch_bottom])
    for index in range(steps):
        notch_ring.append([notch_left, notch_bottom + (top - notch_bottom) * index / steps])
    for index in range(steps):
        notch_ring.append([notch_left - (notch_left - left) * index / steps, top])
    for index in range(steps):
        notch_ring.append([left, top - (top - bottom) * index / steps])
    notch_ring.append(notch_ring[0])
    document = _polygon(notch_ring)

    partitions = partition_boundary(document, maximum_ring_points=1_200)

    assert len(partitions) > 1
    assert all(len(partition.ring) <= 1_200 for partition in partitions)
    # Clipping is exact: the concave notch is neither filled in nor cut away.
    assert _covered_area(partitions) == pytest.approx(_boundary_area(document), rel=1e-9)


def test_hole_is_kept_exact_when_it_fits_and_declared_when_it_does_not() -> None:
    outer = _square(13.0, 52.0, 0.04)
    inner = _square(13.015, 52.015, 0.005)
    document = _polygon(outer, inner)

    partitions = partition_boundary(document)
    assert len(partitions) == 1
    # The partition carries the hole: the queried area is a declared over-approximation.
    assert not partitions[0].covers_exactly
    assert len(partitions[0].excluded_holes) == 1
    # The hole is excluded by declaration, so the net covered area matches the document.
    assert _covered_area(partitions) - _ring_area(inner) == pytest.approx(
        _boundary_area(document), rel=1e-9
    )
    assert _covered_area(partitions) == pytest.approx(
        _boundary_area(document) + _ring_area(inner), rel=1e-9
    )


def test_multipart_boundary_keeps_every_component() -> None:
    document = _multipolygon(
        [_square(13.0, 52.0, 0.01)],
        [_square(13.5, 52.5, 0.01)],
        [_square(13.9, 52.9, 0.005)],
    )
    partitions = partition_boundary(document)

    assert len(partitions) == 3
    assert _covered_area(partitions) == pytest.approx(_boundary_area(document), rel=1e-9)
    # Each component is represented; none may be dropped as a redundant piece.
    centres = sorted(
        round(sum(p.longitude for p in part.ring) / len(part.ring), 1) for part in partitions
    )
    assert centres == [13.0, 13.5, 13.9]


def test_partition_is_deterministic_for_replay() -> None:
    document = _multipolygon(
        [_square(13.0, 52.0, 0.02)],
        [_square(13.5, 52.5, 0.02)],
    )
    first = partition_boundary(document, maximum_ring_points=8)
    second = partition_boundary(document, maximum_ring_points=8)

    assert [tuple(part.ring) for part in first] == [tuple(part.ring) for part in second]
    assert [part.excluded_holes for part in first] == [part.excluded_holes for part in second]


def test_partition_limit_is_enforced_rather_than_truncated() -> None:
    document = _multipolygon(
        [_square(13.0, 52.0, 0.02)],
        [_square(13.5, 52.5, 0.02)],
    )
    with pytest.raises(ValueError, match="more than 1 partitions"):
        partition_boundary(document, maximum_ring_points=4, maximum_partitions=1)


def test_zero_area_ring_is_rejected() -> None:
    degenerate = [[13.0, 52.0], [13.0, 52.0], [13.0, 52.0], [13.0, 52.0]]
    with pytest.raises(ValueError, match="no area"):
        partition_boundary(_polygon(degenerate))


def test_berlin_boundary_covers_the_real_administrative_area() -> None:
    boundary = json.loads(
        Path("campaigns/berlin_recording_services/geography/berlin-boundary.geojson").read_text(
            encoding="utf-8"
        )
    )
    partitions = partition_boundary(boundary)

    assert all(len(partition.ring) <= _LIMIT for partition in partitions)
    # Covered area equals the boundary plus exactly the declared hole it cannot exclude.
    declared_hole_area = sum(
        _ring_area([[point.longitude, point.latitude] for point in hole])
        for partition in partitions
        for hole in partition.excluded_holes
    )
    assert _covered_area(partitions) == pytest.approx(
        _boundary_area(boundary) + declared_hole_area, rel=1e-9
    )
    # Only the tiny interior ring is unfetchable, and it is declared, not silent.
    inexact = [partition for partition in partitions if not partition.covers_exactly]
    assert len(inexact) == 1
    assert sum(len(hole) for part in inexact for hole in part.excluded_holes) == 6
