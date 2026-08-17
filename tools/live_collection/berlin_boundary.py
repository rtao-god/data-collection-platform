from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import yaml
from defusedxml import ElementTree

from collection_application.geography import decode_boundary_geojson

_OFFICIAL_HOST_SUFFIX = ".berlin.de"
_PORTAL_ENDPOINTS = (
    "https://daten.berlin.de/api/3/action/package_search",
    "https://datenregister.berlin.de/api/3/action/package_search",
)
_SEARCH_QUERIES = (
    "Landesgrenze Berlin",
    "Verwaltungsgrenzen Berlin",
    "Bezirksgrenzen Berlin",
)
_GEOJSON_FORMATS = frozenset({"geojson", "json"})
_BOUNDARY_NAME_TOKENS = (
    "landesgrenze",
    "verwaltungsgrenze",
    "bezirksgrenze",
    "berlin boundary",
)
_MIN_LON = 12.9
_MAX_LON = 13.9
_MIN_LAT = 52.2
_MAX_LAT = 52.8


class BoundaryMaterializationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResourceCandidate:
    dataset_title: str
    dataset_identifier: str
    resource_identifier: str
    resource_url: str
    resource_format: str
    license_identifier: str
    license_title: str
    score: int

    def __post_init__(self) -> None:
        _require_official_url(self.resource_url)
        if not self.dataset_title.strip():
            raise ValueError("dataset title cannot be empty")
        if not self.resource_identifier.strip():
            raise ValueError("resource identifier cannot be empty")
        if not self.license_identifier.strip() and not self.license_title.strip():
            raise ValueError("official boundary resource must declare a license")


@dataclass(frozen=True, slots=True)
class MaterializedBoundary:
    geojson: Mapping[str, Any]
    source_digest: str
    source_url: str
    feature_type: str | None


def _require_official_url(value: str) -> None:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "berlin.de" or host.endswith(_OFFICIAL_HOST_SUFFIX)
    ):
        raise BoundaryMaterializationError(
            f"boundary source must use HTTPS on an official Berlin host: {value}"
        )


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _score_resource(dataset: Mapping[str, object], resource: Mapping[str, object]) -> int:
    title = " ".join(
        (
            _text(dataset.get("title")),
            _text(dataset.get("name")),
            _text(resource.get("name")),
            _text(resource.get("description")),
        )
    ).lower()
    resource_format = _text(resource.get("format")).lower()
    url = _text(resource.get("url")).lower()
    score = 0
    for index, token in enumerate(_BOUNDARY_NAME_TOKENS):
        if token in title:
            score += 120 - index * 10
    if "landesgrenze" in title:
        score += 100
    if resource_format in _GEOJSON_FORMATS or "geojson" in url:
        score += 80
    if resource_format == "wfs" or "service=wfs" in url or "/wfs" in url:
        score += 60
    if "bezirks" in title:
        score += 30
    if "archiv" in title or "histor" in title:
        score -= 40
    return score


def discover_candidates(client: httpx.Client) -> tuple[ResourceCandidate, ...]:
    candidates: dict[tuple[str, str], ResourceCandidate] = {}
    failures: list[str] = []
    for endpoint in _PORTAL_ENDPOINTS:
        for query in _SEARCH_QUERIES:
            try:
                response = client.get(endpoint, params={"q": query, "rows": 100})
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(f"{endpoint}: {type(exc).__name__}")
                continue
            if not isinstance(payload, Mapping) or payload.get("success") is not True:
                failures.append(f"{endpoint}: invalid CKAN response")
                continue
            result = payload.get("result")
            if not isinstance(result, Mapping):
                continue
            datasets = result.get("results")
            if not isinstance(datasets, Sequence):
                continue
            for dataset in datasets:
                if not isinstance(dataset, Mapping):
                    continue
                dataset_title = _text(dataset.get("title"))
                dataset_identifier = _text(dataset.get("id")) or _text(dataset.get("name"))
                license_identifier = _text(dataset.get("license_id"))
                license_title = _text(dataset.get("license_title"))
                resources = dataset.get("resources")
                if not isinstance(resources, Sequence):
                    continue
                for resource in resources:
                    if not isinstance(resource, Mapping):
                        continue
                    resource_url = _text(resource.get("url"))
                    if not resource_url:
                        continue
                    try:
                        _require_official_url(resource_url)
                    except BoundaryMaterializationError:
                        continue
                    score = _score_resource(dataset, resource)
                    if score <= 0:
                        continue
                    identifier = (
                        _text(resource.get("id"))
                        or hashlib.sha256(resource_url.encode("utf-8")).hexdigest()
                    )
                    try:
                        candidate = ResourceCandidate(
                            dataset_title=dataset_title,
                            dataset_identifier=dataset_identifier,
                            resource_identifier=identifier,
                            resource_url=resource_url,
                            resource_format=_text(resource.get("format")).lower(),
                            license_identifier=license_identifier,
                            license_title=license_title,
                            score=score,
                        )
                    except ValueError:
                        continue
                    candidates[(dataset_identifier, identifier)] = candidate
    if not candidates:
        detail = "; ".join(sorted(set(failures))) or "no matching resources"
        raise BoundaryMaterializationError(
            "official Berlin data portals yielded no licensed boundary resource: " + detail
        )
    return tuple(
        sorted(
            candidates.values(),
            key=lambda item: (-item.score, item.dataset_title, item.resource_identifier),
        )
    )


def _is_geojson(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("type") in {
        "Feature",
        "FeatureCollection",
        "Polygon",
        "MultiPolygon",
    }


def _replace_query(url: str, values: Mapping[str, str]) -> str:
    parsed = urlparse(url)
    existing = {
        key.lower(): value for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    }
    existing.update({key.lower(): value for key, value in values.items()})
    return urlunparse(parsed._replace(query=urlencode(existing)))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _feature_types(capabilities: bytes) -> tuple[tuple[str, str], ...]:
    try:
        root = ElementTree.fromstring(capabilities)
    except ElementTree.ParseError as exc:
        raise BoundaryMaterializationError("WFS capabilities XML is invalid") from exc
    values: list[tuple[str, str]] = []
    for node in root.iter():
        if _local_name(node.tag) != "FeatureType":
            continue
        name = ""
        title = ""
        for child in node:
            if _local_name(child.tag) == "Name" and child.text:
                name = child.text.strip()
            elif _local_name(child.tag) == "Title" and child.text:
                title = child.text.strip()
        if name:
            values.append((name, title))
    return tuple(values)


def _feature_type_score(name: str, title: str) -> int:
    text = f"{name} {title}".lower()
    score = 0
    for index, token in enumerate(_BOUNDARY_NAME_TOKENS):
        if token in text:
            score += 120 - index * 10
    if "landesgrenze" in text:
        score += 100
    if "bezirk" in text:
        score += 30
    return score


def _download_json(client: httpx.Client, url: str) -> tuple[object, bytes]:
    response = client.get(url)
    response.raise_for_status()
    body = response.content
    try:
        value = json.loads(body)
    except ValueError as exc:
        raise BoundaryMaterializationError(f"resource is not JSON: {url}") from exc
    return value, body


def _download_wfs(
    client: httpx.Client,
    candidate: ResourceCandidate,
) -> MaterializedBoundary:
    capabilities_url = _replace_query(
        candidate.resource_url,
        {"service": "WFS", "request": "GetCapabilities"},
    )
    response = client.get(capabilities_url)
    response.raise_for_status()
    feature_types = sorted(
        _feature_types(response.content),
        key=lambda item: (-_feature_type_score(*item), item[0]),
    )
    if not feature_types or _feature_type_score(*feature_types[0]) <= 0:
        raise BoundaryMaterializationError(
            f"WFS contains no Berlin administrative boundary feature: {candidate.resource_url}"
        )
    feature_type = feature_types[0][0]
    errors: list[str] = []
    for output_format in ("application/json", "json", "geojson"):
        feature_url = _replace_query(
            candidate.resource_url,
            {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typenames": feature_type,
                "srsname": "EPSG:4326",
                "outputformat": output_format,
                "count": "100",
            },
        )
        try:
            value, body = _download_json(client, feature_url)
        except (BoundaryMaterializationError, httpx.HTTPError) as exc:
            errors.append(type(exc).__name__)
            continue
        if _is_geojson(value):
            return MaterializedBoundary(
                geojson=_as_mapping(value),
                source_digest=_sha256(body),
                source_url=feature_url,
                feature_type=feature_type,
            )
    raise BoundaryMaterializationError(
        f"WFS did not provide GeoJSON for {feature_type}: {', '.join(errors)}"
    )


def _as_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BoundaryMaterializationError("GeoJSON root must be an object")
    return value


def download_candidate(
    client: httpx.Client,
    candidate: ResourceCandidate,
) -> MaterializedBoundary:
    try:
        value, body = _download_json(client, candidate.resource_url)
    except (BoundaryMaterializationError, httpx.HTTPError):
        return _download_wfs(client, candidate)
    if not _is_geojson(value):
        return _download_wfs(client, candidate)
    return MaterializedBoundary(
        geojson=_as_mapping(value),
        source_digest=_sha256(body),
        source_url=candidate.resource_url,
        feature_type=None,
    )


def _feature_geometries(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    kind = value.get("type")
    if kind == "FeatureCollection":
        features = value.get("features")
        if not isinstance(features, Sequence):
            raise BoundaryMaterializationError("FeatureCollection features must be an array")
        result: list[Mapping[str, Any]] = []
        for feature in features:
            if not isinstance(feature, Mapping) or feature.get("type") != "Feature":
                raise BoundaryMaterializationError("boundary feature is invalid")
            geometry = feature.get("geometry")
            if not isinstance(geometry, Mapping):
                raise BoundaryMaterializationError("boundary feature geometry is missing")
            result.append(geometry)
        return tuple(result)
    if kind == "Feature":
        geometry = value.get("geometry")
        if not isinstance(geometry, Mapping):
            raise BoundaryMaterializationError("boundary feature geometry is missing")
        return (geometry,)
    return (value,)


def _iter_positions(value: object) -> Iterable[tuple[float, float]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if (
            len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            yield float(value[0]), float(value[1])
            return
        for item in value:
            yield from _iter_positions(item)


def _bounds(geometries: Sequence[Mapping[str, Any]]) -> tuple[float, float, float, float]:
    positions = tuple(
        position
        for geometry in geometries
        for position in _iter_positions(geometry.get("coordinates"))
    )
    if not positions:
        raise BoundaryMaterializationError("boundary geometry contains no positions")
    longitudes = tuple(item[0] for item in positions)
    latitudes = tuple(item[1] for item in positions)
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


def canonicalize_boundary(value: Mapping[str, Any]) -> Mapping[str, Any]:
    geometries = _feature_geometries(value)
    for candidate_geometry in geometries:
        if candidate_geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise BoundaryMaterializationError(
                f"unsupported boundary geometry: {candidate_geometry.get('type')}"
            )
    min_lon, min_lat, max_lon, max_lat = _bounds(geometries)
    if not (
        _MIN_LON <= min_lon <= max_lon <= _MAX_LON and _MIN_LAT <= min_lat <= max_lat <= _MAX_LAT
    ):
        raise BoundaryMaterializationError(
            "boundary coordinates are outside the Berlin validation envelope"
        )
    if len(geometries) == 1:
        canonical_geometry: Mapping[str, Any] = geometries[0]
    else:
        try:
            from shapely.geometry import mapping, shape
            from shapely.ops import unary_union
        except ImportError as exc:
            raise BoundaryMaterializationError(
                "multiple official boundary features require shapely for deterministic union"
            ) from exc
        merged = unary_union(tuple(shape(dict(item)) for item in geometries))
        if merged.geom_type not in {"Polygon", "MultiPolygon"} or not merged.is_valid:
            raise BoundaryMaterializationError("official boundary union is not a valid polygon")
        canonical_geometry = _as_mapping(mapping(merged))
    return {
        "type": "Feature",
        "id": "berlin-administrative-boundary",
        "properties": {
            "name": "Berlin administrative boundary",
            "authority": "State of Berlin",
        },
        "geometry": canonical_geometry,
    }


def _update_yaml_value(value: object, boundary_path: str, digest: str) -> tuple[object, int]:
    changes = 0
    if isinstance(value, list):
        updated: list[object] = []
        for item in value:
            if isinstance(item, Mapping) and item.get("code") == "BERLIN_BOUNDARY_ARTIFACT_MISSING":
                changes += 1
                continue
            rewritten, item_changes = _update_yaml_value(item, boundary_path, digest)
            changes += item_changes
            updated.append(rewritten)
        return updated, changes
    if not isinstance(value, Mapping):
        return value, 0
    result: dict[object, object] = {}
    for key, item in value.items():
        lowered = str(key).lower()
        if "boundary" in lowered and "digest" in lowered:
            result[key] = digest
            changes += 1
            continue
        if (
            "boundary" in lowered
            and any(token in lowered for token in ("path", "artifact", "reference", "file"))
            and isinstance(item, str)
        ):
            result[key] = boundary_path
            changes += 1
            continue
        rewritten, item_changes = _update_yaml_value(item, boundary_path, digest)
        result[key] = rewritten
        changes += item_changes
    readiness = result.get("readiness")
    if isinstance(readiness, Mapping):
        blockers = readiness.get("blockers")
        if isinstance(blockers, Sequence) and not blockers:
            readiness = dict(readiness)
            readiness["state"] = "ready"
            readiness.pop("blockers", None)
            result["readiness"] = readiness
    return result, changes


def update_campaign(
    campaign_root: Path,
    *,
    boundary_path: str,
    digest: str,
) -> tuple[Path, ...]:
    modified: list[Path] = []
    for path in sorted(campaign_root.glob("*.yaml")):
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        rewritten, changes = _update_yaml_value(value, boundary_path, digest)
        if changes:
            path.write_text(
                yaml.safe_dump(
                    rewritten,
                    allow_unicode=True,
                    sort_keys=False,
                    width=100,
                ),
                encoding="utf-8",
            )
            modified.append(path)
    if not modified:
        raise BoundaryMaterializationError(
            "campaign contains no explicit Berlin boundary artifact placeholders"
        )
    return tuple(modified)


def _distribution_feature_count(value: Mapping[str, Any]) -> int:
    if value.get("type") != "FeatureCollection":
        return 1
    features = value.get("features")
    if not isinstance(features, Sequence) or isinstance(features, (str, bytes, bytearray)):
        raise BoundaryMaterializationError("boundary FeatureCollection has no feature sequence")
    if not features:
        raise BoundaryMaterializationError("boundary FeatureCollection is empty")
    return len(features)


def _campaign_geography_revision(campaign_root: Path) -> str:
    campaign_path = campaign_root / "campaign.yaml"
    value = yaml.safe_load(campaign_path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise BoundaryMaterializationError("campaign.yaml must contain one mapping")
    revision = value.get("geography_revision")
    if not isinstance(revision, str) or not revision.strip():
        raise BoundaryMaterializationError(
            "campaign.yaml must declare an explicit geography_revision"
        )
    return revision


def write_geography_revision(
    repository_root: Path,
    *,
    canonical_boundary: Mapping[str, Any],
    source_digest: str,
    source_url: str,
    dataset_title: str,
    dataset_identifier: str,
    distribution_owner: str,
    distribution_feature_count: int,
    license_identifier: str,
    license_title: str,
) -> tuple[Path, Path, tuple[Path, ...]]:
    if distribution_feature_count <= 0:
        raise BoundaryMaterializationError("distribution feature count must be positive")
    if not source_digest.startswith("sha256:") or len(source_digest) != 71:
        raise BoundaryMaterializationError("source digest must be one SHA-256 identity")
    required_text = {
        "source URL": source_url,
        "dataset title": dataset_title,
        "dataset identifier": dataset_identifier,
        "distribution owner": distribution_owner,
        "license identifier": license_identifier,
        "license title": license_title,
    }
    missing = tuple(label for label, value in required_text.items() if not value.strip())
    if missing:
        raise BoundaryMaterializationError(
            "geography provenance is missing required values: " + ", ".join(missing)
        )

    campaign_root = repository_root / "campaigns/berlin_recording_services"
    boundary_relative = "geography/berlin-boundary.geojson"
    provenance_relative = "geography/berlin-boundary.provenance.json"
    boundary_path = campaign_root / boundary_relative
    provenance_path = campaign_root / provenance_relative
    geography_path = campaign_root / "geography.yaml"

    canonical_feature = canonicalize_boundary(canonical_boundary)
    geometry = canonical_feature.get("geometry")
    if not isinstance(geometry, Mapping):
        raise BoundaryMaterializationError("canonical Berlin boundary feature has no geometry")
    boundary = decode_boundary_geojson(
        json.dumps(
            geometry,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    boundary_bytes = boundary.canonical_geojson
    boundary_digest = boundary.geometry_digest
    feature_label = "geometry" if distribution_feature_count == 1 else "geometries"
    provenance = {
        "authority": "State of Berlin",
        "boundary_artifact_path": boundary_relative,
        "boundary_digest": boundary_digest,
        "contract": "campaign-geography-provenance",
        "contract_revision": "campaign-geography-provenance-v1",
        "dataset_identifier": dataset_identifier,
        "dataset_title": dataset_title,
        "derivation": (
            f"Deterministic union of {distribution_feature_count} official Berlin "
            f"administrative boundary {feature_label}."
        ),
        "distribution_feature_count": distribution_feature_count,
        "distribution_owner": distribution_owner,
        "distribution_url": source_url,
        "license_identifier": license_identifier,
        "license_title": license_title,
        "source_digest": source_digest,
    }
    provenance_bytes = _canonical_json(provenance)
    geography = {
        "schema_revision": "geography-config-v1",
        "geography_revision": _campaign_geography_revision(campaign_root),
        "boundary_artifact_path": boundary_relative,
        "boundary_digest": boundary_digest,
        "provenance_artifact_path": provenance_relative,
        "provenance_digest": _sha256(provenance_bytes),
    }

    boundary_path.parent.mkdir(parents=True, exist_ok=True)
    boundary_path.write_bytes(boundary_bytes)
    provenance_path.write_bytes(provenance_bytes)
    geography_path.write_text(
        yaml.safe_dump(
            geography,
            allow_unicode=True,
            sort_keys=False,
            width=100,
        ),
        encoding="utf-8",
    )
    modified = update_campaign(
        campaign_root,
        boundary_path=boundary_relative,
        digest=boundary_digest,
    )
    campaign_value = yaml.safe_load((campaign_root / "campaign.yaml").read_text(encoding="utf-8"))
    readiness = campaign_value.get("readiness") if isinstance(campaign_value, Mapping) else None
    blockers = readiness.get("blockers") if isinstance(readiness, Mapping) else None
    if isinstance(blockers, Sequence) and any(
        isinstance(blocker, Mapping) and blocker.get("code") == "BERLIN_BOUNDARY_ARTIFACT_MISSING"
        for blocker in blockers
    ):
        raise BoundaryMaterializationError(
            "campaign geography artifacts were written but the boundary blocker remains"
        )
    return boundary_path, provenance_path, modified


def materialize(
    repository_root: Path,
    *,
    timeout_seconds: float,
) -> tuple[Path, Path, tuple[Path, ...]]:
    with httpx.Client(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        headers={"User-Agent": "data-collection-platform-boundary-materializer/1"},
    ) as client:
        errors: list[str] = []
        selected: ResourceCandidate | None = None
        downloaded: MaterializedBoundary | None = None
        canonical: Mapping[str, Any] | None = None
        for candidate in discover_candidates(client):
            try:
                downloaded = download_candidate(client, candidate)
                canonical = canonicalize_boundary(downloaded.geojson)
            except (BoundaryMaterializationError, httpx.HTTPError) as exc:
                errors.append(
                    f"{candidate.dataset_identifier}/{candidate.resource_identifier}: "
                    f"{type(exc).__name__}"
                )
                continue
            selected = candidate
            break
    if selected is None or downloaded is None or canonical is None:
        raise BoundaryMaterializationError(
            "no official Berlin boundary candidate passed validation: " + "; ".join(errors)
        )
    return write_geography_revision(
        repository_root,
        canonical_boundary=canonical,
        source_digest=downloaded.source_digest,
        source_url=downloaded.source_url,
        dataset_title=selected.dataset_title,
        dataset_identifier=selected.dataset_identifier,
        distribution_owner="State of Berlin Open Data",
        distribution_feature_count=_distribution_feature_count(downloaded.geojson),
        license_identifier=selected.license_identifier,
        license_title=selected.license_title,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        boundary, provenance, modified = materialize(
            args.repository_root.resolve(),
            timeout_seconds=args.timeout_seconds,
        )
    except (BoundaryMaterializationError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"Berlin boundary materialization failed: {exc}", file=sys.stderr)
        return 1
    print(boundary)
    print(provenance)
    for path in modified:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
