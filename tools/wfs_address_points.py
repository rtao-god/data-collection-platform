"""Official Berlin address points as publishable coordinates.

The registry evidence proves *which* address record a studio matches (house number and postal
code); the address dataset itself carries the point. This module turns one evidence request into
a coordinate with an explicit CRS origin, never into a coordinate inferred from a text excerpt.

Two properties matter and are enforced here:

* The point comes from the address dataset, not from an official studio website. A coordinate
  that appears inside a website excerpt is never treated as an address point.
* A record without a matching address point stays unresolved: no district centroid, no postal
  code centroid, no rounding of an unrelated point.
"""

from __future__ import annotations

# ruff: noqa: S310 - every url is built by request_url() from one fixed https endpoint.
import hashlib
import json
import math
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

WFS_ENDPOINT = "https://gdi.berlin.de/services/wfs/adressen_berlin"
DATASET_IDENTIFIER = "adressen_berlin"
SOURCE_NAME = "official_berlin_address_register"

# Berlin's extent. A point outside it is not a Berlin address, whatever the response claims.
BERLIN_LATITUDE_RANGE = (52.30, 52.72)
BERLIN_LONGITUDE_RANGE = (13.00, 13.85)

# The dataset publishes address points at metre level, so a claimed precision coarser than that
# would understate the evidence and a finer one would overstate it.
ADDRESS_POINT_ACCURACY_METERS = "1.0"

# ETRS89 / UTM zone 33N, the CRS the WFS returns unless a client asks for something else.
_UTM_SOUTH_HEMISPHERE = False
_UTM_SCALE = 0.9996
_GRS80_A = 6378137.0
_GRS80_F = 1 / 298.257222101
_GRS80_E2 = _GRS80_F * (2 - _GRS80_F)
_GRS80_EP2 = _GRS80_E2 / (1 - _GRS80_E2)


class WfsError(RuntimeError):
    """The dataset could not be read, so no coordinate may be claimed."""


@dataclass(frozen=True)
class AddressPoint:
    latitude: str
    longitude: str
    accuracy_meters: str
    address_record_id: str
    dataset_identifier: str
    source_crs: str
    source_reference: str
    source_digest: str

    def as_manifest_block(self, assertion_id: str) -> dict[str, Any]:
        return {
            "latitude": float(self.latitude),
            "longitude": float(self.longitude),
            "accuracyMeters": float(self.accuracy_meters),
            "source": SOURCE_NAME,
            "datasetIdentifier": self.dataset_identifier,
            "addressRecordId": self.address_record_id,
            "sourceReference": self.source_reference,
            "sourceCrs": self.source_crs,
            "assertionId": assertion_id,
        }


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


def _point_from_coordinates(x: float, y: float) -> tuple[float, float, str]:
    """Detect the CRS from the magnitude, then return (lat, lon, source_crs)."""
    if abs(x) <= 180 and abs(y) <= 90:
        return y, x, "EPSG:4326"
    latitude, longitude = utm33n_to_wgs84(x, y)
    return latitude, longitude, "EPSG:25833"


def _in_berlin(latitude: float, longitude: float) -> bool:
    return (
        BERLIN_LATITUDE_RANGE[0] <= latitude <= BERLIN_LATITUDE_RANGE[1]
        and BERLIN_LONGITUDE_RANGE[0] <= longitude <= BERLIN_LONGITUDE_RANGE[1]
    )


ACCEPTED_QUALITY = "Qualitaet A"


def split_house_number(street_number: str) -> tuple[int, str] | None:
    """Split a registry house number into the dataset's numeric part and its letter suffix.

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


def _cql_filter(house_number: int, suffix: str, postal_code: str) -> str:
    clauses = [f"hnr={house_number}"]
    clauses.append(f"hnr_zusatz='{suffix.upper()}'" if suffix else "hnr_zusatz IS NULL")
    clauses.append(f"plz='{postal_code}'")
    return " AND ".join(clauses)


def request_url(street_number: str, postal_code: str) -> str:
    split = split_house_number(street_number)
    if split is None:
        raise WfsError(f"house number {street_number!r} names no single address")
    query = urllib.parse.urlencode(
        {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "adressen_berlin",
            "outputFormat": "application/json",
            "CQL_FILTER": _cql_filter(split[0], split[1], postal_code),
        }
    )
    return f"{WFS_ENDPOINT}?{query}"


def _cache_path(cache_dir: pathlib.Path, street_number: str, postal_code: str) -> pathlib.Path:
    key = f"{postal_code}|{street_number}"
    return cache_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}.json"


def _read_cache(path: pathlib.Path) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:  # pragma: no cover - corrupt cache is a hard failure
        raise WfsError(f"cached address response {path} is not readable JSON: {error}") from error


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


def _dataset_identifier(properties: Mapping[str, Any]) -> str:
    """The record's own dataset date, so an unchanged address keeps one stable identifier."""
    recorded = str(properties.get("adr_datum") or "").strip()
    return f"{DATASET_IDENTIFIER}@{recorded}" if recorded else DATASET_IDENTIFIER


def parse_address_point(
    payload: Mapping[str, Any], street_number: str, postal_code: str, reference: str
) -> AddressPoint | None:
    """Return the point of the exact house number, or None when the dataset does not provide one."""
    split = split_house_number(street_number)
    if split is None:
        return None
    expected_number, expected_suffix = split

    coordinate_ref = None
    for feature in payload.get("features") or []:
        properties = feature.get("properties") or {}
        try:
            number = int(str(properties.get("hnr") or "").strip())
        except ValueError:
            continue
        if number != expected_number:
            continue
        suffix = str(properties.get("hnr_zusatz") or "").strip()
        if suffix.upper() != expected_suffix.upper():
            continue
        if str(properties.get("plz") or "").strip() != postal_code:
            continue
        if str(properties.get("typ") or "").strip() != "Adresse":
            continue
        if str(properties.get("qualitaet") or "").strip() != ACCEPTED_QUALITY:
            continue
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            x, y = float(coordinates[0]), float(coordinates[1])
        except (TypeError, ValueError):
            continue
        coordinate_ref = (x, y, properties)
        break

    if coordinate_ref is None:
        return None

    x, y, properties = coordinate_ref
    latitude, longitude, source_crs = _point_from_coordinates(x, y)
    if not _in_berlin(latitude, longitude):
        return None

    address_record_id = str(properties.get("adressid") or "").strip()
    if not address_record_id:
        raise WfsError(
            f"address point for {postal_code}/{street_number} carries no address record id"
        )

    return AddressPoint(
        latitude=f"{latitude:.7f}",
        longitude=f"{longitude:.7f}",
        accuracy_meters=ADDRESS_POINT_ACCURACY_METERS,
        address_record_id=address_record_id,
        dataset_identifier=_dataset_identifier(properties),
        source_crs=source_crs,
        source_reference=reference,
        source_digest=hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    )


class AddressPointLookup:
    """Address point lookup with a response cache keyed by the address itself.

    Re-running an unchanged import must not touch the network, so a cached response is reused
    as-is; the cache key is the address, and the dataset revision travels inside the response.
    """

    def __init__(self, cache_dir: pathlib.Path, *, timeout: float = 20.0) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.requests_made = 0
        self.cache_hits = 0

    def __call__(self, street_number: str, postal_code: str) -> AddressPoint | None:
        if not street_number or not postal_code:
            return None
        if split_house_number(street_number) is None:
            return None
        path = _cache_path(self.cache_dir, street_number, postal_code)
        cached = _read_cache(path)
        reference = request_url(street_number, postal_code)
        if cached is None:
            payload_bytes = _fetch(reference, self.timeout)
            self.requests_made += 1
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload_bytes)
            payload = json.loads(payload_bytes.decode("utf-8"))
        else:
            self.cache_hits += 1
            payload = cached
        return parse_address_point(payload, street_number, postal_code, reference)


def lookup_without_cache(street_number: str, postal_code: str) -> AddressPoint | None:
    """One-off lookup used by fixtures and by manual verification."""
    payload = json.loads(_fetch(request_url(street_number, postal_code), 20.0).decode("utf-8"))
    return parse_address_point(
        payload, street_number, postal_code, request_url(street_number, postal_code)
    )
