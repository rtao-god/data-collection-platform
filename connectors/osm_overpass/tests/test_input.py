from __future__ import annotations

import json

import pytest

from osm_overpass.input import decode_query_spec
from osm_overpass.response import OverpassResponseError


def document() -> dict[str, object]:
    return {
        "schemaRevision": "osm-overpass-query/1",
        "polygon": [[52.5, 13.3], [52.6, 13.3], [52.55, 13.5]],
        "elementTypes": ["node", "relation", "way"],
        "tagFilters": [
            {"key": "amenity", "values": ["music_venue"]},
            {"key": "studio", "values": ["recording"]},
        ],
        "timeoutSeconds": 60,
        "maximumElements": 25000,
    }


def test_query_spec_is_decoded_without_domain_defaults() -> None:
    spec = decode_query_spec(json.dumps(document()).encode())
    assert spec.polygon.points[0].latitude == 52.5
    assert spec.element_types == ("node", "relation", "way")
    assert spec.tag_filters[1].values == ("recording",)


def test_query_spec_rejects_unknown_fields() -> None:
    value = document()
    value["city"] = "Berlin"
    with pytest.raises(OverpassResponseError) as error:
        decode_query_spec(json.dumps(value).encode())
    assert error.value.code == "OVERPASS_QUERY_SPEC_FIELDS_INVALID"


def test_query_spec_rejects_duplicate_keys() -> None:
    body = b'{"schemaRevision":"osm-overpass-query/1","schemaRevision":"other"}'
    with pytest.raises(OverpassResponseError) as error:
        decode_query_spec(body)
    assert error.value.code == "OVERPASS_QUERY_SPEC_DUPLICATE_KEY"
