from __future__ import annotations

import re
from hashlib import sha256

from osm_overpass.contracts import OverpassQuerySpec

_MAX_QUERY_BYTES = 256 * 1024


def build_overpass_query(spec: OverpassQuerySpec) -> str:
    polygon = " ".join(
        f"{_coordinate(point.latitude)} {_coordinate(point.longitude)}"
        for point in spec.polygon.points
    )
    selectors: list[str] = []
    for element_type in spec.element_types:
        for tag_filter in spec.tag_filters:
            escaped_values = "|".join(re.escape(value) for value in tag_filter.values)
            selector = (
                f'{element_type}["{tag_filter.key}"~"^(?:{escaped_values})$"](poly:"{polygon}");'
            )
            selectors.append(selector)
    query = (
        f"[out:json][timeout:{spec.timeout_seconds}];"
        "(" + "".join(selectors) + ");"
        "out body center qt;"
    )
    encoded = query.encode("utf-8")
    if len(encoded) > _MAX_QUERY_BYTES:
        raise ValueError("Overpass query exceeds the byte limit")
    return query


def query_digest(spec: OverpassQuerySpec) -> str:
    return f"sha256:{sha256(build_overpass_query(spec).encode('utf-8')).hexdigest()}"


def _coordinate(value: float) -> str:
    rendered = format(value, ".7f").rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", ""} else rendered
