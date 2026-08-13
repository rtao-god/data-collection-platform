from __future__ import annotations

import json

from osm_overpass.planning import plan_overpass_queries

from osm_overpass import (
    GeoPoint,
    OsmTagFilter,
    OverpassPolygon,
    decode_query_spec,
)


def polygon() -> OverpassPolygon:
    return OverpassPolygon(
        points=(
            GeoPoint(52.5, 13.3),
            GeoPoint(52.6, 13.3),
            GeoPoint(52.55, 13.5),
        )
    )


def filters() -> tuple[OsmTagFilter, ...]:
    return tuple(
        OsmTagFilter(key=f"tag_{index:02d}", values=(f"value_{index:02d}",)) for index in range(5)
    )


def test_plan_is_deterministic_and_each_artifact_round_trips() -> None:
    first = plan_overpass_queries(
        polygon=polygon(),
        element_types=("node", "relation", "way"),
        tag_filters=filters(),
        filters_per_query=2,
    )
    second = plan_overpass_queries(
        polygon=polygon(),
        element_types=("node", "relation", "way"),
        tag_filters=filters(),
        filters_per_query=2,
    )
    assert first.to_bytes() == second.to_bytes()
    assert first.digest == second.digest
    assert len(first.queries) == 3
    assert [len(query.spec.tag_filters) for query in first.queries] == [2, 2, 1]
    for query in first.queries:
        assert decode_query_spec(query.query_spec_bytes) == query.spec


def test_plan_assigns_every_filter_exactly_once_in_sorted_order() -> None:
    plan = plan_overpass_queries(
        polygon=polygon(),
        element_types=("node",),
        tag_filters=filters(),
        filters_per_query=2,
    )
    observed = tuple(tag_filter for query in plan.queries for tag_filter in query.spec.tag_filters)
    assert observed == filters()


def test_query_artifact_contains_no_endpoint_or_campaign_domain() -> None:
    plan = plan_overpass_queries(
        polygon=polygon(),
        element_types=("node",),
        tag_filters=(OsmTagFilter(key="amenity", values=("studio",)),),
    )
    document = json.loads(plan.queries[0].query_spec_bytes)
    assert set(document) == {
        "elementTypes",
        "maximumElements",
        "polygon",
        "schemaRevision",
        "tagFilters",
        "timeoutSeconds",
    }
    assert "endpoint" not in document
    assert "Berlin" not in plan.queries[0].query_spec_bytes.decode()
    assert "recording" not in plan.queries[0].query_spec_bytes.decode()


def test_boundary_change_changes_plan_identity() -> None:
    first = plan_overpass_queries(
        polygon=polygon(),
        element_types=("node",),
        tag_filters=(OsmTagFilter(key="amenity", values=("studio",)),),
    )
    changed = plan_overpass_queries(
        polygon=OverpassPolygon(
            points=(
                GeoPoint(52.5, 13.3),
                GeoPoint(52.61, 13.3),
                GeoPoint(52.55, 13.5),
            )
        ),
        element_types=("node",),
        tag_filters=(OsmTagFilter(key="amenity", values=("studio",)),),
    )
    assert first.digest != changed.digest
    assert first.queries[0].overpass_query_digest != changed.queries[0].overpass_query_digest
