from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import MappingProxyType
from typing import Literal

OsmElementType = Literal["node", "way", "relation"]

_TAG_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,254}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_TAGS = 512


@dataclass(frozen=True, slots=True)
class GeoPoint:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90 <= self.latitude <= 90:
            raise ValueError("latitude is outside the WGS84 range")
        if not -180 <= self.longitude <= 180:
            raise ValueError("longitude is outside the WGS84 range")


@dataclass(frozen=True, slots=True)
class OverpassPolygon:
    points: tuple[GeoPoint, ...]

    def __post_init__(self) -> None:
        if not 3 <= len(self.points) <= 5_000:
            raise ValueError("Overpass polygon must contain between 3 and 5000 points")
        if len({(point.latitude, point.longitude) for point in self.points}) < 3:
            raise ValueError("Overpass polygon must contain three distinct points")


@dataclass(frozen=True, slots=True)
class OsmTagFilter:
    key: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if _TAG_KEY.fullmatch(self.key) is None:
            raise ValueError("OSM tag key is invalid")
        if not 1 <= len(self.values) <= 100:
            raise ValueError("OSM tag filter must contain between 1 and 100 values")
        normalized = tuple(sorted(set(self.values)))
        if normalized != self.values:
            raise ValueError("OSM tag values must be unique and sorted")
        for value in self.values:
            if not value or len(value) > 255:
                raise ValueError("OSM tag value is invalid")
            if any(character in value for character in ("\x00", "\r", "\n")):
                raise ValueError("OSM tag value contains a forbidden character")


@dataclass(frozen=True, slots=True)
class OverpassQuerySpec:
    polygon: OverpassPolygon
    element_types: tuple[OsmElementType, ...]
    tag_filters: tuple[OsmTagFilter, ...]
    timeout_seconds: int = 60
    maximum_elements: int = 25_000

    def __post_init__(self) -> None:
        normalized_types = tuple(sorted(set(self.element_types)))
        if not normalized_types or normalized_types != self.element_types:
            raise ValueError("OSM element types must be unique, sorted, and non-empty")
        if not 1 <= len(self.tag_filters) <= 100:
            raise ValueError("Overpass query requires between 1 and 100 tag filters")
        filter_identities = tuple((item.key, item.values) for item in self.tag_filters)
        if tuple(sorted(set(filter_identities))) != filter_identities:
            raise ValueError("OSM tag filters must be unique and sorted")
        if not 1 <= self.timeout_seconds <= 180:
            raise ValueError("Overpass timeout must be between 1 and 180 seconds")
        if not 1 <= self.maximum_elements <= 100_000:
            raise ValueError("Overpass element limit must be between 1 and 100000")


@dataclass(frozen=True, slots=True)
class OsmAddress:
    house_number: str | None
    street: str | None
    postcode: str | None
    city: str | None
    country: str | None


@dataclass(frozen=True, slots=True)
class OsmElementObservation:
    element_type: OsmElementType
    osm_id: int
    location: GeoPoint
    tags: MappingProxyType[str, str]
    address: OsmAddress
    source_url: str

    def __post_init__(self) -> None:
        if self.osm_id <= 0:
            raise ValueError("OSM element ID must be positive")
        if not 0 <= len(self.tags) <= _MAX_TAGS:
            raise ValueError("OSM element has too many tags")
        for key, value in self.tags.items():
            if _TAG_KEY.fullmatch(key) is None:
                raise ValueError("OSM response contains an invalid tag key")
            if len(value) > 4_096 or "\x00" in value:
                raise ValueError("OSM response contains an invalid tag value")
        expected_url = f"https://www.openstreetmap.org/{self.element_type}/{self.osm_id}"
        if self.source_url != expected_url:
            raise ValueError("OSM source URL does not match the element identity")


@dataclass(frozen=True, slots=True)
class OsmObservationBatch:
    query_digest: str
    response_digest: str
    generator: str | None
    osm_base_timestamp_utc: datetime | None
    observations: tuple[OsmElementObservation, ...]
    attribution: str = "© OpenStreetMap contributors"

    def __post_init__(self) -> None:
        for value in (self.query_digest, self.response_digest):
            if _SHA256.fullmatch(value) is None:
                raise ValueError("OSM observation digest is invalid")
        if self.osm_base_timestamp_utc is not None:
            if (
                self.osm_base_timestamp_utc.tzinfo is None
                or self.osm_base_timestamp_utc.utcoffset() != timedelta(0)
            ):
                raise ValueError("OSM base timestamp must be UTC")
        identities = tuple(
            (observation.element_type, observation.osm_id) for observation in self.observations
        )
        if len(set(identities)) != len(identities):
            raise ValueError("OSM observation batch contains duplicate element identities")
        if tuple(sorted(identities)) != identities:
            raise ValueError("OSM observations must be sorted by identity")
        if self.attribution != "© OpenStreetMap contributors":
            raise ValueError("OSM attribution must remain canonical")

    def to_bytes(self) -> bytes:
        document = {
            "attribution": self.attribution,
            "generator": self.generator,
            "observations": [
                {
                    "address": {
                        "city": item.address.city,
                        "country": item.address.country,
                        "houseNumber": item.address.house_number,
                        "postcode": item.address.postcode,
                        "street": item.address.street,
                    },
                    "elementType": item.element_type,
                    "latitude": item.location.latitude,
                    "longitude": item.location.longitude,
                    "osmId": item.osm_id,
                    "sourceUrl": item.source_url,
                    "tags": dict(sorted(item.tags.items())),
                }
                for item in self.observations
            ],
            "osmBaseTimestampUtc": (
                self.osm_base_timestamp_utc.astimezone(UTC).isoformat().replace("+00:00", "Z")
                if self.osm_base_timestamp_utc is not None
                else None
            ),
            "queryDigest": self.query_digest,
            "responseDigest": self.response_digest,
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
