"""Classify a candidate document against the campaign's exact boundary artifact.

The classifier is a pure function over already-verified inputs: the run's canonical
campaign bundle and the candidate's own typed observations. It never queries PostGIS,
never geocodes, and never invents a point. A candidate without an observed coordinate
keeps `UNKNOWN` coverage bound to its exact declared boundary, so a legitimately
remote-only entity is neither forced to carry coordinates nor silently treated as
located inside Berlin.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from collection_application.geography import decode_boundary_geojson
from collection_contracts.campaign_config import GeographyDocument
from collection_contracts.osm_query_spec import boundary_contains

# The campaign field key that carries a source-native point. Additional source-native
# keys are admitted only through the explicit alias map below, never by name guessing.
COORDINATE_FIELD_KEY = "coordinates"
COORDINATE_FIELD_ALIASES: Mapping[str, tuple[str, ...]] = {
    "coordinates": ("coordinates", "location", "geo_point"),
}

# The exact coordinate value contract: a GeoJSON-style point object with decimal text.
POINT_DOCUMENT_FIELDS = frozenset({"latitude", "longitude"})

type CoverageKind = Literal["inside", "boundary", "outside", "unknown"]


class CandidateGeographyError(RuntimeError):
    """Raised when the run's exact geography provenance cannot classify a candidate."""

    def __init__(self, *, code: str, context: Mapping[str, object]) -> None:
        super().__init__(code)
        self.code = code
        self.context = dict(context)


@dataclass(frozen=True, slots=True)
class CandidateGeographyBoundary:
    """The exact boundary the run was collected against."""

    market_area_revision: str
    market_area_digest: str
    document: object


@dataclass(frozen=True, slots=True)
class CandidateGeographyPoint:
    latitude: Decimal
    longitude: Decimal
    observation_id: str


@dataclass(frozen=True, slots=True)
class CandidateGeographyDecision:
    """A decided coverage plus the exact observation that produced the point, if any."""

    coverage: CoverageKind
    boundary: CandidateGeographyBoundary
    point: CandidateGeographyPoint | None


def read_candidate_geography_boundary(
    bundle: Mapping[str, object],
) -> CandidateGeographyBoundary | None:
    """Return the declared boundary of the run, or `None` when the campaign has none.

    A campaign without `geography.yaml` performs no areal classification at all; the
    caller must keep the candidate unresolved instead of inventing a market area.
    """

    components = _bundle_components(bundle)
    geography_payload = None
    for component in components:
        if component["path"] == "geography.yaml":
            geography_payload = component["content"]
            break
    if geography_payload is None:
        return None
    try:
        geography = GeographyDocument.model_validate(geography_payload)
    except (TypeError, ValueError) as exc:
        raise CandidateGeographyError(
            code="CANDIDATE_GEOGRAPHY_DECLARATION_INVALID",
            context={"causeType": type(exc).__name__},
        ) from exc
    boundary_payload = None
    for component in components:
        if component["path"] == geography.boundary_artifact_path:
            boundary_payload = component["content"]
            break
    if boundary_payload is None:
        raise CandidateGeographyError(
            code="CANDIDATE_GEOGRAPHY_BOUNDARY_MISSING",
            context={"boundaryPath": geography.boundary_artifact_path},
        )
    boundary_bytes = _canonical_component_bytes(boundary_payload, geography.boundary_artifact_path)
    if _digest_bytes(boundary_bytes) != geography.boundary_digest:
        raise CandidateGeographyError(
            code="CANDIDATE_GEOGRAPHY_BOUNDARY_DIGEST_CONFLICT",
            context={
                "boundaryPath": geography.boundary_artifact_path,
                "declaredDigest": geography.boundary_digest,
            },
        )
    artifact = decode_boundary_geojson(boundary_bytes)
    return CandidateGeographyBoundary(
        market_area_revision=geography.geography_revision,
        market_area_digest=geography.boundary_digest,
        document=json.loads(artifact.canonical_geojson),
    )


def classify_candidate_geography(
    *,
    boundary: CandidateGeographyBoundary | None,
    observations: Sequence[tuple[str, str, str]],
) -> CandidateGeographyDecision:
    """Decide coverage from the candidate's observed point, never from a guess.

    `observations` carries `(field_key, observation_id, numeric_text)` for the candidate's
    observed fields. Exactly one distinct point may be present: two contradictory
    coordinates are a provenance conflict, not a vote.
    """

    if boundary is None:
        raise CandidateGeographyError(
            code="CANDIDATE_GEOGRAPHY_DECLARATION_MISSING",
            context={"reason": "campaign_declares_no_geography"},
        )
    points = _distinct_points(observations)
    if not points:
        return CandidateGeographyDecision(coverage="unknown", boundary=boundary, point=None)
    if len(points) > 1:
        raise CandidateGeographyError(
            code="CANDIDATE_GEOGRAPHY_POINT_CONFLICT",
            context={"pointCount": len(points)},
        )
    point = points[0]
    inside = boundary_contains(
        boundary.document,
        float(point.latitude),
        float(point.longitude),
    )
    return CandidateGeographyDecision(
        coverage="inside" if inside else "outside",
        boundary=boundary,
        point=point,
    )


def _distinct_points(
    observations: Sequence[tuple[str, str, str]],
) -> tuple[CandidateGeographyPoint, ...]:
    points: dict[tuple[Decimal, Decimal], CandidateGeographyPoint] = {}
    for field_key, observation_id, value in observations:
        if field_key not in COORDINATE_FIELD_ALIASES.get(COORDINATE_FIELD_KEY, ()):
            continue
        latitude, longitude = _parse_point(field_key, value)
        points.setdefault(
            (latitude, longitude),
            CandidateGeographyPoint(
                latitude=latitude,
                longitude=longitude,
                observation_id=observation_id,
            ),
        )
    return tuple(points[key] for key in sorted(points))


def _parse_point(field_key: str, value: str) -> tuple[Decimal, Decimal]:
    try:
        document = json.loads(value)
        if not isinstance(document, Mapping) or set(document) != POINT_DOCUMENT_FIELDS:
            raise TypeError("coordinate value must be an exact point object")
        latitude = Decimal(str(document["latitude"]))
        longitude = Decimal(str(document["longitude"]))
    except (TypeError, ValueError, KeyError, InvalidOperation) as exc:
        raise CandidateGeographyError(
            code="CANDIDATE_GEOGRAPHY_POINT_INVALID",
            context={"fieldKey": field_key},
        ) from exc
    if not (Decimal("-90") <= latitude <= Decimal("90")):
        raise CandidateGeographyError(
            code="CANDIDATE_GEOGRAPHY_POINT_OUT_OF_RANGE",
            context={"fieldKey": field_key, "axis": "latitude"},
        )
    if not (Decimal("-180") <= longitude <= Decimal("180")):
        raise CandidateGeographyError(
            code="CANDIDATE_GEOGRAPHY_POINT_OUT_OF_RANGE",
            context={"fieldKey": field_key, "axis": "longitude"},
        )
    return latitude, longitude


def _bundle_components(bundle: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    """Return the canonical component ledger of the run snapshot.

    The bundle is `{campaignKey, components: [{path, digest, content}]}`; components are
    matched by path because that shape is the owner's canonical document.
    """

    components = bundle.get("components")
    if not isinstance(components, list):
        raise CandidateGeographyError(
            code="CANDIDATE_GEOGRAPHY_SNAPSHOT_INVALID",
            context={"reason": "component_ledger_missing"},
        )
    result: list[Mapping[str, object]] = []
    for component in components:
        if not isinstance(component, Mapping) or set(component) != {
            "path",
            "digest",
            "content",
        }:
            raise CandidateGeographyError(
                code="CANDIDATE_GEOGRAPHY_SNAPSHOT_INVALID",
                context={"reason": "component_shape_invalid"},
            )
        result.append(component)
    return result


def _canonical_component_bytes(payload: object, path: str) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, Mapping | list):
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    raise CandidateGeographyError(
        code="CANDIDATE_GEOGRAPHY_BOUNDARY_UNREPRESENTABLE",
        context={"boundaryPath": path, "payloadType": type(payload).__name__},
    )


def _digest_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


__all__ = [
    "COORDINATE_FIELD_ALIASES",
    "COORDINATE_FIELD_KEY",
    "POINT_DOCUMENT_FIELDS",
    "CandidateGeographyBoundary",
    "CandidateGeographyDecision",
    "CandidateGeographyError",
    "CandidateGeographyPoint",
    "classify_candidate_geography",
    "read_candidate_geography_boundary",
]
