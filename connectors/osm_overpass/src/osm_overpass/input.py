from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

from osm_overpass.contracts import (
    GeoPoint,
    OsmElementType,
    OsmTagFilter,
    OverpassPolygon,
    OverpassQuerySpec,
)
from osm_overpass.response import OverpassResponseError

_MAX_INPUT_BYTES = 4 * 1024 * 1024
_REQUIRED_KEYS = frozenset(
    {
        "schemaRevision",
        "polygon",
        "elementTypes",
        "tagFilters",
        "timeoutSeconds",
        "maximumElements",
    }
)


def decode_query_spec(body: bytes) -> OverpassQuerySpec:
    if len(body) > _MAX_INPUT_BYTES:
        raise _error(
            "OVERPASS_QUERY_SPEC_TOO_LARGE",
            "The Overpass query specification exceeds the byte limit.",
        )
    try:
        decoded = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _error(
            "OVERPASS_QUERY_SPEC_ENCODING_INVALID",
            "The Overpass query specification is not valid UTF-8.",
        ) from exc
    try:
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, OverpassResponseError):
            raise
        raise _error(
            "OVERPASS_QUERY_SPEC_JSON_INVALID",
            "The Overpass query specification is not valid JSON.",
        ) from exc
    root = _object(value, "query specification")
    if set(root) != _REQUIRED_KEYS:
        raise _error(
            "OVERPASS_QUERY_SPEC_FIELDS_INVALID",
            "The Overpass query specification fields do not match the contract.",
            actualFields=sorted(root),
            expectedFields=sorted(_REQUIRED_KEYS),
        )
    if root["schemaRevision"] != "osm-overpass-query/1":
        raise _error(
            "OVERPASS_QUERY_SPEC_REVISION_UNSUPPORTED",
            "The Overpass query specification revision is unsupported.",
        )
    points_value = root["polygon"]
    if not isinstance(points_value, list):
        raise _error(
            "OVERPASS_QUERY_SPEC_POLYGON_INVALID",
            "The Overpass polygon must be an array of coordinate pairs.",
        )
    points: list[GeoPoint] = []
    for index, point_value in enumerate(points_value):
        if not isinstance(point_value, list) or len(point_value) != 2:
            raise _error(
                "OVERPASS_QUERY_SPEC_POLYGON_INVALID",
                "Each Overpass polygon point must be a latitude/longitude pair.",
                index=index,
            )
        latitude = _number(point_value[0], "latitude", index=index)
        longitude = _number(point_value[1], "longitude", index=index)
        points.append(GeoPoint(latitude=latitude, longitude=longitude))

    element_types_value = root["elementTypes"]
    if not isinstance(element_types_value, list):
        raise _error(
            "OVERPASS_QUERY_SPEC_ELEMENT_TYPES_INVALID",
            "The OSM element types must be an array.",
        )
    element_types: list[OsmElementType] = []
    for value in element_types_value:
        if value not in {"node", "way", "relation"}:
            raise _error(
                "OVERPASS_QUERY_SPEC_ELEMENT_TYPES_INVALID",
                "The query specification contains an unsupported OSM element type.",
            )
        element_types.append(cast(OsmElementType, value))

    filters_value = root["tagFilters"]
    if not isinstance(filters_value, list):
        raise _error(
            "OVERPASS_QUERY_SPEC_TAG_FILTERS_INVALID",
            "The OSM tag filters must be an array.",
        )
    filters: list[OsmTagFilter] = []
    for index, filter_value in enumerate(filters_value):
        item = _object(filter_value, f"tagFilters[{index}]")
        if set(item) != {"key", "values"}:
            raise _error(
                "OVERPASS_QUERY_SPEC_TAG_FILTER_INVALID",
                "An OSM tag filter has invalid fields.",
                index=index,
            )
        key = item["key"]
        values = item["values"]
        if not isinstance(key, str) or not isinstance(values, list):
            raise _error(
                "OVERPASS_QUERY_SPEC_TAG_FILTER_INVALID",
                "An OSM tag filter has an invalid key or values array.",
                index=index,
            )
        if not all(isinstance(value, str) for value in values):
            raise _error(
                "OVERPASS_QUERY_SPEC_TAG_FILTER_INVALID",
                "OSM tag filter values must be strings.",
                index=index,
            )
        filters.append(OsmTagFilter(key=key, values=tuple(cast(list[str], values))))

    return OverpassQuerySpec(
        polygon=OverpassPolygon(points=tuple(points)),
        element_types=tuple(element_types),
        tag_filters=tuple(filters),
        timeout_seconds=_integer(root["timeoutSeconds"], "timeoutSeconds"),
        maximum_elements=_integer(root["maximumElements"], "maximumElements"),
    )


def _object(value: object, owner: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(
            "OVERPASS_QUERY_SPEC_OBJECT_INVALID",
            f"The {owner} must be a JSON object.",
        )
    return cast(Mapping[str, object], value)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(
            "OVERPASS_QUERY_SPEC_INTEGER_INVALID",
            "The query specification integer field is invalid.",
            field=field,
        )
    return value


def _number(value: object, field: str, *, index: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(
            "OVERPASS_QUERY_SPEC_COORDINATE_INVALID",
            "The query specification coordinate is invalid.",
            field=field,
            index=index,
        )
    result = float(value)
    if not result == result or result in {float("inf"), float("-inf")}:
        raise _error(
            "OVERPASS_QUERY_SPEC_COORDINATE_INVALID",
            "The query specification coordinate must be finite.",
            field=field,
            index=index,
        )
    return result


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _error(
                "OVERPASS_QUERY_SPEC_DUPLICATE_KEY",
                "The query specification contains a duplicate object key.",
                key=key,
            )
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise _error(
        "OVERPASS_QUERY_SPEC_NON_FINITE_NUMBER",
        "The query specification contains a non-finite number.",
        value=value,
    )


def _error(code: str, message: str, **context: object) -> OverpassResponseError:
    return OverpassResponseError(code=code, message=message, context=context)
