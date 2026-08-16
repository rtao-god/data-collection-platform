from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from tools.live_collection.berlin_boundary import (
    BoundaryMaterializationError,
    MaterializedBoundary,
    ResourceCandidate,
    _canonical_json,
    _require_official_url,
    _sha256,
    canonicalize_boundary,
    download_candidate,
    update_campaign,
)

_SEARCH_URLS = (
    "https://daten.berlin.de/datensaetze?search=Landesgrenze",
    "https://daten.berlin.de/datensaetze?search=Verwaltungsgrenzen",
    "https://daten.berlin.de/datensaetze?search=Bezirksgrenzen",
)
_DATASET_PATH_SEGMENT = "/datensaetze/"
_RESOURCE_KEYS = frozenset(
    {
        "contenturl",
        "downloadurl",
        "accessurl",
        "url",
    }
)
_LICENSE_KEYS = frozenset({"license", "licence"})
_NAME_KEYS = frozenset({"name", "title", "headline"})


class _PortalParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.json_ld: list[str] = []
        self._json_depth = 0
        self._json_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs}
        if tag.lower() == "a" and values.get("href"):
            self.links.append(str(values["href"]))
        if tag.lower() == "script" and str(values.get("type", "")).lower() == "application/ld+json":
            self._json_depth = 1
            self._json_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._json_depth:
            self.json_ld.append("".join(self._json_buffer))
            self._json_depth = 0
            self._json_buffer = []

    def handle_data(self, data: str) -> None:
        if self._json_depth:
            self._json_buffer.append(data)


def _verify_official_request(request: httpx.Request) -> None:
    _require_official_url(str(request.url))


def _walk(value: object) -> Iterable[object]:
    yield value
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _walk(item)


def _text_for_keys(value: Mapping[str, object], keys: frozenset[str]) -> str:
    for key, item in value.items():
        if str(key).lower() not in keys:
            continue
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, Mapping):
            nested = _text_for_keys(item, frozenset({"name", "url", "identifier"}))
            if nested:
                return nested
    return ""


def _resource_urls(value: Mapping[str, object]) -> tuple[str, ...]:
    result: list[str] = []
    for key, item in value.items():
        if str(key).lower() not in _RESOURCE_KEYS:
            continue
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            result.extend(str(entry) for entry in item if isinstance(entry, str))
    return tuple(result)


def _score_metadata(value: Mapping[str, object], url: str) -> int:
    text = " ".join(
        (
            _text_for_keys(value, _NAME_KEYS),
            str(value.get("description", "")),
            url,
        )
    ).lower()
    score = 0
    for token, weight in (
        ("landesgrenze", 160),
        ("verwaltungsgrenze", 130),
        ("bezirksgrenze", 100),
        ("geojson", 80),
        ("wfs", 60),
    ):
        if token in text:
            score += weight
    return score


def _metadata_candidates(dataset_url: str, html: str) -> tuple[ResourceCandidate, ...]:
    parser = _PortalParser()
    parser.feed(html)
    result: dict[str, ResourceCandidate] = {}
    for raw in parser.json_ld:
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        for node in _walk(value):
            if not isinstance(node, Mapping):
                continue
            license_value = _text_for_keys(node, _LICENSE_KEYS)
            if not license_value:
                for parent in _walk(value):
                    if isinstance(parent, Mapping):
                        license_value = _text_for_keys(parent, _LICENSE_KEYS)
                        if license_value:
                            break
            for resource_url in _resource_urls(node):
                resource_url = urljoin(dataset_url, resource_url)
                try:
                    _require_official_url(resource_url)
                except BoundaryMaterializationError:
                    continue
                score = _score_metadata(node, resource_url)
                if score <= 0 or not license_value:
                    continue
                identifier = hashlib.sha256(resource_url.encode("utf-8")).hexdigest()
                result[resource_url] = ResourceCandidate(
                    dataset_title=_text_for_keys(node, _NAME_KEYS) or "Berlin boundary",
                    dataset_identifier=hashlib.sha256(dataset_url.encode("utf-8")).hexdigest(),
                    resource_identifier=identifier,
                    resource_url=resource_url,
                    resource_format=(
                        "wfs"
                        if "wfs" in resource_url.lower()
                        else Path(urlparse(resource_url).path).suffix.lstrip(".").lower()
                    ),
                    license_identifier=license_value,
                    license_title=license_value,
                    score=score,
                )
    return tuple(sorted(result.values(), key=lambda item: (-item.score, item.resource_url)))


def discover_portal_candidates(client: httpx.Client) -> tuple[ResourceCandidate, ...]:
    dataset_urls: set[str] = set()
    failures: list[str] = []
    for search_url in _SEARCH_URLS:
        try:
            response = client.get(search_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            failures.append(f"{search_url}: {type(exc).__name__}")
            continue
        parser = _PortalParser()
        parser.feed(response.text)
        for link in parser.links:
            url = urljoin(search_url, link)
            parsed = urlparse(url)
            if _DATASET_PATH_SEGMENT not in parsed.path:
                continue
            try:
                _require_official_url(url)
            except BoundaryMaterializationError:
                continue
            dataset_urls.add(url)
    candidates: dict[str, ResourceCandidate] = {}
    for dataset_url in sorted(dataset_urls):
        try:
            response = client.get(dataset_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            failures.append(f"{dataset_url}: {type(exc).__name__}")
            continue
        for candidate in _metadata_candidates(dataset_url, response.text):
            candidates[candidate.resource_url] = candidate
    if not candidates:
        detail = "; ".join(failures) or "no dataset metadata links"
        raise BoundaryMaterializationError(
            "Berlin Open Data portal yielded no licensed official boundary resource: " + detail
        )
    return tuple(sorted(candidates.values(), key=lambda item: (-item.score, item.resource_url)))


def materialize_from_portal(
    repository_root: Path,
    *,
    timeout_seconds: float,
) -> tuple[Path, Path, tuple[Path, ...]]:
    campaign_root = repository_root / "campaigns/berlin_recording_services"
    boundary_path = campaign_root / "geography/berlin-boundary.geojson"
    provenance_path = campaign_root / "geography/berlin-boundary.provenance.json"
    selected: ResourceCandidate | None = None
    downloaded: MaterializedBoundary | None = None
    canonical: Mapping[str, Any] | None = None
    errors: list[str] = []
    with httpx.Client(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=True,
        headers={"User-Agent": "data-collection-platform-portal-boundary/1"},
        event_hooks={"request": [_verify_official_request]},
    ) as client:
        for candidate in discover_portal_candidates(client):
            try:
                downloaded = download_candidate(client, candidate)
                canonical = canonicalize_boundary(downloaded.geojson)
            except (BoundaryMaterializationError, httpx.HTTPError) as exc:
                errors.append(f"{candidate.resource_identifier}: {type(exc).__name__}")
                continue
            selected = candidate
            break
    if selected is None or downloaded is None or canonical is None:
        raise BoundaryMaterializationError(
            "no Berlin portal boundary resource passed validation: " + "; ".join(errors)
        )
    boundary_bytes = _canonical_json(canonical)
    boundary_digest = _sha256(boundary_bytes)
    relative = boundary_path.relative_to(repository_root).as_posix()
    provenance = {
        "contract": "berlin-boundary-provenance",
        "contractRevision": "1",
        "authority": "State of Berlin",
        "discoveryMethod": "Berlin Open Data portal JSON-LD",
        "datasetTitle": selected.dataset_title,
        "datasetIdentifier": selected.dataset_identifier,
        "resourceIdentifier": selected.resource_identifier,
        "resourceUrl": downloaded.source_url,
        "resourceFormat": selected.resource_format,
        "featureType": downloaded.feature_type,
        "licenseIdentifier": selected.license_identifier,
        "licenseTitle": selected.license_title,
        "sourceDigest": downloaded.source_digest,
        "boundaryDigest": boundary_digest,
        "boundaryPath": relative,
    }
    boundary_path.parent.mkdir(parents=True, exist_ok=True)
    boundary_path.write_bytes(boundary_bytes)
    provenance_path.write_bytes(_canonical_json(provenance))
    modified = update_campaign(
        campaign_root,
        boundary_path=relative,
        digest=boundary_digest,
    )
    return boundary_path, provenance_path, modified


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        boundary, provenance, modified = materialize_from_portal(
            args.repository_root.resolve(),
            timeout_seconds=args.timeout_seconds,
        )
    except (BoundaryMaterializationError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"Berlin portal boundary materialization failed: {exc}", file=sys.stderr)
        return 1
    print(boundary)
    print(provenance)
    for path in modified:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
