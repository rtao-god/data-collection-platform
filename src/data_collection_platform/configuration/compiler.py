"""Strict campaign configuration validation and deterministic bundle compilation."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import NoReturn, cast
from urllib.parse import urlsplit

from data_collection_platform.shared.contracts import (
    ContractViolation,
    JsonValue,
    canonical_json_bytes,
    require_non_empty_text,
    sha256_hex,
)

_CAMPAIGN_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_ALLOWED_ENTITY_KINDS = frozenset({"place", "provider"})
_ALLOWED_SOURCE_TYPES = frozenset({"manual", "osm", "http", "browser"})
_ALLOWED_SOURCE_STATUSES = frozenset({"approved", "disabled"})
_ALLOWED_ROBOTS_POLICIES = frozenset({"respect", "not_applicable", "blocked"})


class CampaignConfigurationViolation(ContractViolation):
    """A configuration artifact violates the repository-owned campaign contract."""


@dataclass(frozen=True, slots=True)
class CampaignDefinition:
    schema_version: int
    campaign_id: str
    display_name: str
    entity_kind: str
    geography_file: str
    source_policy_files: tuple[str, ...]
    seeds_file: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "display_name": self.display_name,
            "entity_kind": self.entity_kind,
            "geography_file": self.geography_file,
            "source_policy_files": list(self.source_policy_files),
            "seeds_file": self.seeds_file,
        }


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    schema_version: int
    source_id: str
    source_type: str
    status: str
    allowed_hosts: tuple[str, ...]
    request_budget: int
    requests_per_minute: int
    max_concurrency: int
    terms_reviewed_at: str
    robots_policy: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "status": self.status,
            "allowed_hosts": list(self.allowed_hosts),
            "request_budget": self.request_budget,
            "requests_per_minute": self.requests_per_minute,
            "max_concurrency": self.max_concurrency,
            "terms_reviewed_at": self.terms_reviewed_at,
            "robots_policy": self.robots_policy,
        }


@dataclass(frozen=True, slots=True)
class Seed:
    expected_entity_kind: str
    display_name: str
    website: str | None
    osm_id: str | None
    reference_urls: tuple[str, ...]
    note: str | None
    provenance: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "expected_entity_kind": self.expected_entity_kind,
            "display_name": self.display_name,
            "website": self.website,
            "osm_id": self.osm_id,
            "reference_urls": list(self.reference_urls),
            "note": self.note,
            "provenance": self.provenance,
        }


@dataclass(frozen=True, slots=True)
class SourceManifestEntry:
    path: str
    size_bytes: int
    sha256: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class CompiledCampaignBundle:
    campaign_id: str
    bundle_sha256: str
    document: dict[str, JsonValue]

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.document)

    def write_atomic(self, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_json_bytes()
        temporary_name: str | None = None
        try:
            with NamedTemporaryFile(
                mode="wb",
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, output_path)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)


def compile_campaign_directory(root: Path) -> CompiledCampaignBundle:
    """Compile one campaign directory without mutating the source tree."""

    root_path = _require_directory(root)
    campaign_path = _resolve_owned_file(root_path, "campaign.json")
    campaign_raw = _read_json_object(campaign_path)
    campaign = _parse_campaign(campaign_raw, path="campaign.json")

    geography_path = _resolve_owned_file(root_path, campaign.geography_file)
    geography = _read_json_object(geography_path)
    _validate_geography(geography, path=campaign.geography_file)

    source_policies: list[SourcePolicy] = []
    source_ids: set[str] = set()
    owned_paths = ["campaign.json", campaign.geography_file, campaign.seeds_file]
    for policy_file in campaign.source_policy_files:
        policy_path = _resolve_owned_file(root_path, policy_file)
        policy = _parse_source_policy(
            _read_json_object(policy_path),
            path=policy_file,
        )
        if policy.source_id in source_ids:
            _raise(
                code="campaign.duplicate_source_id",
                message="A campaign must not define the same source owner twice.",
                path=policy_file,
                context={"source_id": policy.source_id},
            )
        if policy.status != "approved":
            _raise(
                code="campaign.source_not_approved",
                message="A referenced source policy must be explicitly approved.",
                path=policy_file,
                context={"source_id": policy.source_id, "status": policy.status},
            )
        if policy.robots_policy == "blocked":
            _raise(
                code="campaign.source_blocked_by_robots_policy",
                message="A blocked source must not enter an executable campaign bundle.",
                path=policy_file,
                context={"source_id": policy.source_id},
            )
        source_ids.add(policy.source_id)
        source_policies.append(policy)
        owned_paths.append(policy_file)

    seeds_path = _resolve_owned_file(root_path, campaign.seeds_file)
    seeds = _read_seeds(seeds_path, expected_entity_kind=campaign.entity_kind)

    manifest = tuple(
        _manifest_entry(root_path, relative_path)
        for relative_path in sorted(set(owned_paths))
    )
    payload: dict[str, JsonValue] = {
        "bundle_schema_version": 1,
        "campaign": campaign.as_json(),
        "geography": cast(JsonValue, geography),
        "source_policies": [
            policy.as_json() for policy in sorted(source_policies, key=lambda item: item.source_id)
        ],
        "seeds": [seed.as_json() for seed in seeds],
        "source_manifest": [entry.as_json() for entry in manifest],
    }
    bundle_digest = sha256_hex(canonical_json_bytes(payload))
    document = dict(payload)
    document["bundle_sha256"] = bundle_digest
    return CompiledCampaignBundle(
        campaign_id=campaign.campaign_id,
        bundle_sha256=bundle_digest,
        document=document,
    )


def _parse_campaign(value: dict[str, object], *, path: str) -> CampaignDefinition:
    _require_exact_keys(
        value,
        required={
            "schema_version",
            "campaign_id",
            "display_name",
            "entity_kind",
            "geography_file",
            "source_policy_files",
            "seeds_file",
        },
        path=path,
    )
    schema_version = _require_integer(value["schema_version"], field="schema_version", path=path)
    if schema_version != 1:
        _raise(
            code="campaign.unsupported_schema_version",
            message="Campaign schema_version is not supported.",
            path=path,
            context={"schema_version": schema_version},
        )
    campaign_id = _require_text(value["campaign_id"], field="campaign_id", path=path)
    if _CAMPAIGN_ID.fullmatch(campaign_id) is None:
        _raise(
            code="campaign.invalid_id",
            message="campaign_id must be a lowercase kebab-case identifier.",
            path=path,
            context={"campaign_id": campaign_id},
        )
    entity_kind = _require_text(value["entity_kind"], field="entity_kind", path=path)
    if entity_kind not in _ALLOWED_ENTITY_KINDS:
        _raise(
            code="campaign.invalid_entity_kind",
            message="entity_kind must name a supported canonical entity kind.",
            path=path,
            context={"entity_kind": entity_kind},
        )
    source_policy_files = tuple(
        _require_relative_path(item, field="source_policy_files", path=path)
        for item in _require_list(value["source_policy_files"], field="source_policy_files", path=path)
    )
    if not source_policy_files:
        _raise(
            code="campaign.missing_source_policy",
            message="A campaign must reference at least one approved source policy.",
            path=path,
        )
    if len(set(source_policy_files)) != len(source_policy_files):
        _raise(
            code="campaign.duplicate_source_policy_file",
            message="A source policy file must not be referenced more than once.",
            path=path,
        )
    return CampaignDefinition(
        schema_version=schema_version,
        campaign_id=campaign_id,
        display_name=_require_text(value["display_name"], field="display_name", path=path),
        entity_kind=entity_kind,
        geography_file=_require_relative_path(
            value["geography_file"],
            field="geography_file",
            path=path,
        ),
        source_policy_files=source_policy_files,
        seeds_file=_require_relative_path(value["seeds_file"], field="seeds_file", path=path),
    )


def _parse_source_policy(value: dict[str, object], *, path: str) -> SourcePolicy:
    _require_exact_keys(
        value,
        required={
            "schema_version",
            "source_id",
            "source_type",
            "status",
            "allowed_hosts",
            "request_budget",
            "requests_per_minute",
            "max_concurrency",
            "terms_reviewed_at",
            "robots_policy",
        },
        path=path,
    )
    schema_version = _require_integer(value["schema_version"], field="schema_version", path=path)
    if schema_version != 1:
        _raise(
            code="source_policy.unsupported_schema_version",
            message="Source policy schema_version is not supported.",
            path=path,
            context={"schema_version": schema_version},
        )
    source_id = _require_text(value["source_id"], field="source_id", path=path)
    if _SOURCE_ID.fullmatch(source_id) is None:
        _raise(
            code="source_policy.invalid_id",
            message="source_id contains unsupported characters.",
            path=path,
            context={"source_id": source_id},
        )
    source_type = _require_member(
        value["source_type"],
        field="source_type",
        allowed=_ALLOWED_SOURCE_TYPES,
        path=path,
    )
    status = _require_member(
        value["status"],
        field="status",
        allowed=_ALLOWED_SOURCE_STATUSES,
        path=path,
    )
    allowed_hosts = tuple(
        _require_host(item, field="allowed_hosts", path=path)
        for item in _require_list(value["allowed_hosts"], field="allowed_hosts", path=path)
    )
    if source_type != "manual" and not allowed_hosts:
        _raise(
            code="source_policy.missing_allowed_host",
            message="A network source policy must allow at least one exact host.",
            path=path,
            context={"source_id": source_id},
        )
    if len(set(allowed_hosts)) != len(allowed_hosts):
        _raise(
            code="source_policy.duplicate_allowed_host",
            message="allowed_hosts must not contain duplicates.",
            path=path,
            context={"source_id": source_id},
        )
    request_budget = _require_positive_integer(
        value["request_budget"],
        field="request_budget",
        path=path,
    )
    requests_per_minute = _require_positive_integer(
        value["requests_per_minute"],
        field="requests_per_minute",
        path=path,
    )
    max_concurrency = _require_positive_integer(
        value["max_concurrency"],
        field="max_concurrency",
        path=path,
    )
    if max_concurrency > request_budget:
        _raise(
            code="source_policy.concurrency_exceeds_budget",
            message="max_concurrency must not exceed the complete request budget.",
            path=path,
            context={
                "max_concurrency": max_concurrency,
                "request_budget": request_budget,
            },
        )
    terms_reviewed_at = _require_iso_date(
        value["terms_reviewed_at"],
        field="terms_reviewed_at",
        path=path,
    )
    robots_policy = _require_member(
        value["robots_policy"],
        field="robots_policy",
        allowed=_ALLOWED_ROBOTS_POLICIES,
        path=path,
    )
    return SourcePolicy(
        schema_version=schema_version,
        source_id=source_id,
        source_type=source_type,
        status=status,
        allowed_hosts=allowed_hosts,
        request_budget=request_budget,
        requests_per_minute=requests_per_minute,
        max_concurrency=max_concurrency,
        terms_reviewed_at=terms_reviewed_at,
        robots_policy=robots_policy,
    )


def _read_seeds(path: Path, *, expected_entity_kind: str) -> tuple[Seed, ...]:
    seeds: list[Seed] = []
    seen_identities: set[tuple[str | None, str | None, tuple[str, ...]]] = set()
    with path.open("r", encoding="utf-8", newline="") as source:
        for line_number, raw_line in enumerate(source, start=1):
            line = raw_line.strip()
            if not line:
                _raise(
                    code="seed.blank_line",
                    message="Seed NDJSON must not contain blank records.",
                    path=f"{path.name}:{line_number}",
                )
            parsed = _parse_json(line, path=f"{path.name}:{line_number}")
            value = _require_object(parsed, path=f"{path.name}:{line_number}")
            seed = _parse_seed(
                value,
                path=f"{path.name}:{line_number}",
                expected_entity_kind=expected_entity_kind,
            )
            identity = (seed.website, seed.osm_id, seed.reference_urls)
            if identity in seen_identities:
                _raise(
                    code="seed.duplicate_identity",
                    message="A campaign seed identity must not be repeated.",
                    path=f"{path.name}:{line_number}",
                )
            seen_identities.add(identity)
            seeds.append(seed)
    if not seeds:
        _raise(
            code="seed.empty_file",
            message="A campaign seed file must contain at least one evidence-bearing seed.",
            path=path.name,
        )
    return tuple(seeds)


def _parse_seed(
    value: dict[str, object],
    *,
    path: str,
    expected_entity_kind: str,
) -> Seed:
    _require_exact_keys(
        value,
        required={
            "expected_entity_kind",
            "display_name",
            "website",
            "osm_id",
            "reference_urls",
            "note",
            "provenance",
        },
        path=path,
    )
    entity_kind = _require_text(
        value["expected_entity_kind"],
        field="expected_entity_kind",
        path=path,
    )
    if entity_kind != expected_entity_kind:
        _raise(
            code="seed.entity_kind_mismatch",
            message="A seed entity kind must match its campaign contract.",
            path=path,
            context={"expected": expected_entity_kind, "actual": entity_kind},
        )
    website = _require_optional_url(value["website"], field="website", path=path)
    osm_id = _require_optional_text(value["osm_id"], field="osm_id", path=path)
    reference_urls = tuple(
        _require_url(item, field="reference_urls", path=path)
        for item in _require_list(value["reference_urls"], field="reference_urls", path=path)
    )
    if website is None and osm_id is None and not reference_urls:
        _raise(
            code="seed.missing_reference",
            message="A seed requires a website, OSM id, or reference URL.",
            path=path,
        )
    note = _require_optional_text(value["note"], field="note", path=path)
    return Seed(
        expected_entity_kind=entity_kind,
        display_name=_require_text(value["display_name"], field="display_name", path=path),
        website=website,
        osm_id=osm_id,
        reference_urls=reference_urls,
        note=note,
        provenance=_require_text(value["provenance"], field="provenance", path=path),
    )


def _validate_geography(value: dict[str, object], *, path: str) -> None:
    _require_exact_keys(value, required={"type", "properties", "geometry"}, path=path)
    if _require_text(value["type"], field="type", path=path) != "Feature":
        _raise(
            code="geography.invalid_feature_type",
            message="Campaign geography must be one GeoJSON Feature.",
            path=path,
        )
    properties = _require_object(value["properties"], path=f"{path}.properties")
    _require_exact_keys(
        properties,
        required={"name", "source", "license", "observed_at"},
        path=f"{path}.properties",
    )
    _require_text(properties["name"], field="name", path=f"{path}.properties")
    _require_text(properties["source"], field="source", path=f"{path}.properties")
    _require_text(properties["license"], field="license", path=f"{path}.properties")
    _require_iso_date(properties["observed_at"], field="observed_at", path=f"{path}.properties")

    geometry = _require_object(value["geometry"], path=f"{path}.geometry")
    _require_exact_keys(geometry, required={"type", "coordinates"}, path=f"{path}.geometry")
    geometry_type = _require_text(geometry["type"], field="type", path=f"{path}.geometry")
    coordinates = _require_list(
        geometry["coordinates"],
        field="coordinates",
        path=f"{path}.geometry",
    )
    if geometry_type == "Polygon":
        _validate_polygon(coordinates, path=f"{path}.geometry.coordinates")
    elif geometry_type == "MultiPolygon":
        if not coordinates:
            _raise(
                code="geography.empty_multi_polygon",
                message="A MultiPolygon must contain at least one polygon.",
                path=path,
            )
        for index, polygon in enumerate(coordinates):
            _validate_polygon(
                _require_list(
                    polygon,
                    field="polygon",
                    path=f"{path}.geometry.coordinates[{index}]",
                ),
                path=f"{path}.geometry.coordinates[{index}]",
            )
    else:
        _raise(
            code="geography.unsupported_geometry",
            message="Campaign geography must use Polygon or MultiPolygon.",
            path=path,
            context={"geometry_type": geometry_type},
        )


def _validate_polygon(value: list[object], *, path: str) -> None:
    if not value:
        _raise(
            code="geography.empty_polygon",
            message="A Polygon must contain at least one linear ring.",
            path=path,
        )
    for ring_index, ring_value in enumerate(value):
        ring = _require_list(
            ring_value,
            field="ring",
            path=f"{path}[{ring_index}]",
        )
        if len(ring) < 4:
            _raise(
                code="geography.short_ring",
                message="A linear ring must contain at least four positions.",
                path=f"{path}[{ring_index}]",
            )
        positions = tuple(
            _validate_position(position, path=f"{path}[{ring_index}][{position_index}]")
            for position_index, position in enumerate(ring)
        )
        if positions[0] != positions[-1]:
            _raise(
                code="geography.open_ring",
                message="A linear ring must repeat its first position as its last position.",
                path=f"{path}[{ring_index}]",
            )


def _validate_position(value: object, *, path: str) -> tuple[float, float]:
    position = _require_list(value, field="position", path=path)
    if len(position) != 2:
        _raise(
            code="geography.invalid_position_arity",
            message="A position must contain exactly longitude and latitude.",
            path=path,
        )
    longitude = _require_finite_number(position[0], field="longitude", path=path)
    latitude = _require_finite_number(position[1], field="latitude", path=path)
    if not -180 <= longitude <= 180:
        _raise(
            code="geography.invalid_longitude",
            message="Longitude must be within [-180, 180].",
            path=path,
            context={"longitude": longitude},
        )
    if not -90 <= latitude <= 90:
        _raise(
            code="geography.invalid_latitude",
            message="Latitude must be within [-90, 90].",
            path=path,
            context={"latitude": latitude},
        )
    return longitude, latitude


def _manifest_entry(root: Path, relative_path: str) -> SourceManifestEntry:
    source_path = _resolve_owned_file(root, relative_path)
    payload = source_path.read_bytes()
    return SourceManifestEntry(
        path=relative_path,
        size_bytes=len(payload),
        sha256=sha256_hex(payload),
    )


def _require_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise CampaignConfigurationViolation(
            code="campaign.root_not_found",
            message="Campaign directory does not exist.",
            context={"path": str(path)},
        ) from error
    if not resolved.is_dir():
        _raise(
            code="campaign.root_not_directory",
            message="Campaign root must be a directory.",
            path=str(path),
        )
    return resolved


def _resolve_owned_file(root: Path, relative_path: str) -> Path:
    normalized = _require_relative_path(relative_path, field="path", path=relative_path)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    if candidate.is_symlink():
        _raise(
            code="campaign.symlink_forbidden",
            message="Campaign source artifacts must not be symbolic links.",
            path=relative_path,
        )
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise CampaignConfigurationViolation(
            code="campaign.file_not_found",
            message="A referenced campaign source artifact does not exist.",
            context={"path": relative_path},
        ) from error
    if not resolved.is_relative_to(root):
        _raise(
            code="campaign.path_escape",
            message="A campaign source artifact must remain inside its campaign root.",
            path=relative_path,
        )
    if not resolved.is_file():
        _raise(
            code="campaign.path_not_file",
            message="A referenced campaign artifact must be a regular file.",
            path=relative_path,
        )
    return resolved


def _read_json_object(path: Path) -> dict[str, object]:
    parsed = _parse_json(path.read_text(encoding="utf-8"), path=path.name)
    return _require_object(parsed, path=path.name)


def _parse_json(value: str, *, path: str) -> object:
    try:
        return cast(object, json.loads(value, parse_constant=_reject_non_finite_json))
    except (json.JSONDecodeError, CampaignConfigurationViolation) as error:
        if isinstance(error, CampaignConfigurationViolation):
            raise
        raise CampaignConfigurationViolation(
            code="campaign.invalid_json",
            message="Campaign source artifact is not valid JSON.",
            context={"path": path, "line": error.lineno, "column": error.colno},
        ) from error


def _reject_non_finite_json(token: str) -> NoReturn:
    _raise(
        code="campaign.non_finite_number",
        message="JSON artifacts must not contain NaN or infinite numbers.",
        path="json",
        context={"token": token},
    )


def _require_object(value: object, *, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _raise(
            code="campaign.expected_object",
            message="A JSON object is required.",
            path=path,
        )
    return cast(dict[str, object], value)


def _require_list(value: object, *, field: str, path: str) -> list[object]:
    if not isinstance(value, list):
        _raise(
            code="campaign.expected_array",
            message="A JSON array is required.",
            path=path,
            context={"field": field},
        )
    return cast(list[object], value)


def _require_exact_keys(value: dict[str, object], *, required: set[str], path: str) -> None:
    actual = set(value)
    missing = sorted(required - actual)
    unknown = sorted(actual - required)
    if missing or unknown:
        _raise(
            code="campaign.object_shape_mismatch",
            message="Configuration objects must contain exactly the owned contract fields.",
            path=path,
            context={"missing": missing, "unknown": unknown},
        )


def _require_text(value: object, *, field: str, path: str) -> str:
    if not isinstance(value, str):
        _raise(
            code="campaign.expected_text",
            message="A required configuration field must be text.",
            path=path,
            context={"field": field},
        )
    try:
        return require_non_empty_text(value, field_name=field)
    except ContractViolation as error:
        raise CampaignConfigurationViolation(
            code=error.code,
            message=error.message,
            context={"path": path, **error.context},
        ) from error


def _require_optional_text(value: object, *, field: str, path: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field=field, path=path)


def _require_integer(value: object, *, field: str, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _raise(
            code="campaign.expected_integer",
            message="A required configuration field must be an integer.",
            path=path,
            context={"field": field},
        )
    return value


def _require_positive_integer(value: object, *, field: str, path: str) -> int:
    parsed = _require_integer(value, field=field, path=path)
    if parsed < 1:
        _raise(
            code="campaign.expected_positive_integer",
            message="A required configuration field must be greater than zero.",
            path=path,
            context={"field": field, "value": parsed},
        )
    return parsed


def _require_finite_number(value: object, *, field: str, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _raise(
            code="campaign.expected_number",
            message="A coordinate must be numeric.",
            path=path,
            context={"field": field},
        )
    parsed = float(value)
    if not math.isfinite(parsed):
        _raise(
            code="campaign.non_finite_number",
            message="A coordinate must be finite.",
            path=path,
            context={"field": field, "value": value},
        )
    return parsed


def _require_member(
    value: object,
    *,
    field: str,
    allowed: frozenset[str],
    path: str,
) -> str:
    parsed = _require_text(value, field=field, path=path)
    if parsed not in allowed:
        _raise(
            code="campaign.unsupported_value",
            message="A configuration field contains an unsupported value.",
            path=path,
            context={"field": field, "value": parsed, "allowed": sorted(allowed)},
        )
    return parsed


def _require_relative_path(value: object, *, field: str, path: str) -> str:
    parsed = _require_text(value, field=field, path=path)
    if "\\" in parsed:
        _raise(
            code="campaign.non_portable_path",
            message="Campaign paths must use forward slashes.",
            path=path,
            context={"field": field, "value": parsed},
        )
    candidate = PurePosixPath(parsed)
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        _raise(
            code="campaign.invalid_relative_path",
            message="Campaign paths must be normalized relative paths without traversal.",
            path=path,
            context={"field": field, "value": parsed},
        )
    return candidate.as_posix()


def _require_host(value: object, *, field: str, path: str) -> str:
    host = _require_text(value, field=field, path=path).lower().rstrip(".")
    if "://" in host or "/" in host or ":" in host or host.startswith("."):
        _raise(
            code="source_policy.invalid_host",
            message="allowed_hosts entries must be exact DNS hostnames without scheme, port, or path.",
            path=path,
            context={"host": host},
        )
    labels = host.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or re.fullmatch(r"[a-z0-9-]+", label) is None
        for label in labels
    ):
        _raise(
            code="source_policy.invalid_host",
            message="allowed_hosts entries must be valid normalized DNS hostnames.",
            path=path,
            context={"host": host},
        )
    return host


def _require_url(value: object, *, field: str, path: str) -> str:
    url = _require_text(value, field=field, path=path)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        _raise(
            code="seed.invalid_url",
            message="Seed URLs must be absolute HTTP(S) URLs without user information.",
            path=path,
            context={"field": field, "value": url},
        )
    if parsed.fragment:
        _raise(
            code="seed.url_fragment_forbidden",
            message="Seed URLs must not contain fragments.",
            path=path,
            context={"field": field, "value": url},
        )
    return url


def _require_optional_url(value: object, *, field: str, path: str) -> str | None:
    if value is None:
        return None
    return _require_url(value, field=field, path=path)


def _require_iso_date(value: object, *, field: str, path: str) -> str:
    parsed = _require_text(value, field=field, path=path)
    try:
        date.fromisoformat(parsed)
    except ValueError as error:
        raise CampaignConfigurationViolation(
            code="campaign.invalid_date",
            message="A configuration date must use ISO YYYY-MM-DD format.",
            context={"path": path, "field": field, "value": parsed},
        ) from error
    return parsed


def _raise(
    *,
    code: str,
    message: str,
    path: str,
    context: dict[str, object] | None = None,
) -> NoReturn:
    raise CampaignConfigurationViolation(
        code=code,
        message=message,
        context={"path": path, **(context or {})},
    )
