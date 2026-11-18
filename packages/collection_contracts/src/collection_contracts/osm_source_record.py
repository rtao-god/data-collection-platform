"""Per-element OSM source records derived from a validated observation envelope.

`SOURCE_RECORD_MAPPING` consumes exactly one source record per work unit. The OSM
connector publishes one artifact per query, so a fan-out step splits the envelope
into immutable per-element records. Those records keep the original raw response
and query digests: the mapping stage never re-derives provenance from scratch.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_OSM_ATTRIBUTION = "© OpenStreetMap contributors"
_ELEMENT_TYPES = ("node", "way", "relation")
_ENVELOPE_FIELDS = frozenset(
    {
        "attribution",
        "generator",
        "observations",
        "osmBaseTimestampUtc",
        "queryDigest",
        "responseDigest",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "address",
        "elementType",
        "latitude",
        "longitude",
        "osmId",
        "sourceUrl",
        "tags",
    }
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _require_digest(name: str, value: object) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical sha256 digest")
    return value


class _OsmModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OsmElementIdentity(_OsmModel):
    """Stable OpenStreetMap element identity."""

    element_type: Literal["node", "way", "relation"]
    osm_id: int

    @field_validator("osm_id")
    @classmethod
    def validate_osm_id(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("OSM element id must be a positive integer")
        return value

    @property
    def source_url(self) -> str:
        return f"https://www.openstreetmap.org/{self.element_type}/{self.osm_id}"

    @property
    def value(self) -> str:
        return f"{self.element_type}/{self.osm_id}"

    @property
    def source_record_id(self) -> str:
        """Token-safe identity: `/` is not allowed in contract tokens."""

        return f"osm:{self.element_type}:{self.osm_id}"


class OsmSourceRecord(_OsmModel):
    """One OSM element taken verbatim from a validated observation envelope."""

    contract: Literal["osm-source-record@1"] = "osm-source-record@1"
    element: OsmElementIdentity
    raw_response_digest: str
    query_digest: str
    envelope_digest: str
    osm_base_timestamp_utc: str | None
    attribution: str
    tags: dict[str, str]
    address: dict[str, str]
    latitude: float
    longitude: float
    # The element's own OSM modification time, when the acquisition preserved it. It is
    # provenance about the object, never a substitute for the acquisition time.
    element_timestamp_utc: str | None = None
    record_digest: str

    @field_validator("raw_response_digest", "query_digest", "envelope_digest", "record_digest")
    @classmethod
    def validate_digest(cls, value: object) -> str:
        return _require_digest("OSM source record digest", value)

    @field_validator("attribution")
    @classmethod
    def validate_attribution(cls, value: str) -> str:
        if value != _OSM_ATTRIBUTION:
            raise ValueError("OSM source record must keep its canonical attribution")
        return value

    @field_validator("tags", "address")
    @classmethod
    def validate_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ValueError("OSM source record tags and address must be string maps")
        return value

    @field_validator("latitude")
    @classmethod
    def validate_latitude(cls, value: float) -> float:
        if not math.isfinite(value) or not -90.0 <= value <= 90.0:
            raise ValueError("OSM source record latitude is out of range")
        return value

    @field_validator("longitude")
    @classmethod
    def validate_longitude(cls, value: float) -> float:
        if not math.isfinite(value) or not -180.0 <= value <= 180.0:
            raise ValueError("OSM source record longitude is out of range")
        return value

    @property
    def source_record_id(self) -> str:
        return self.element.source_record_id


@dataclass(frozen=True, slots=True)
class OsmEnvelope:
    """Validated, immutable view of an `osm_observations` artifact."""

    query_digest: str
    response_digest: str
    osm_base_timestamp_utc: str | None
    attribution: str
    envelope_digest: str
    elements: tuple[OsmSourceRecord, ...]

    @property
    def identities(self) -> tuple[tuple[str, int], ...]:
        return tuple((item.element.element_type, item.element.osm_id) for item in self.elements)


def _osm_source_record(
    entry: object,
    *,
    index: int,
    query_digest: str,
    response_digest: str,
    base_timestamp: str | None,
    attribution: str,
    envelope_digest: str,
    element_timestamp: str | None = None,
) -> OsmSourceRecord:
    if not isinstance(entry, dict):
        raise ValueError(f"OSM observation at position {index} must be a JSON object")
    if set(entry) != _OBSERVATION_FIELDS:
        raise ValueError(f"OSM observation at position {index} has an unexpected shape")
    element_type = entry["elementType"]
    if element_type not in _ELEMENT_TYPES:
        raise ValueError(f"OSM observation at position {index} has an unsupported element type")
    osm_id = entry["osmId"]
    if not isinstance(osm_id, int) or isinstance(osm_id, bool) or osm_id <= 0:
        raise ValueError(f"OSM observation at position {index} has an invalid element id")
    element = OsmElementIdentity(element_type=element_type, osm_id=osm_id)
    if entry["sourceUrl"] != element.source_url:
        raise ValueError(
            f"OSM observation at position {index} source URL does not match its identity"
        )
    tags = entry["tags"]
    address = entry["address"]
    for label, value in (("tags", tags), ("address", address)):
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in value.items()
        ):
            raise ValueError(f"OSM observation at position {index} {label} must be a string map")
    latitude = entry["latitude"]
    longitude = entry["longitude"]
    for label, value, bound in (
        ("latitude", latitude, 90.0),
        ("longitude", longitude, 180.0),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"OSM observation at position {index} {label} is not numeric")
        if not math.isfinite(float(value)) or abs(float(value)) > bound:
            raise ValueError(f"OSM observation at position {index} {label} is out of range")
    unsealed = OsmSourceRecord(
        element=element,
        raw_response_digest=response_digest,
        query_digest=query_digest,
        envelope_digest=envelope_digest,
        osm_base_timestamp_utc=base_timestamp,
        attribution=attribution,
        tags=dict(tags),
        address=dict(address),
        latitude=float(latitude),
        longitude=float(longitude),
        element_timestamp_utc=element_timestamp,
        record_digest="sha256:" + "0" * 64,
    )
    return unsealed.model_copy(update={"record_digest": _osm_record_digest(unsealed)})


def _osm_record_digest(record: OsmSourceRecord) -> str:
    payload = record.model_dump(mode="json", exclude={"record_digest"})
    return _digest_bytes(_canonical_json(payload).encode("utf-8"))


def canonical_osm_source_record_json(record: OsmSourceRecord) -> str:
    """Canonical bytes of one per-element OSM source record."""

    return _canonical_json(record.model_dump(mode="json"))


def decode_osm_source_record(content: bytes) -> OsmSourceRecord:
    """Decode and verify one per-element OSM source record."""

    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("OSM source record is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("OSM source record must be a JSON object")
    record = OsmSourceRecord.model_validate(document)
    if record.record_digest != _osm_record_digest(record):
        raise ValueError("OSM source record digest does not match its content")
    if _canonical_json(document) != canonical_osm_source_record_json(record):
        raise ValueError("OSM source record is not canonical")
    return record


def decode_osm_envelope(content: bytes) -> OsmEnvelope:
    """Validate an OSM observation envelope and expose one record per element."""

    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("OSM observations artifact is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("OSM observations artifact must be a JSON object")
    if set(document) != _ENVELOPE_FIELDS:
        raise ValueError("OSM observations artifact has an unexpected document shape")
    query_digest = _require_digest("OSM observations queryDigest", document["queryDigest"])
    response_digest = _require_digest("OSM observations responseDigest", document["responseDigest"])
    attribution = document["attribution"]
    if attribution != _OSM_ATTRIBUTION:
        raise ValueError("OSM observations must keep its canonical ODbL attribution")
    base_timestamp = document["osmBaseTimestampUtc"]
    if base_timestamp is not None and not isinstance(base_timestamp, str):
        raise ValueError("OSM base timestamp must be a string or null")
    raw_observations = document["observations"]
    if not isinstance(raw_observations, list):
        raise ValueError("OSM observations must be a list")
    envelope_digest = _digest_bytes(content)
    records = tuple(
        _osm_source_record(
            entry,
            index=index,
            query_digest=query_digest,
            response_digest=response_digest,
            base_timestamp=base_timestamp,
            attribution=str(attribution),
            element_timestamp=entry.get("elementTimestampUtc"),
            envelope_digest=envelope_digest,
        )
        for index, entry in enumerate(raw_observations)
    )
    identities = tuple((item.element.element_type, item.element.osm_id) for item in records)
    if tuple(sorted(identities)) != identities:
        raise ValueError("OSM observations must be sorted by element identity")
    if len(set(identities)) != len(identities):
        raise ValueError("OSM observations contain duplicate element identities")
    return OsmEnvelope(
        query_digest=query_digest,
        response_digest=response_digest,
        osm_base_timestamp_utc=base_timestamp,
        attribution=str(attribution),
        envelope_digest=envelope_digest,
        elements=records,
    )
