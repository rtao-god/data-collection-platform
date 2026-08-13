from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol, cast

type GeographyCoverageKind = Literal["inside", "boundary", "outside"]

_MAX_BOUNDARY_BYTES = 16 * 1024 * 1024
_MAX_POSITIONS = 100_000
_POINT_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class GeographyBoundaryError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.context = dict(context or {})
        super().__init__(message)


class GeographyEvaluationError(RuntimeError):
    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GeographyBoundaryArtifact:
    source_digest: str
    geometry_digest: str
    canonical_geojson: bytes
    geometry_type: Literal["Polygon", "MultiPolygon"]
    position_count: int

    def __post_init__(self) -> None:
        if _SHA256_PATTERN.fullmatch(self.source_digest) is None:
            raise ValueError("geography boundary source digest is invalid")
        if _SHA256_PATTERN.fullmatch(self.geometry_digest) is None:
            raise ValueError("geography boundary geometry digest is invalid")
        if not 4 <= self.position_count <= _MAX_POSITIONS:
            raise ValueError("geography boundary position count is invalid")
        observed = f"sha256:{sha256(self.canonical_geojson).hexdigest()}"
        if observed != self.geometry_digest:
            raise ValueError("geography boundary digest does not match canonical GeoJSON")


@dataclass(frozen=True, slots=True)
class GeographyPoint:
    point_key: str
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if _POINT_KEY_PATTERN.fullmatch(self.point_key) is None:
            raise ValueError("geography point key is invalid")
        if not math.isfinite(self.latitude) or not -90 <= self.latitude <= 90:
            raise ValueError("geography point latitude is outside the WGS84 range")
        if not math.isfinite(self.longitude) or not -180 <= self.longitude <= 180:
            raise ValueError("geography point longitude is outside the WGS84 range")


@dataclass(frozen=True, slots=True)
class GeographyCoverage:
    point_key: str
    boundary_digest: str
    coverage: GeographyCoverageKind

    def __post_init__(self) -> None:
        if _POINT_KEY_PATTERN.fullmatch(self.point_key) is None:
            raise ValueError("geography coverage point key is invalid")
        if _SHA256_PATTERN.fullmatch(self.boundary_digest) is None:
            raise ValueError("geography coverage boundary digest is invalid")


class GeographyCoveragePort(Protocol):
    def evaluate(
        self,
        boundary: GeographyBoundaryArtifact,
        points: Sequence[GeographyPoint],
    ) -> Sequence[GeographyCoverage]: ...


class GeographyCoverageService:
    def __init__(self, port: GeographyCoveragePort) -> None:
        self._port = port

    def evaluate(
        self,
        boundary: GeographyBoundaryArtifact,
        points: Sequence[GeographyPoint],
    ) -> tuple[GeographyCoverage, ...]:
        point_tuple = tuple(points)
        identities = tuple(point.point_key for point in point_tuple)
        if len(set(identities)) != len(identities):
            raise ValueError("geography point keys must be unique within a batch")
        if len(point_tuple) > 10_000:
            raise ValueError("geography coverage batch cannot exceed 10000 points")
        if not point_tuple:
            return ()
        results = tuple(self._port.evaluate(boundary, point_tuple))
        if len(results) != len(point_tuple):
            raise GeographyEvaluationError(
                code="GEOGRAPHY_COVERAGE_RESULT_INCOMPLETE",
                message="PostGIS did not return one coverage result per input point.",
            )
        expected_keys = identities
        actual_keys = tuple(result.point_key for result in results)
        if actual_keys != expected_keys:
            raise GeographyEvaluationError(
                code="GEOGRAPHY_COVERAGE_RESULT_ORDER_INVALID",
                message="PostGIS returned geography results in an invalid order.",
            )
        if any(result.boundary_digest != boundary.geometry_digest for result in results):
            raise GeographyEvaluationError(
                code="GEOGRAPHY_COVERAGE_BOUNDARY_MISMATCH",
                message="A geography result references another boundary digest.",
            )
        return results


def decode_boundary_geojson(body: bytes) -> GeographyBoundaryArtifact:
    if len(body) > _MAX_BOUNDARY_BYTES:
        raise _boundary_error(
            "GEOGRAPHY_BOUNDARY_TOO_LARGE",
            "The geography boundary exceeds the byte limit.",
            actualSizeBytes=len(body),
            maximumSizeBytes=_MAX_BOUNDARY_BYTES,
        )
    source_digest = f"sha256:{sha256(body).hexdigest()}"
    try:
        decoded = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _boundary_error(
            "GEOGRAPHY_BOUNDARY_ENCODING_INVALID",
            "The geography boundary is not valid UTF-8.",
            byteOffset=exc.start,
        ) from exc
    try:
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, GeographyBoundaryError):
            raise
        raise _boundary_error(
            "GEOGRAPHY_BOUNDARY_JSON_INVALID",
            "The geography boundary is not valid JSON.",
            causeType=type(exc).__name__,
        ) from exc
    root = _object(value, "boundary")
    if set(root) != {"type", "coordinates"}:
        raise _boundary_error(
            "GEOGRAPHY_BOUNDARY_FIELDS_INVALID",
            "The geography boundary fields do not match the geometry contract.",
            actualFields=sorted(root),
        )
    geometry_type = root["type"]
    if geometry_type not in {"Polygon", "MultiPolygon"}:
        raise _boundary_error(
            "GEOGRAPHY_BOUNDARY_TYPE_INVALID",
            "The geography boundary must be Polygon or MultiPolygon.",
            actualType=geometry_type,
        )
    coordinates = root["coordinates"]
    counter = [0]
    if geometry_type == "Polygon":
        canonical_coordinates = _polygon(coordinates, counter=counter, owner="coordinates")
    else:
        if not isinstance(coordinates, list) or not coordinates:
            raise _boundary_error(
                "GEOGRAPHY_BOUNDARY_COORDINATES_INVALID",
                "A MultiPolygon must contain at least one polygon.",
            )
        canonical_coordinates = [
            _polygon(item, counter=counter, owner=f"coordinates[{index}]")
            for index, item in enumerate(coordinates)
        ]
    canonical_document = {
        "coordinates": canonical_coordinates,
        "type": geometry_type,
    }
    canonical = json.dumps(
        canonical_document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return GeographyBoundaryArtifact(
        source_digest=source_digest,
        geometry_digest=f"sha256:{sha256(canonical).hexdigest()}",
        canonical_geojson=canonical,
        geometry_type=cast(Literal["Polygon", "MultiPolygon"], geometry_type),
        position_count=counter[0],
    )


def _polygon(
    value: object,
    *,
    counter: list[int],
    owner: str,
) -> list[list[list[float]]]:
    if not isinstance(value, list) or not value:
        raise _boundary_error(
            "GEOGRAPHY_BOUNDARY_COORDINATES_INVALID",
            f"{owner} must contain at least one linear ring.",
        )
    rings: list[list[list[float]]] = []
    for ring_index, ring_value in enumerate(value):
        if not isinstance(ring_value, list) or len(ring_value) < 4:
            raise _boundary_error(
                "GEOGRAPHY_BOUNDARY_RING_INVALID",
                "A geography boundary ring must contain at least four positions.",
                owner=owner,
                ringIndex=ring_index,
            )
        ring = [
            _position(
                position,
                counter=counter,
                owner=owner,
                ring_index=ring_index,
                position_index=position_index,
            )
            for position_index, position in enumerate(ring_value)
        ]
        if ring[0] != ring[-1]:
            raise _boundary_error(
                "GEOGRAPHY_BOUNDARY_RING_OPEN",
                "A geography boundary ring must be explicitly closed.",
                owner=owner,
                ringIndex=ring_index,
            )
        if len({tuple(position) for position in ring[:-1]}) < 3:
            raise _boundary_error(
                "GEOGRAPHY_BOUNDARY_RING_DEGENERATE",
                "A geography boundary ring requires three distinct vertices.",
                owner=owner,
                ringIndex=ring_index,
            )
        rings.append(ring)
    return rings


def _position(
    value: object,
    *,
    counter: list[int],
    owner: str,
    ring_index: int,
    position_index: int,
) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise _boundary_error(
            "GEOGRAPHY_BOUNDARY_POSITION_INVALID",
            "A geography boundary position must be [longitude, latitude].",
            owner=owner,
            ringIndex=ring_index,
            positionIndex=position_index,
        )
    longitude = _coordinate(value[0], minimum=-180, maximum=180, field="longitude")
    latitude = _coordinate(value[1], minimum=-90, maximum=90, field="latitude")
    counter[0] += 1
    if counter[0] > _MAX_POSITIONS:
        raise _boundary_error(
            "GEOGRAPHY_BOUNDARY_POSITION_LIMIT_EXCEEDED",
            "The geography boundary exceeds the position limit.",
            maximumPositionCount=_MAX_POSITIONS,
        )
    return [longitude, latitude]


def _coordinate(
    value: object,
    *,
    minimum: float,
    maximum: float,
    field: str,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _boundary_error(
            "GEOGRAPHY_BOUNDARY_COORDINATE_INVALID",
            "A geography boundary coordinate must be numeric.",
            field=field,
        )
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise _boundary_error(
            "GEOGRAPHY_BOUNDARY_COORDINATE_INVALID",
            "A geography boundary coordinate is outside the WGS84 range.",
            field=field,
        )
    return 0.0 if result == 0 else result


def _object(value: object, owner: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _boundary_error(
            "GEOGRAPHY_BOUNDARY_OBJECT_INVALID",
            f"The {owner} must be a JSON object.",
        )
    return cast(Mapping[str, object], value)


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _boundary_error(
                "GEOGRAPHY_BOUNDARY_DUPLICATE_KEY",
                "The geography boundary contains a duplicate object key.",
                key=key,
            )
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise _boundary_error(
        "GEOGRAPHY_BOUNDARY_NON_FINITE_NUMBER",
        "The geography boundary contains a non-finite number.",
        value=value,
    )


def _boundary_error(
    code: str,
    message: str,
    **context: object,
) -> GeographyBoundaryError:
    return GeographyBoundaryError(code=code, message=message, context=context)
