from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType
from typing import cast

from osm_overpass.contracts import (
    GeoPoint,
    OsmAddress,
    OsmElementObservation,
    OsmElementType,
    OsmObservationBatch,
    OverpassQuerySpec,
)
from osm_overpass.query import query_digest

_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_ROOT_KEYS = frozenset({"version", "generator", "osm3s", "elements", "remark"})
_ELEMENT_KEYS = frozenset(
    {
        "type",
        "id",
        "lat",
        "lon",
        "center",
        "tags",
        "timestamp",
        "version",
        "changeset",
        "user",
        "uid",
        "nodes",
        "members",
        "bounds",
        "geometry",
    }
)


class OverpassResponseError(ValueError):
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


def parse_overpass_response(
    body: bytes,
    *,
    spec: OverpassQuerySpec,
) -> OsmObservationBatch:
    if len(body) > _MAX_RESPONSE_BYTES:
        raise _error(
            "OVERPASS_RESPONSE_TOO_LARGE",
            "The Overpass response exceeds the byte limit.",
            actualSizeBytes=len(body),
            maximumSizeBytes=_MAX_RESPONSE_BYTES,
        )
    response_digest = f"sha256:{sha256(body).hexdigest()}"
    try:
        decoded = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _error(
            "OVERPASS_RESPONSE_ENCODING_INVALID",
            "The Overpass response is not valid UTF-8.",
            byteOffset=exc.start,
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
            "OVERPASS_RESPONSE_JSON_INVALID",
            "The Overpass response is not valid JSON.",
            causeType=type(exc).__name__,
        ) from exc
    root = _object(value, "response")
    unknown_root = set(root) - _ROOT_KEYS
    if unknown_root:
        raise _error(
            "OVERPASS_RESPONSE_ROOT_FIELD_UNKNOWN",
            "The Overpass response contains unknown root fields.",
            fields=sorted(unknown_root),
        )
    elements = root.get("elements")
    if not isinstance(elements, list):
        raise _error(
            "OVERPASS_RESPONSE_ELEMENTS_INVALID",
            "The Overpass response elements field must be an array.",
        )
    if len(elements) > spec.maximum_elements:
        raise _error(
            "OVERPASS_RESPONSE_ELEMENT_LIMIT_EXCEEDED",
            "The Overpass response exceeds the configured element limit.",
            actualElementCount=len(elements),
            maximumElementCount=spec.maximum_elements,
        )
    observations = tuple(
        sorted(
            (_decode_element(item, index=index) for index, item in enumerate(elements)),
            key=lambda item: (item.element_type, item.osm_id),
        )
    )
    timestamp = _base_timestamp(root.get("osm3s"))
    generator = root.get("generator")
    if generator is not None and not isinstance(generator, str):
        raise _error(
            "OVERPASS_RESPONSE_GENERATOR_INVALID",
            "The Overpass generator field must be a string.",
        )
    return OsmObservationBatch(
        query_digest=query_digest(spec),
        response_digest=response_digest,
        generator=generator,
        osm_base_timestamp_utc=timestamp,
        observations=observations,
    )


def _decode_element(value: object, *, index: int) -> OsmElementObservation:
    element = _object(value, f"element[{index}]")
    unknown = set(element) - _ELEMENT_KEYS
    if unknown:
        raise _error(
            "OVERPASS_ELEMENT_FIELD_UNKNOWN",
            "An Overpass element contains unknown fields.",
            index=index,
            fields=sorted(unknown),
        )
    element_type = element.get("type")
    if element_type not in {"node", "way", "relation"}:
        raise _error(
            "OVERPASS_ELEMENT_TYPE_INVALID",
            "An Overpass element has an unsupported type.",
            index=index,
            actualType=element_type,
        )
    osm_id = element.get("id")
    if isinstance(osm_id, bool) or not isinstance(osm_id, int) or osm_id <= 0:
        raise _error(
            "OVERPASS_ELEMENT_ID_INVALID",
            "An Overpass element ID must be a positive integer.",
            index=index,
        )
    latitude: object
    longitude: object
    if element_type == "node":
        latitude = element.get("lat")
        longitude = element.get("lon")
    else:
        center = _object(element.get("center"), f"element[{index}].center")
        latitude = center.get("lat")
        longitude = center.get("lon")
    point = GeoPoint(
        latitude=_finite_coordinate(latitude, "latitude", index=index),
        longitude=_finite_coordinate(longitude, "longitude", index=index),
    )
    tags_value = element.get("tags", {})
    tags_object = _object(tags_value, f"element[{index}].tags")
    tags: dict[str, str] = {}
    for key, tag_value in tags_object.items():
        if not isinstance(tag_value, str):
            raise _error(
                "OVERPASS_ELEMENT_TAG_INVALID",
                "OSM tag values must be strings.",
                index=index,
                tag=key,
            )
        tags[key] = tag_value
    frozen_tags = MappingProxyType(dict(sorted(tags.items())))
    return OsmElementObservation(
        element_type=cast(OsmElementType, element_type),
        osm_id=osm_id,
        location=point,
        tags=frozen_tags,
        address=OsmAddress(
            house_number=tags.get("addr:housenumber"),
            street=tags.get("addr:street"),
            postcode=tags.get("addr:postcode"),
            city=tags.get("addr:city"),
            country=tags.get("addr:country"),
        ),
        source_url=f"https://www.openstreetmap.org/{element_type}/{osm_id}",
    )


def _base_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    osm3s = _object(value, "osm3s")
    timestamp = osm3s.get("timestamp_osm_base")
    if timestamp is None:
        return None
    if not isinstance(timestamp, str):
        raise _error(
            "OVERPASS_RESPONSE_TIMESTAMP_INVALID",
            "The OSM base timestamp must be a string.",
        )
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error(
            "OVERPASS_RESPONSE_TIMESTAMP_INVALID",
            "The OSM base timestamp is invalid.",
            actualValue=timestamp,
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _error(
            "OVERPASS_RESPONSE_TIMESTAMP_INVALID",
            "The OSM base timestamp must include a timezone.",
            actualValue=timestamp,
        )
    return parsed.astimezone(UTC)


def _object(value: object, owner: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(
            "OVERPASS_RESPONSE_OBJECT_INVALID",
            f"{owner} must be a JSON object.",
        )
    return cast(Mapping[str, object], value)


def _finite_coordinate(value: object, field: str, *, index: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(
            "OVERPASS_ELEMENT_COORDINATE_INVALID",
            "An Overpass element coordinate must be numeric.",
            index=index,
            field=field,
        )
    result = float(value)
    if not result == result or result in {float("inf"), float("-inf")}:
        raise _error(
            "OVERPASS_ELEMENT_COORDINATE_INVALID",
            "An Overpass element coordinate must be finite.",
            index=index,
            field=field,
        )
    return result


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _error(
                "OVERPASS_RESPONSE_DUPLICATE_KEY",
                "The Overpass response contains a duplicate object key.",
                key=key,
            )
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise _error(
        "OVERPASS_RESPONSE_NON_FINITE_NUMBER",
        "The Overpass response contains a non-finite number.",
        value=value,
    )


def _error(code: str, message: str, **context: object) -> OverpassResponseError:
    return OverpassResponseError(code=code, message=message, context=context)
