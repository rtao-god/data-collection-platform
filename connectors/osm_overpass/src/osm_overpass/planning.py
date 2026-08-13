from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from osm_overpass.contracts import (
    OsmElementType,
    OsmTagFilter,
    OverpassPolygon,
    OverpassQuerySpec,
)
from osm_overpass.query import query_digest

_QUERY_SPEC_REVISION = "osm-overpass-query/1"
_PLAN_REVISION = "osm-overpass-query-plan/1"


@dataclass(frozen=True, slots=True)
class OverpassPlannedQuery:
    position: int
    spec: OverpassQuerySpec
    query_spec_bytes: bytes
    query_spec_digest: str
    overpass_query_digest: str

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("planned Overpass query position cannot be negative")
        observed = f"sha256:{sha256(self.query_spec_bytes).hexdigest()}"
        if observed != self.query_spec_digest:
            raise ValueError("planned query-spec digest does not match its bytes")
        if not self.overpass_query_digest.startswith("sha256:"):
            raise ValueError("planned Overpass query digest is invalid")


@dataclass(frozen=True, slots=True)
class OverpassQueryPlan:
    queries: tuple[OverpassPlannedQuery, ...]

    def __post_init__(self) -> None:
        if not self.queries:
            raise ValueError("Overpass query plan cannot be empty")
        positions = tuple(query.position for query in self.queries)
        if positions != tuple(range(len(self.queries))):
            raise ValueError("Overpass query plan positions must be contiguous")
        digests = tuple(query.query_spec_digest for query in self.queries)
        if len(set(digests)) != len(digests):
            raise ValueError("Overpass query plan contains duplicate query specifications")

    def to_bytes(self) -> bytes:
        document = {
            "queries": [
                {
                    "overpassQueryDigest": query.overpass_query_digest,
                    "position": query.position,
                    "querySpec": json.loads(query.query_spec_bytes),
                    "querySpecDigest": query.query_spec_digest,
                }
                for query in self.queries
            ],
            "schemaRevision": _PLAN_REVISION,
        }
        return json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return f"sha256:{sha256(self.to_bytes()).hexdigest()}"


def plan_overpass_queries(
    *,
    polygon: OverpassPolygon,
    element_types: tuple[OsmElementType, ...],
    tag_filters: tuple[OsmTagFilter, ...],
    timeout_seconds: int = 60,
    maximum_elements: int = 25_000,
    filters_per_query: int = 10,
) -> OverpassQueryPlan:
    if not 1 <= filters_per_query <= 20:
        raise ValueError("filters_per_query must be between 1 and 20")
    if not tag_filters:
        raise ValueError("Overpass query planning requires tag filters")
    planned: list[OverpassPlannedQuery] = []
    for position, offset in enumerate(range(0, len(tag_filters), filters_per_query)):
        filters = tag_filters[offset : offset + filters_per_query]
        spec = OverpassQuerySpec(
            polygon=polygon,
            element_types=element_types,
            tag_filters=filters,
            timeout_seconds=timeout_seconds,
            maximum_elements=maximum_elements,
        )
        query_spec_bytes = _query_spec_bytes(spec)
        planned.append(
            OverpassPlannedQuery(
                position=position,
                spec=spec,
                query_spec_bytes=query_spec_bytes,
                query_spec_digest=f"sha256:{sha256(query_spec_bytes).hexdigest()}",
                overpass_query_digest=query_digest(spec),
            )
        )
    if len(planned) > 100:
        raise ValueError("Overpass query plan cannot exceed 100 work items")
    return OverpassQueryPlan(queries=tuple(planned))


def _query_spec_bytes(spec: OverpassQuerySpec) -> bytes:
    document = {
        "elementTypes": list(spec.element_types),
        "maximumElements": spec.maximum_elements,
        "polygon": [
            [point.latitude, point.longitude]
            for point in spec.polygon.points
        ],
        "schemaRevision": _QUERY_SPEC_REVISION,
        "tagFilters": [
            {
                "key": tag_filter.key,
                "values": list(tag_filter.values),
            }
            for tag_filter in spec.tag_filters
        ],
        "timeoutSeconds": spec.timeout_seconds,
    }
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
