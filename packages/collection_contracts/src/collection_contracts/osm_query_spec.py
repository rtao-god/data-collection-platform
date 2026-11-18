"""Campaign-owned Overpass query specification.

The OSM connector owns how a query is rendered into Overpass QL. Collection needs to
produce the immutable `overpass_query_spec` artifact the worker consumes, so the
document shape lives here — in the shared contracts layer — and is the single source
of truth for both producers and the deployment that validates it.

Deliberately free of any connector import: `collection_application` must not depend on
`connectors/*`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

QUERY_SPEC_CONTRACT = "osm-overpass-query-spec@1"
_QUERY_SPEC_REVISION = QUERY_SPEC_CONTRACT
_SCHEMA_REVISION: Literal["overpass-query-spec-v1"] = "overpass-query-spec-v1"
_TAG_KEY = re.compile(r"^[A-Za-z0-9_:.-]{1,64}$")
_MIN_POLYGON_POINTS = 3
_MAX_POLYGON_POINTS = 5_000
_MAX_TAG_FILTERS = 100
_MAX_TIMEOUT_SECONDS = 180
_MAX_ELEMENTS = 100_000
_MAX_PARTITIONS = 512


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _QueryModel(BaseModel):
    # The canonical document is camelCase, so the model must be able to read back
    # exactly the bytes this module writes.
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, alias_generator=_camel
    )


class OverpassPoint(_QueryModel):
    latitude: float
    longitude: float

    @model_validator(mode="after")
    def validate_point(self) -> OverpassPoint:
        if not math.isfinite(self.latitude) or not -90 <= self.latitude <= 90:
            raise ValueError("Overpass polygon latitude is outside the WGS84 range")
        if not math.isfinite(self.longitude) or not -180 <= self.longitude <= 180:
            raise ValueError("Overpass polygon longitude is outside the WGS84 range")
        return self


class OverpassTagFilter(_QueryModel):
    key: str
    values: tuple[str, ...]

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if _TAG_KEY.fullmatch(value) is None:
            raise ValueError("Overpass tag filter key is invalid")
        return value

    @field_validator("values")
    @classmethod
    def validate_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not 1 <= len(value) <= 100:
            raise ValueError("Overpass tag filter must hold between 1 and 100 values")
        if not all(item and len(item) <= 128 for item in value):
            raise ValueError("Overpass tag filter values must be non-empty and bounded")
        if tuple(sorted(set(value))) != value:
            raise ValueError("Overpass tag filter values must be unique and sorted")
        return value


class OverpassQuerySpecDocument(_QueryModel):
    """Exactly the document the OSM connector renders into Overpass QL."""

    schema_revision: Literal["overpass-query-spec-v1"] = _SCHEMA_REVISION
    element_types: tuple[Literal["node", "relation", "way"], ...]
    tag_filters: tuple[OverpassTagFilter, ...]
    polygon: tuple[OverpassPoint, ...]
    timeout_seconds: int = 60
    maximum_elements: int = 25_000

    @field_validator("element_types")
    @classmethod
    def validate_element_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("Overpass query requires at least one element type")
        if tuple(sorted(set(value))) != value:
            raise ValueError("Overpass element types must be unique and sorted")
        return value

    @field_validator("tag_filters")
    @classmethod
    def validate_tag_filters(
        cls, value: tuple[OverpassTagFilter, ...]
    ) -> tuple[OverpassTagFilter, ...]:
        if not 1 <= len(value) <= _MAX_TAG_FILTERS:
            raise ValueError("Overpass query requires between 1 and 100 tag filters")
        identities = tuple((item.key, item.values) for item in value)
        if tuple(sorted(set(identities))) != identities:
            raise ValueError("Overpass tag filters must be unique and sorted")
        return value

    @field_validator("polygon", mode="before")
    @classmethod
    def decode_polygon(cls, value: object) -> object:
        """Accept the canonical ``[latitude, longitude]`` pairs this module writes."""
        if not isinstance(value, (list, tuple)):
            return value
        decoded = []
        for entry in value:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                decoded.append({"latitude": entry[0], "longitude": entry[1]})
            else:
                decoded.append(entry)
        return tuple(decoded)

    @field_validator("polygon")
    @classmethod
    def validate_polygon(cls, value: tuple[OverpassPoint, ...]) -> tuple[OverpassPoint, ...]:
        if not _MIN_POLYGON_POINTS <= len(value) <= _MAX_POLYGON_POINTS:
            raise ValueError("Overpass polygon must contain between 3 and 5000 points")
        if len({(item.latitude, item.longitude) for item in value}) < _MIN_POLYGON_POINTS:
            raise ValueError("Overpass polygon must contain three distinct points")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: int) -> int:
        if not 1 <= value <= _MAX_TIMEOUT_SECONDS:
            raise ValueError("Overpass timeout must be between 1 and 180 seconds")
        return value

    @field_validator("maximum_elements")
    @classmethod
    def validate_maximum_elements(cls, value: int) -> int:
        if not 1 <= value <= _MAX_ELEMENTS:
            raise ValueError("Overpass element limit must be between 1 and 100000")
        return value


def canonical_query_spec_document(spec: OverpassQuerySpecDocument) -> bytes:
    """Render the exact bytes the OSM connector hashes and replays.

    Field order and formatting are pinned so a replayed campaign run publishes an
    identical artifact digest.
    """

    document = {
        "elementTypes": list(spec.element_types),
        "maximumElements": spec.maximum_elements,
        "polygon": [[item.latitude, item.longitude] for item in spec.polygon],
        "schemaRevision": _SCHEMA_REVISION,
        "tagFilters": [{"key": item.key, "values": list(item.values)} for item in spec.tag_filters],
        "timeoutSeconds": spec.timeout_seconds,
    }
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def query_spec_digest(spec: OverpassQuerySpecDocument) -> str:
    return "sha256:" + hashlib.sha256(canonical_query_spec_document(spec)).hexdigest()


def _ring_points(ring: object, *, part: int, ring_index: int) -> tuple[OverpassPoint, ...]:
    if not isinstance(ring, list):
        raise ValueError(f"geography boundary part {part} ring {ring_index} is not a list")
    points: list[OverpassPoint] = []
    for position, entry in enumerate(ring):
        if not isinstance(entry, list | tuple) or len(entry) < 2:
            raise ValueError(
                f"geography boundary part {part} ring {ring_index} position "
                f"{position} is not a point"
            )
        longitude_raw, latitude_raw = entry[0], entry[1]
        if not isinstance(longitude_raw, (int, float)) or isinstance(longitude_raw, bool):
            raise ValueError(f"geography boundary longitudes at {part}/{position} are not numeric")
        if not isinstance(latitude_raw, (int, float)) or isinstance(latitude_raw, bool):
            raise ValueError(f"geography boundary latitudes at {part}/{position} are not numeric")
        points.append(OverpassPoint(latitude=float(latitude_raw), longitude=float(longitude_raw)))
    # GeoJSON closes the ring by repeating the first position; Overpass wants the
    # open ring, and dropping the duplicate loses no coverage.
    if len(points) >= 2 and points[0] == points[-1]:
        points.pop()
    return tuple(points)


def _ring_area(ring: tuple[OverpassPoint, ...]) -> float:
    """Shoelace area; zero means the ring is degenerate and covers nothing."""
    total = 0.0
    for index, point in enumerate(ring):
        following = ring[(index + 1) % len(ring)]
        total += point.longitude * following.latitude - following.longitude * point.latitude
    return abs(total) / 2


class OverpassPartition(_QueryModel):
    """One Overpass-representable piece of the boundary.

    ``ring`` is the query polygon. ``excluded_holes`` lists interior rings this piece
    cannot exclude through Overpass (an Overpass polygon has no holes), so the queried
    area is a declared over-approximation by exactly those rings.
    """

    ring: tuple[OverpassPoint, ...]
    excluded_holes: tuple[tuple[OverpassPoint, ...], ...] = ()

    @property
    def covers_exactly(self) -> bool:
        return not self.excluded_holes


class _BoundaryShape:
    """One polygon part of the boundary: an exterior ring and its interior holes.

    Rings are held as ``(latitude, longitude)`` floats, exactly as the boundary
    document carries them, so clipping cannot drift the coordinates it passes through.
    """

    __slots__ = ("_rings",)

    def __init__(
        self,
        exterior: Sequence[object],
        *holes: Sequence[object],
    ) -> None:
        rings = [list(exterior), *(list(hole) for hole in holes)]
        parsed = [
            _open_ring(_raw_ring(ring, part=0, index=position), index=position)
            for position, ring in enumerate(rings)
        ]
        exterior_ring = parsed[0]
        if len(exterior_ring) < _MIN_POLYGON_POINTS:
            raise ValueError("geography boundary part has fewer than 3 points")
        if _raw_area(exterior_ring) == 0:
            raise ValueError("geography boundary part has no area")
        self._rings = tuple(parsed)

    def rings(self) -> tuple[tuple[OverpassPoint, ...], ...]:
        return tuple(
            tuple(OverpassPoint(latitude=lat, longitude=lon) for lat, lon in ring)
            for ring in self._rings
        )

    def exterior_only(self) -> tuple[tuple[OverpassPoint, ...], ...]:
        """The exterior ring alone, used when a hole cannot be clipped away."""

        return self.rings()[:1]

    def ring_count(self) -> int:
        return len(self._rings)

    def point_count(self) -> int:
        return sum(len(ring) for ring in self._rings)

    def halves(self) -> tuple[_BoundaryShape, _BoundaryShape] | None:
        """Split by the longer extent's midline; ``None`` when no split is possible."""

        clipped = self._halves_on_longer_extent()
        if clipped is None or len(clipped) != 2:
            return None
        left, right = clipped
        if left.point_count() >= self.point_count() or right.point_count() >= self.point_count():
            return None
        return left, right

    def _halves_on_longer_extent(self) -> tuple[_BoundaryShape, ...] | None:
        latitudes = [lat for ring in self._rings for lat, _ in ring]
        longitudes = [lon for ring in self._rings for _, lon in ring]
        lat_span = max(latitudes) - min(latitudes)
        lon_span = max(longitudes) - min(longitudes)
        for use_latitude in (lon_span >= lat_span, not (lon_span >= lat_span)):
            if use_latitude:
                values = latitudes
                low, high = min(values), max(values)
                if low == high:
                    continue
                cut = (low + high) / 2
                return (
                    self._clip(_below(cut, keep_latitude=True), keep_latitude=True, cut=cut),
                    self._clip(_above(cut, keep_latitude=True), keep_latitude=True, cut=cut),
                )
            values = longitudes
            low, high = min(values), max(values)
            if low == high:
                continue
            cut = (low + high) / 2
            return (
                self._clip(_below(cut, keep_latitude=False), keep_latitude=False, cut=cut),
                self._clip(_above(cut, keep_latitude=False), keep_latitude=False, cut=cut),
            )
        return None

    def _clip(
        self,
        inside: Callable[[float, float], bool],
        *,
        keep_latitude: bool,
        cut: float,
    ) -> _BoundaryShape:
        exterior = _clip_ring(self._rings[0], inside, keep_latitude=keep_latitude, cut=cut)
        holes: list[list[list[float]]] = [
            [[lon, lat] for lat, lon in clipped]
            for ring in self._rings[1:]
            if (clipped := _clip_ring(ring, inside, keep_latitude=keep_latitude, cut=cut))
        ]
        # _BoundaryShape parses GeoJSON order, so re-wrap the (lat, lon) tuples.
        return _BoundaryShape([[lon, lat] for lat, lon in exterior], *holes)


_AxisPredicate = Callable[[float, float], bool]


def _below(cut: float, *, keep_latitude: bool) -> _AxisPredicate:
    """Predicate keeping the lower half of the chosen axis."""

    def below(latitude: float, longitude: float) -> bool:
        return (latitude if keep_latitude else longitude) <= cut

    return below


def _above(cut: float, *, keep_latitude: bool) -> _AxisPredicate:
    """Predicate keeping the upper half of the chosen axis."""

    def above(latitude: float, longitude: float) -> bool:
        return (latitude if keep_latitude else longitude) >= cut

    return above


def _raw_ring(ring: object, *, part: int, index: int) -> list[tuple[float, float]]:
    if not isinstance(ring, list):
        raise ValueError(f"geography boundary part {part} ring {index} is not a list")
    points: list[tuple[float, float]] = []
    for position, entry in enumerate(ring):
        if not isinstance(entry, list | tuple) or len(entry) < 2:
            raise ValueError(
                f"geography boundary part {part} ring {index} position {position} is not a point"
            )
        longitude_raw, latitude_raw = entry[0], entry[1]
        if not isinstance(longitude_raw, (int, float)) or isinstance(longitude_raw, bool):
            raise ValueError(f"geography boundary longitudes at {part}/{position} are not numeric")
        if not isinstance(latitude_raw, (int, float)) or isinstance(latitude_raw, bool):
            raise ValueError(f"geography boundary latitudes at {part}/{position} are not numeric")
        points.append((float(latitude_raw), float(longitude_raw)))
    return points


def _open_ring(points: list[tuple[float, float]], *, index: int) -> list[tuple[float, float]]:
    """Drop the closing duplicate GeoJSON adds and reject empty rings."""

    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    if not points:
        raise ValueError(f"geography boundary ring {index} is empty")
    return points


def _raw_area(ring: list[tuple[float, float]]) -> float:
    total = 0.0
    for position, (latitude, longitude) in enumerate(ring):
        next_latitude, next_longitude = ring[(position + 1) % len(ring)]
        total += longitude * next_latitude - next_longitude * latitude
    return abs(total) / 2


def _clip_ring(
    ring: list[tuple[float, float]],
    inside: Callable[[float, float], bool],
    *,
    keep_latitude: bool,
    cut: float,
) -> list[tuple[float, float]]:
    """Sutherland-Hodgman clip of one ring by an axis-aligned half-plane."""

    if not ring:
        return []
    output: list[tuple[float, float]] = []
    previous = ring[-1]
    previous_inside = inside(*previous)
    for current in ring:
        current_inside = inside(*current)
        if current_inside != previous_inside:
            output.append(_intersection(previous, current, keep_latitude=keep_latitude, cut=cut))
        if current_inside:
            output.append(current)
        previous, previous_inside = current, current_inside
    if len(output) >= 2 and output[0] == output[-1]:
        output = output[:-1]
    return output


def _intersection(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    keep_latitude: bool,
    cut: float,
) -> tuple[float, float]:
    start_latitude, start_longitude = start
    end_latitude, end_longitude = end
    if keep_latitude:
        if end_latitude == start_latitude:
            return cut, start_longitude
        ratio = (cut - start_latitude) / (end_latitude - start_latitude)
        return cut, start_longitude + ratio * (end_longitude - start_longitude)
    if end_longitude == start_longitude:
        return start_latitude, cut
    ratio = (cut - start_longitude) / (end_longitude - start_longitude)
    return start_latitude + ratio * (end_latitude - start_latitude), cut


def _shape_area(shape: _BoundaryShape) -> float:
    exterior, *holes = shape._rings
    return _raw_area(exterior) - sum(_raw_area(hole) for hole in holes)


def _representable(shape: _BoundaryShape, maximum_ring_points: int) -> bool:
    """An Overpass loop carries one hole-free ring within the connector limit."""

    return shape.ring_count() == 1 and shape.point_count() <= maximum_ring_points


def _boundary_polygons(document: object) -> list[list[list[object]]]:
    """Normalise a GeoJSON boundary into a list of polygons, each a list of rings."""

    if not isinstance(document, dict):
        raise ValueError("geography boundary document must be a GeoJSON object")
    geometry = document.get("geometry") if document.get("type") == "Feature" else document
    if not isinstance(geometry, dict):
        raise ValueError("geography boundary has no geometry object")
    coordinates = geometry.get("coordinates")
    geometry_type = geometry.get("type")
    if geometry_type == "Polygon":
        if not isinstance(coordinates, list):
            raise ValueError("geography boundary polygon coordinates are not a list")
        return [coordinates]
    if geometry_type == "MultiPolygon":
        if not isinstance(coordinates, list) or not coordinates:
            raise ValueError("geography boundary multipolygon coordinates are empty")
        for index, polygon in enumerate(coordinates):
            if not isinstance(polygon, list) or not polygon:
                raise ValueError(f"geography boundary part {index} has no rings")
        return coordinates
    raise ValueError(
        f"Overpass query requires a Polygon or MultiPolygon boundary; got {geometry_type!r}"
    )


def partition_boundary(
    document: object,
    *,
    maximum_ring_points: int = _MAX_POLYGON_POINTS,
    maximum_partitions: int = _MAX_PARTITIONS,
) -> tuple[OverpassPartition, ...]:
    """Split a verified boundary into Overpass-representable exterior rings.

    An Overpass ``poly`` filter is a single hole-free ring, so a boundary with holes or
    with more than `maximum_ring_points` vertices cannot be sent verbatim. Splitting a
    ring into contiguous vertex windows would close each window with a straight chord
    and therefore query a different region than the boundary describes.

    Instead the boundary is clipped by axis-aligned half-planes, recursively halving the
    extent until every piece is expressible. Clipping a polygon by a rectangle is exact:
    the union of the pieces is the original geometry (holes included), no chord is
    introduced across the interior, and no vertex is dropped. Partitioning is a pure
    function of the boundary, so a replayed run reproduces identical rings.
    """

    if not 2 <= maximum_ring_points <= _MAX_POLYGON_POINTS:
        raise ValueError("maximum ring size is outside the supported range")
    if maximum_partitions < 1:
        raise ValueError("maximum partition count must be positive")
    shapes: list[_BoundaryShape] = []
    for polygon in _boundary_polygons(document):
        rings: list[list[object]] = polygon
        shapes.append(_BoundaryShape(rings[0], *rings[1:]))
    if not shapes:
        raise ValueError("geography boundary has no polygon parts")

    pending = list(shapes)
    partitions: list[OverpassPartition] = []
    budget = maximum_partitions
    while pending:
        budget -= 1
        if budget < 0:
            raise ValueError(f"geography boundary needs more than {maximum_partitions} partitions")
        current = pending.pop(0)
        if current.point_count() and _shape_area(current) == 0:
            # A clip fragment with no area covers nothing and must not be queried.
            continue
        if _representable(current, maximum_ring_points):
            partitions.append(OverpassPartition(ring=current.rings()[0]))
            continue
        halves = current.halves() if current.point_count() > maximum_ring_points else None
        if halves is None:
            # A fragment small enough for the limit whose hole survives every clip:
            # Overpass cannot express it, so the exterior is queried and the hole is
            # reported as an exclusion instead of being silently filled in.
            if current.point_count() <= maximum_ring_points:
                exterior, *holes = current.rings()
                partitions.append(OverpassPartition(ring=exterior, excluded_holes=tuple(holes)))
                continue
            raise ValueError(
                "geography boundary cannot be partitioned into hole-free rings under "
                f"the {maximum_ring_points}-point limit"
            )
        pending[:0] = list(halves)
    if not partitions:
        raise ValueError("geography boundary produced no Overpass-representable rings")
    return tuple(partitions)


def boundary_exclusions(document: object) -> tuple[tuple[OverpassPoint, ...], ...]:
    """Interior rings, returned so callers can record what the boundary excludes."""

    polygons = _boundary_polygons(document)
    return tuple(
        _ring_points(ring, part=part, ring_index=index)
        for part, polygon in enumerate(polygons)
        for index, ring in enumerate(polygon[1:], start=1)
    )


def _winding_contains(
    ring: list[tuple[float, float]],
    latitude: float,
    longitude: float,
) -> bool:
    """Even-odd point-in-ring test; rings are held as (latitude, longitude)."""

    inside = False
    count = len(ring)
    for index in range(count):
        lat_a, lon_a = ring[index]
        lat_b, lon_b = ring[(index + 1) % count]
        if (lat_a > latitude) != (lat_b > latitude):
            crossings = (lon_b - lon_a) * (latitude - lat_a) / (lat_b - lat_a) + lon_a
            if longitude < crossings:
                inside = not inside
    return inside


def boundary_contains(
    document: object,
    latitude: float,
    longitude: float,
) -> bool:
    """Report whether a coordinate lies inside the boundary proper, holes excluded.

    An Overpass `poly` filter cannot express interior holes, so a partition query
    over-approximates by exactly the declared holes. This predicate applies the exact
    original boundary afterwards: a point inside an exterior ring but also inside one of
    its interior rings is outside the boundary.
    """

    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        raise ValueError("boundary containment requires finite coordinates")
    polygons = _boundary_polygons(document)
    for part, polygon in enumerate(polygons):
        exterior = _open_ring(_raw_ring(polygon[0], part=part, index=0), index=0)
        if not _winding_contains(exterior, latitude, longitude):
            continue
        in_hole = any(
            _winding_contains(
                _open_ring(_raw_ring(ring, part=part, index=index), index=index),
                latitude,
                longitude,
            )
            for index, ring in enumerate(polygon[1:], start=1)
        )
        if not in_hole:
            return True
    return False
