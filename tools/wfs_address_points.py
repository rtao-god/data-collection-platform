"""Official Berlin address points as publishable coordinates.

The registry evidence proves *which* address a studio claims; the official address register
supplies the point. This module turns one complete search identity (street + house number +
suffix + postal code) into at most one coordinate with an explicit CRS origin, never into a
coordinate inferred from a text excerpt or picked from the first result of a postal code.

Properties enforced here:

* The search identity is complete. A response feature matches only when its street name,
  house number, house-number suffix and postal code all match the search. Two streets that
  share a postal code and a house number stay distinct.
* Exactly one feature may match. Zero matches, several matching records or a conflicting
  address record id are unresolved verdicts, never a first-result selection.
* The coordinate source is the official address dataset, addressed by its own request URL and
  sealed by the SHA-256 digest of the raw response bytes. ``adr_datum`` is the address record
  date, not a dataset release revision; the response snapshot identity is the digest.
* The request and the response declare EPSG:25833, and the conversion to EPSG:4326 is an
  explicit inverse UTM 33N with ``always_xy`` axis order. An unknown or mixed CRS is refused
  instead of guessed from coordinate magnitude.
* The converted point must lie inside the official Berlin boundary polygon. The bounding box
  alone admits points that are not Berlin addresses.
"""

from __future__ import annotations

# ruff: noqa: S310 - every url is built by request_url() from one fixed https endpoint.
import base64
import functools
import hashlib
import json
import math
import pathlib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

WFS_ENDPOINT = "https://gdi.berlin.de/services/wfs/adressen_berlin"
WFS_LAYER = "adressen_berlin"
DATASET_IDENTIFIER = WFS_LAYER
SOURCE_NAME = "official_berlin_address_register"

# The dataset is requested in the CRS it publishes and converted to WGS84 exactly once.
SOURCE_CRS = "EPSG:25833"
OUTPUT_CRS = "EPSG:4326"
SOURCE_EPSG = 25833

CACHE_SCHEMA = "wfs-address-point-cache-v2"

# The dataset publishes address points at metre level, so a claimed precision coarser than that
# would understate the evidence and a finer one would overstate it.
ADDRESS_POINT_ACCURACY_METERS = "1.0"

# Only the building-coordinate class is published at metre level; the coarser classes are
# recorded as unresolved instead of claiming a precision the dataset does not state.
ACCEPTED_QUALITY = "Qualitaet A"
ACCEPTED_TYPE = "Adresse"

_CAMPAIGN_GEOGRAPHY = (
    pathlib.Path(__file__).resolve().parent.parent
    / "campaigns"
    / "berlin_recording_services"
    / "geography"
)
BOUNDARY_PATH = _CAMPAIGN_GEOGRAPHY / "berlin-boundary.geojson"
BOUNDARY_PROVENANCE_PATH = _CAMPAIGN_GEOGRAPHY / "berlin-boundary.provenance.json"

# Unresolved verdicts. Each one names why the point cannot be claimed, so a reviewer acts on
# the cause instead of re-running the lookup.
REASON_ADDRESS_INCOMPLETE = "address_point_not_derivable_from_address"
REASON_RANGE_INCOMPLETE = "address_point_range_endpoint_unresolved"
REASON_RANGE_TOO_WIDE = "address_point_range_span_too_wide"

# A house-number range names several buildings, so the derived point is the middle of its
# endpoints and is published as an approximate location, never as a metre-level address point.
# En dash is what official sites and the register print for a range ("34–36"); treat it as a
# separator so the range stays a range instead of being collapsed into a single house number.
RANGE_SEPARATORS = ("-", "–", "/")
RANGE_MAX_SPAN_METERS = 300.0
DERIVATION_ADDRESS_POINT = "single_address_point"
DERIVATION_RANGE_MIDPOINT = "range_endpoints_midpoint"
LOCALITY_EXACT = "exact"
LOCALITY_APPROXIMATE_RANGE = "approximate_range"
REASON_NOT_FOUND = "address_point_not_found_in_address_dataset"
REASON_QUALITY_UNSUPPORTED = "address_point_quality_not_metre_level"
REASON_AMBIGUOUS = "address_point_ambiguous_match"
REASON_CONFLICTING_RECORD = "address_point_conflicting_address_record"
REASON_OUTSIDE_BOUNDARY = "address_point_outside_berlin_boundary"
REASON_CRS_UNSUPPORTED = "address_point_source_crs_unsupported"
REASON_GEOMETRY_INVALID = "address_point_geometry_invalid"

# ETRS89 / UTM zone 33N on GRS80.
_UTM_SOUTH_HEMISPHERE = False
_UTM_SCALE = 0.9996
_GRS80_A = 6378137.0
_GRS80_F = 1 / 298.257222101
_GRS80_E2 = _GRS80_F * (2 - _GRS80_F)
_GRS80_EP2 = _GRS80_E2 / (1 - _GRS80_E2)


class WfsError(RuntimeError):
    """The dataset could not be read or its own contract was violated, so no point is claimed."""


@dataclass(frozen=True)
class AddressPoint:
    latitude: str
    longitude: str
    accuracy_meters: str
    address_record_id: str
    address_record_date: str | None
    dataset_identifier: str
    dataset_version: str
    source_crs: str
    source_x: str
    source_y: str
    source_reference: str
    source_digest: str
    source_observed_at: str
    derivation: str = DERIVATION_ADDRESS_POINT
    address_record_ids: tuple[str, ...] = ()
    locality_semantics: str = LOCALITY_EXACT

    def as_manifest_block(self, assertion_id: str) -> dict[str, Any]:
        return {
            "latitude": float(self.latitude),
            "longitude": float(self.longitude),
            "accuracyMeters": float(self.accuracy_meters),
            "derivation": self.derivation,
            "localitySemantics": self.locality_semantics,
            "addressRecordIds": list(self.address_record_ids or (self.address_record_id,)),
            "source": SOURCE_NAME,
            "datasetIdentifier": self.dataset_identifier,
            "datasetVersion": self.dataset_version,
            "addressRecordId": self.address_record_id,
            "addressRecordDate": self.address_record_date,
            "sourceReference": self.source_reference,
            "sourceCrs": self.source_crs,
            "sourceX": float(self.source_x),
            "sourceY": float(self.source_y),
            "sourceDigest": self.source_digest,
            "sourceObservedAtUtc": self.source_observed_at,
            "outputCrs": OUTPUT_CRS,
            "assertionId": assertion_id,
        }


@dataclass(frozen=True)
class AddressResolution:
    """The exact outcome of one lookup: either one point or one named unresolved reason."""

    point: AddressPoint | None
    reason: str | None

    @property
    def resolved(self) -> bool:
        return self.point is not None

    @classmethod
    def unresolved(cls, reason: str) -> AddressResolution:
        return cls(None, reason)

    @classmethod
    def resolved_at(cls, point: AddressPoint) -> AddressResolution:
        return cls(point, None)


def _meridional_arc(latitude: float) -> float:
    e2 = _GRS80_E2
    return _GRS80_A * (
        (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * latitude
        - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * math.sin(2 * latitude)
        + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * math.sin(4 * latitude)
        - (35 * e2**3 / 3072) * math.sin(6 * latitude)
    )


def utm33n_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """Inverse Transverse Mercator on GRS80 for UTM zone 33N, returned as (lat, lon) degrees.

    ``always_xy`` semantics: easting first, then northing.
    """
    if not (100_000 <= easting <= 900_000 and 0 <= northing <= 10_000_000):
        raise WfsError(f"point ({easting}, {northing}) is not a UTM 33N coordinate")

    k0 = _UTM_SCALE
    x = easting - 500_000.0
    y = northing if _UTM_SOUTH_HEMISPHERE else northing - 0.0

    m = y / k0
    mu = m / (_GRS80_A * (1 - _GRS80_E2 / 4 - 3 * _GRS80_E2**2 / 64 - 5 * _GRS80_E2**3 / 256))
    e1 = (1 - math.sqrt(1 - _GRS80_E2)) / (1 + math.sqrt(1 - _GRS80_E2))

    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )

    sin_phi1 = math.sin(phi1)
    cos_phi1 = math.cos(phi1)
    tan_phi1 = math.tan(phi1)

    c1 = _GRS80_EP2 * cos_phi1**2
    t1 = tan_phi1**2
    n1 = _GRS80_A / math.sqrt(1 - _GRS80_E2 * sin_phi1**2)
    r1 = _GRS80_A * (1 - _GRS80_E2) / (1 - _GRS80_E2 * sin_phi1**2) ** 1.5
    d = x / (n1 * k0)

    latitude = phi1 - (n1 * tan_phi1 / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * _GRS80_EP2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * _GRS80_EP2 - 3 * c1**2) * d**6 / 720
    )
    longitude = (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * _GRS80_EP2 + 24 * t1**2) * d**5 / 120
    ) / cos_phi1

    return math.degrees(latitude), math.degrees(longitude) + 15.0


def split_house_number(street_number: str) -> tuple[int, str] | None:
    """Split a house number into the dataset's numeric part and its letter suffix.

    A house number range names two buildings, so it identifies no single address point.
    """
    normalized = street_number.strip()
    digits = ""
    suffix = ""
    for character in normalized:
        if character.isdigit() and not suffix:
            digits += character
        elif character.isalpha():
            suffix += character
        elif character in " -/":
            return None
        else:
            return None
    if not digits:
        return None
    return int(digits), suffix


def parse_house_number_range(street_number: str) -> tuple[int, int] | None:
    """Numbers of a house-number range written as ``a-b`` or ``a/b``.

    A range names more than one building, so it never identifies one address point on its own;
    a caller may derive an explicitly approximate location from its endpoints. Suffixed ranges
    (``1a-3b``) stay unnamed here: the register's own suffix semantics would be guessed.
    """
    text = (street_number or "").strip().replace(" ", "")
    if not text:
        return None
    for separator in RANGE_SEPARATORS:
        if separator not in text:
            continue
        left, _, right = text.partition(separator)
        if left.isdigit() and right.isdigit():
            low, high = int(left), int(right)
            if low != high:
                return (min(low, high), max(low, high))
    return None


def normalize_street_name(value: str) -> str:
    """Normalized comparison key for a street name.

    The register writes ``Prinzessinnenstraße`` where the registry records
    ``Prinzessinnenstrasse``; case folding and the German sharp-s expansion make the two the
    same street. The registry abbreviates the street suffix (``Mollstr.``, ``Behaimstr``), so
    the final token is expanded to ``strasse``; anything else stays a literal comparison.
    """
    text = unicodedata.normalize("NFC", value).casefold().replace("ß", "ss")
    text = text.replace(".", " ").replace("-", " ")
    tokens = text.split()
    if tokens and tokens[-1].endswith("str") and not tokens[-1].endswith("strasse"):
        tokens[-1] = f"{tokens[-1]}asse"
    if tokens and tokens[-1].endswith("strasse") and len(tokens[-1]) > len("strasse"):
        # The register separates the suffix (``Skalitzer Straße``) where the registry writes one
        # word (``Skalitzerstr.``). Both forms name the same street, so the suffix is always its
        # own token; without this the exact-identity check rejects a genuinely present address.
        tokens = [*tokens[:-1], tokens[-1][: -len("strasse")], "strasse"]
    return " ".join(tokens)


@dataclass(frozen=True)
class AddressSearch:
    street: str
    street_number: str
    postal_code: str
    expected_address_record_id: str | None = None

    @property
    def number(self) -> int:
        split = split_house_number(self.street_number)
        if split is None:
            raise WfsError(f"house number {self.street_number!r} names no single address")
        return split[0]

    @property
    def suffix(self) -> str:
        split = split_house_number(self.street_number)
        if split is None:
            raise WfsError(f"house number {self.street_number!r} names no single address")
        return split[1].upper()

    @property
    def street_key(self) -> str:
        return normalize_street_name(self.street)

    def identity(self) -> dict[str, str | None]:
        """The complete search identity a cache entry must be keyed by."""
        return {
            "endpoint": WFS_ENDPOINT,
            "layer": WFS_LAYER,
            "sourceCrs": SOURCE_CRS,
            "street": self.street_key,
            "houseNumber": str(self.number),
            "houseNumberSuffix": self.suffix,
            "postalCode": self.postal_code.strip(),
            "expectedAddressRecordId": self.expected_address_record_id,
        }

    def cache_key(self) -> str:
        return hashlib.sha256(
            json.dumps(self.identity(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def create_search(
    street: str | None,
    street_number: str | None,
    postal_code: str | None,
    expected_address_record_id: str | None = None,
) -> AddressSearch | None:
    """Build a search only from a complete identity; an incomplete address has no search."""
    if not street or not street.strip() or not street_number or not street_number.strip():
        return None
    if not postal_code or not postal_code.strip():
        return None
    if split_house_number(street_number) is None:
        return None
    return AddressSearch(
        street.strip(),
        street_number.strip(),
        postal_code.strip(),
        expected_address_record_id.strip() if expected_address_record_id else None,
    )


def _cql_filter(search: AddressSearch) -> str:
    """Coarse filter only: the exact identity is verified against the returned features."""
    clauses = [f"hnr={search.number}", f"plz='{search.postal_code}'"]
    return " AND ".join(clauses)


def request_url(search: AddressSearch) -> str:
    query = urllib.parse.urlencode(
        {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": WFS_LAYER,
            "outputFormat": "application/json",
            "srsName": SOURCE_CRS,
            "CQL_FILTER": _cql_filter(search),
        }
    )
    return f"{WFS_ENDPOINT}?{query}"


def _read_cache(path: pathlib.Path, search: AddressSearch, reference: str) -> bytes | None:
    if not path.exists():
        return None
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WfsError(f"cached address response {path} is not readable JSON: {error}") from error
    if not isinstance(envelope, Mapping) or envelope.get("schemaVersion") != CACHE_SCHEMA:
        raise WfsError(
            f"cached address response {path} uses schema {envelope!r}; the cache must be "
            f"rebuilt for {CACHE_SCHEMA}"
        )
    if envelope.get("cacheKey") != search.cache_key():
        raise WfsError(
            f"cached address response {path} belongs to another search identity; it cannot be reused"
        )
    if envelope.get("requestUrl") != reference:
        raise WfsError(f"cached address response {path} was fetched from a different request url")
    try:
        payload = base64.b64decode(str(envelope["responseBytesBase64"]), validate=True)
    except (KeyError, ValueError) as error:
        raise WfsError(
            f"cached address response {path} carries no decodable response bytes"
        ) from error
    if hashlib.sha256(payload).hexdigest() != envelope.get("responseSha256"):
        raise WfsError(
            f"cached address response {path} does not match its recorded digest; the raw source "
            "bytes changed and the snapshot cannot be trusted"
        )
    return payload


def _write_cache(path: pathlib.Path, search: AddressSearch, reference: str, payload: bytes) -> None:
    envelope = {
        "schemaVersion": CACHE_SCHEMA,
        "cacheKey": search.cache_key(),
        "endpoint": WFS_ENDPOINT,
        "layer": WFS_LAYER,
        "sourceCrs": SOURCE_CRS,
        "requestUrl": reference,
        "responseSha256": hashlib.sha256(payload).hexdigest(),
        "responseBytesBase64": base64.b64encode(payload).decode("ascii"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _cache_path(cache_dir: pathlib.Path, search: AddressSearch) -> pathlib.Path:
    return cache_dir / f"{search.cache_key()[:32]}.json"


def _fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if urllib.parse.urlsplit(url).scheme != "https":
        raise WfsError(f"address dataset url must be https: {url}")
    opener = urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.URLError as error:
        raise WfsError(f"address dataset request failed: {error}") from error


def _normalized_crs(value: str) -> str | None:
    """The EPSG code a CRS name declares, whatever URN/URI form it is written in."""
    text = value.strip()
    if not text:
        return None
    digits = ""
    for part in reversed(text.replace("#", "/").replace("::", ":").replace(":", "/").split("/")):
        if part.isdigit():
            digits = part
            break
    return f"EPSG:{digits}" if digits else None


def _payload_crs(payload: Mapping[str, Any]) -> str | None:
    crs = payload.get("crs")
    if isinstance(crs, Mapping):
        properties = crs.get("properties")
        if isinstance(properties, Mapping):
            name = properties.get("name")
            if isinstance(name, str):
                return _normalized_crs(name)
    if isinstance(payload.get("crs"), str):
        return _normalized_crs(str(payload["crs"]))
    return None


def _observed_second(payload: Mapping[str, Any]) -> str:
    raw = payload.get("timeStamp")
    if not isinstance(raw, str) or not raw.strip():
        raise WfsError("address dataset response carries no response timestamp")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise WfsError(f"address dataset response timestamp is unreadable: {raw!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@functools.lru_cache(maxsize=1)
def berlin_boundary() -> tuple[str, tuple[tuple[tuple[float, float], ...], ...]]:
    """The official Berlin boundary polygon, verified against its recorded provenance digest."""
    try:
        raw = BOUNDARY_PATH.read_bytes()
        provenance = json.loads(BOUNDARY_PROVENANCE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WfsError(f"official Berlin boundary is unreadable: {error}") from error
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if provenance.get("boundary_digest") != digest:
        raise WfsError(
            f"official Berlin boundary {BOUNDARY_PATH} does not match its provenance digest "
            f"{provenance.get('boundary_digest')!r}"
        )
    geometry = json.loads(raw.decode("utf-8"))
    polygons = _geojson_polygons(geometry)
    if not polygons:
        raise WfsError("official Berlin boundary carries no polygon geometry")
    return digest, polygons


def _geojson_polygons(geometry: Mapping[str, Any]) -> tuple[tuple[tuple[float, float], ...], ...]:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    raw_polygons: list[Any]
    if kind == "Polygon":
        raw_polygons = [coordinates]
    elif kind == "MultiPolygon":
        raw_polygons = list(coordinates)
    else:
        return ()
    polygons: list[tuple[tuple[float, float], ...]] = []
    for polygon in raw_polygons:
        rings = tuple(
            tuple((float(point[0]), float(point[1])) for point in ring) for ring in polygon
        )
        if rings:
            # GeoJSON rings are flat on the sphere enough for this containment test; the
            # official boundary does not cross the antimeridian.
            polygons.append(rings)
    return tuple(polygons)


def _ring_contains(ring: tuple[tuple[float, float], ...], x: float, y: float) -> bool:
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing:
                inside = not inside
        previous = current
    return inside


def _boundary_contains(
    boundary: tuple[tuple[tuple[float, float], ...], ...], latitude: float, longitude: float
) -> bool:
    return any(
        _ring_contains(polygon[0], longitude, latitude)
        and not any(_ring_contains(hole, longitude, latitude) for hole in polygon[1:])
        for polygon in boundary
    )


def _in_berlin(latitude: float, longitude: float) -> bool:
    _, boundary = berlin_boundary()
    return _boundary_contains(boundary, latitude, longitude)


def _number(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _feature_crs(feature: Mapping[str, Any]) -> str | None:
    crs = feature.get("crs")
    if isinstance(crs, Mapping):
        properties = crs.get("properties")
        if isinstance(properties, Mapping) and isinstance(properties.get("name"), str):
            return _normalized_crs(properties["name"])
    if isinstance(crs, str):
        return _normalized_crs(crs)
    return None


def _matches_identity(properties: Mapping[str, Any], search: AddressSearch) -> bool:
    if _text(properties.get("typ")) != ACCEPTED_TYPE:
        return False
    if _text(properties.get("plz")) != search.postal_code:
        return False
    if _number(properties.get("hnr")) != search.number:
        return False
    if _text(properties.get("hnr_zusatz")).upper() != search.suffix:
        return False
    street = _text(properties.get("str_name"))
    if not street or normalize_street_name(street) != search.street_key:
        return False
    return True


def resolve_address_point(
    payload: Mapping[str, Any],
    search: AddressSearch,
    reference: str,
    *,
    raw_bytes: bytes | None = None,
) -> AddressResolution:
    """Resolve exactly one address point for a complete search identity."""
    if not isinstance(payload, Mapping):
        raise WfsError("address dataset response is not a JSON object")
    if _payload_crs(payload) != SOURCE_CRS:
        return AddressResolution.unresolved(REASON_CRS_UNSUPPORTED)

    observed_at = _observed_second(payload)
    matches: list[tuple[Mapping[str, Any], float, float]] = []
    record_ids: set[str] = set()
    for feature in payload.get("features") or []:
        if not isinstance(feature, Mapping):
            continue
        declared = _feature_crs(feature)
        if declared is not None and declared != SOURCE_CRS:
            return AddressResolution.unresolved(REASON_CRS_UNSUPPORTED)
        properties = feature.get("properties")
        if not isinstance(properties, Mapping):
            continue
        if not _matches_identity(properties, search):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, Mapping) or geometry.get("type") != "Point":
            return AddressResolution.unresolved(REASON_GEOMETRY_INVALID)
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) != 2:
            return AddressResolution.unresolved(REASON_GEOMETRY_INVALID)
        try:
            x, y = float(coordinates[0]), float(coordinates[1])
        except (TypeError, ValueError):
            return AddressResolution.unresolved(REASON_GEOMETRY_INVALID)
        record_id = _text(properties.get("adressid"))
        if not record_id:
            raise WfsError(
                f"address point for {search.postal_code}/{search.street_number} carries no address record id"
            )
        record_ids.add(record_id)
        matches.append((properties, x, y))

    if not matches:
        return AddressResolution.unresolved(REASON_NOT_FOUND)
    # The register may hold several rows for one address: a metre-level entrance point
    # (``Qualitaet A``) and a coarser building point (``Qualitaet B``) of the same building. The
    # precise row is the address point; a coarser row for the same number is not a competing
    # address, so only the precise rows decide identity. Without a precise row nothing is claimed.
    precise = [match for match in matches if _text(match[0].get("qualitaet")) == ACCEPTED_QUALITY]
    if not precise:
        observed = sorted({_text(match[0].get("qualitaet")) or "unknown" for match in matches})
        return AddressResolution.unresolved(f"{REASON_QUALITY_UNSUPPORTED}:{'/'.join(observed)}")
    matches = precise
    record_ids = {_text(properties.get("adressid")) for properties, _, _ in matches}
    if (
        search.expected_address_record_id is not None
        and search.expected_address_record_id not in record_ids
    ):
        return AddressResolution.unresolved(REASON_CONFLICTING_RECORD)
    if len(record_ids) != 1 or len(matches) != 1:
        # Two features with the same record id are still an ambiguous source; the dataset
        # must identify one address record, not two rows for one address.
        return AddressResolution.unresolved(REASON_AMBIGUOUS)

    properties, x, y = matches[0]
    try:
        latitude, longitude = utm33n_to_wgs84(x, y)
    except WfsError:
        return AddressResolution.unresolved(REASON_GEOMETRY_INVALID)
    if not _in_berlin(latitude, longitude):
        return AddressResolution.unresolved(REASON_OUTSIDE_BOUNDARY)

    return AddressResolution.resolved_at(
        AddressPoint(
            latitude=f"{latitude:.7f}",
            longitude=f"{longitude:.7f}",
            accuracy_meters=ADDRESS_POINT_ACCURACY_METERS,
            address_record_id=_text(properties.get("adressid")),
            address_record_date=_text(properties.get("adr_datum")) or None,
            dataset_identifier=DATASET_IDENTIFIER,
            dataset_version=f"{WFS_LAYER}@{observed_at}",
            source_crs=SOURCE_CRS,
            source_x=f"{x:.3f}",
            source_y=f"{y:.3f}",
            source_reference=reference,
            source_digest=hashlib.sha256(
                raw_bytes
                if raw_bytes is not None
                else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            source_observed_at=observed_at,
            derivation=DERIVATION_ADDRESS_POINT,
            address_record_ids=(_text(properties.get("adressid")),),
            locality_semantics=LOCALITY_EXACT,
        )
    )


class AddressPointLookup:
    """Address point lookup over an immutable raw-response snapshot cache.

    The cache key is the complete normalized search identity plus the endpoint, layer and
    source CRS, so a changed street, number, suffix, postal code or source cannot read another
    address's response. Cached bytes are the exact response the coordinate was derived from;
    the digest is verified on every read.
    """

    def __init__(self, cache_dir: pathlib.Path, *, timeout: float = 20.0) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.requests_made = 0
        self.cache_hits = 0

    def __call__(
        self,
        street: str | None,
        street_number: str | None,
        postal_code: str | None,
        expected_address_record_id: str | None = None,
    ) -> AddressResolution:
        range_numbers = parse_house_number_range(street_number or "")
        if range_numbers is not None:
            return self._range_point(street, postal_code, range_numbers, expected_address_record_id)
        search = create_search(street, street_number, postal_code, expected_address_record_id)
        if search is None:
            return AddressResolution.unresolved(REASON_ADDRESS_INCOMPLETE)
        path = _cache_path(self.cache_dir, search)
        reference = request_url(search)
        cached = _read_cache(path, search, reference)
        if cached is None:
            payload_bytes = _fetch(reference, self.timeout)
            self.requests_made += 1
            _write_cache(path, search, reference, payload_bytes)
        else:
            self.cache_hits += 1
            payload_bytes = cached
        payload = json.loads(payload_bytes.decode("utf-8"))
        return resolve_address_point(payload, search, reference, raw_bytes=payload_bytes)

    def _range_point(
        self,
        street: str | None,
        postal_code: str | None,
        numbers: tuple[int, int],
        expected_address_record_id: str | None,
    ) -> AddressResolution:
        """Derive an explicitly approximate location from both ends of a house-number range.

        Each end must resolve as its own metre-level address point. The published point is the
        middle of the two, its accuracy is half of their distance, and it says so: a range never
        yields a metre-level coordinate for the building.
        """
        if not street or not postal_code:
            return AddressResolution.unresolved(REASON_ADDRESS_INCOMPLETE)
        endpoints: list[AddressPoint] = []
        for number in numbers:
            resolution = self(street, str(number), postal_code, None)
            if not resolution.resolved:
                return AddressResolution.unresolved(
                    f"{REASON_RANGE_INCOMPLETE}:{number}:{resolution.reason}"
                )
            endpoints.append(resolution.point)  # type: ignore[arg-type]
        identifiers = tuple(point.address_record_id for point in endpoints)
        if expected_address_record_id is not None and expected_address_record_id not in identifiers:
            return AddressResolution.unresolved(REASON_CONFLICTING_RECORD)
        if len({point.dataset_identifier for point in endpoints}) != 1:
            return AddressResolution.unresolved(REASON_CONFLICTING_RECORD)
        first, last = endpoints
        x = (float(first.source_x) + float(last.source_x)) / 2
        y = (float(first.source_y) + float(last.source_y)) / 2
        span = math.dist(
            (float(first.source_x), float(first.source_y)),
            (float(last.source_x), float(last.source_y)),
        )
        if span > RANGE_MAX_SPAN_METERS:
            return AddressResolution.unresolved(f"{REASON_RANGE_TOO_WIDE}:{span:.0f}")
        try:
            latitude, longitude = utm33n_to_wgs84(x, y)
        except WfsError:
            return AddressResolution.unresolved(REASON_GEOMETRY_INVALID)
        if not _in_berlin(latitude, longitude):
            return AddressResolution.unresolved(REASON_OUTSIDE_BOUNDARY)
        accuracy = max(span / 2, float(ADDRESS_POINT_ACCURACY_METERS))
        return AddressResolution.resolved_at(
            AddressPoint(
                latitude=f"{latitude:.7f}",
                longitude=f"{longitude:.7f}",
                accuracy_meters=f"{accuracy:.1f}",
                address_record_id=first.address_record_id,
                address_record_date=first.address_record_date,
                dataset_identifier=first.dataset_identifier,
                dataset_version=first.dataset_version,
                source_crs=first.source_crs,
                source_x=f"{x:.3f}",
                source_y=f"{y:.3f}",
                source_reference=first.source_reference,
                source_digest=hashlib.sha256(
                    f"{first.source_digest}\n{last.source_digest}".encode()
                ).hexdigest(),
                source_observed_at=max(first.source_observed_at, last.source_observed_at),
                derivation=DERIVATION_RANGE_MIDPOINT,
                address_record_ids=identifiers,
                locality_semantics=LOCALITY_APPROXIMATE_RANGE,
            )
        )


def lookup_without_cache(
    street: str, street_number: str, postal_code: str, expected_address_record_id: str | None = None
) -> AddressResolution:
    """One-off lookup used by fixtures and by manual verification."""
    search = create_search(street, street_number, postal_code, expected_address_record_id)
    if search is None:
        return AddressResolution.unresolved(REASON_ADDRESS_INCOMPLETE)
    reference = request_url(search)
    payload_bytes = _fetch(reference, 20.0)
    payload = json.loads(payload_bytes.decode("utf-8"))
    return resolve_address_point(payload, search, reference, raw_bytes=payload_bytes)
