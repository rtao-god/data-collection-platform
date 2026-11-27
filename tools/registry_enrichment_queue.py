"""Validate durable studio-enrichment observations and refresh the review projection.

The canonical studio registry remains authoritative. This tool never edits it, approves facts,
or consumes pending observations; accepted facts continue through studio_registry patches.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    from tools import registry_catalog_manifest as manifest
    from tools import studio_registry as registry
except ImportError:  # pragma: no cover - direct script execution
    import registry_catalog_manifest as manifest
    import studio_registry as registry

DEFAULT_DISCOVERY = Path("campaigns/berlin_recording_services/discovery")
DEFAULT_REGISTRY = DEFAULT_DISCOVERY / "studios.json"
DEFAULT_INCOMING = DEFAULT_DISCOVERY / "studios-incoming.json"
DEFAULT_CONFLICTS = DEFAULT_DISCOVERY / "studios-conflicts.json"
INCOMING_SCHEMA = "studio-enrichment-incoming-v1"
CONFLICTS_SCHEMA = "studio-enrichment-conflicts-v1"
OBSERVATION_FIELD_PREFIXES = (*registry.PATCHABLE_PREFIXES, "photo_candidates")


class QueueError(ValueError):
    """The durable work queue is malformed or stale and cannot be projected safely."""


def _validate_photo_candidate_value(value: Any, path: str) -> None:
    problems: list[registry.Problem] = []
    registry.validate_photos(problems, [value], f"{path}.normalized_value")
    if problems:
        raise QueueError("; ".join(problem.render() for problem in problems))
    if not isinstance(value, Mapping):
        raise QueueError(f"{path}.normalized_value must be an object")
    if value.get("rights_state") != "candidate_unreviewed":
        raise QueueError(f"{path}.normalized_value.rights_state must remain candidate_unreviewed")
    if registry.parse_utc(value.get("observed_at_utc")) is None:
        raise QueueError(f"{path}.normalized_value.observed_at_utc is invalid")
    for key in ("rights_holder", "license_url", "license_version", "omission_reason"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise QueueError(f"{path}.normalized_value.{key} must be a string or null")
    if value.get("license_url") and not registry.is_http_url(value["license_url"]):
        raise QueueError(f"{path}.normalized_value.license_url must be an HTTP URL")


def _validate_photo_metadata(metadata: Any, path: str) -> None:
    if not isinstance(metadata, Mapping) or not registry.is_http_url(
        metadata.get("source_page_url")
    ):
        raise QueueError(f"{path}.photo_metadata.source_page_url must be an HTTP URL")
    for key in ("source_title", "image_description"):
        if not isinstance(metadata.get(key), str) or not metadata[key]:
            raise QueueError(f"{path}.photo_metadata.{key} is required")
    for key in ("image_taken_at", "image_metadata_recorded_at", "credit", "mime"):
        if metadata.get(key) is not None and not isinstance(metadata[key], str):
            raise QueueError(f"{path}.photo_metadata.{key} must be a string or null")
    profile_url = metadata.get("rights_holder_profile_url")
    if profile_url is not None and not registry.is_http_url(profile_url):
        raise QueueError(f"{path}.photo_metadata.rights_holder_profile_url must be an HTTP URL")
    attribution = metadata.get("attribution_required")
    if attribution is not None and not isinstance(attribution, bool):
        raise QueueError(f"{path}.photo_metadata.attribution_required must be a boolean or null")
    for key in ("size_bytes", "width", "height"):
        number = metadata.get(key)
        if number is not None and (
            not isinstance(number, int) or isinstance(number, bool) or number <= 0
        ):
            raise QueueError(f"{path}.photo_metadata.{key} must be a positive integer or null")


def _validate_photo_provenance(item: Mapping[str, Any], path: str) -> None:
    provenance = item.get("source_provenance")
    if not isinstance(provenance, list) or not provenance:
        raise QueueError(f"{path}.source_provenance must preserve at least one acquisition")
    seen_run_ids: set[str] = set()
    for entry in provenance:
        if not isinstance(entry, Mapping):
            raise QueueError(f"{path}.source_provenance entries must be objects")
        run_id = entry.get("source_run_id")
        if not isinstance(run_id, str) or not run_id or run_id in seen_run_ids:
            raise QueueError(f"{path}.source_provenance source_run_id is missing or duplicated")
        seen_run_ids.add(run_id)
        if not isinstance(entry.get("source_key"), str) or not entry["source_key"]:
            raise QueueError(f"{path}.source_provenance.source_key is required")
        if entry.get("source_record_id") != item["source"].get("source_record_id"):
            raise QueueError(f"{path}.source_provenance source_record_id changed")
        if not registry.is_http_url(entry.get("source_url")):
            raise QueueError(f"{path}.source_provenance.source_url must be an HTTP URL")
        if registry.parse_utc(entry.get("acquired_at_utc")) is None:
            raise QueueError(f"{path}.source_provenance.acquired_at_utc is invalid")
        for key in ("response_digest_sha256", "candidate_evidence_digest_sha256"):
            digest = entry.get(key)
            if not isinstance(digest, str) or registry.HEX64_RE.fullmatch(digest) is None:
                raise QueueError(f"{path}.source_provenance.{key} is invalid")


def _validate_photo_bytes(item: Mapping[str, Any], path: str) -> None:
    downloaded = item.get("image_bytes_downloaded")
    image_digest = item.get("image_bytes_sha256")
    if not isinstance(downloaded, bool):
        raise QueueError(f"{path}.image_bytes_downloaded must be a boolean")
    if downloaded and (
        not isinstance(image_digest, str) or registry.HEX64_RE.fullmatch(image_digest) is None
    ):
        raise QueueError(f"{path}.image_bytes_sha256 is required for downloaded image bytes")
    if not downloaded and image_digest is not None:
        raise QueueError(
            f"{path}.image_bytes_sha256 must be null when image bytes were not downloaded"
        )


def _validate_photo_observation(item: Mapping[str, Any], path: str) -> None:
    if item.get("operation") != "add_collection_item":
        raise QueueError(f"{path}.operation must be add_collection_item for photo candidates")
    _validate_photo_candidate_value(item.get("normalized_value"), path)
    _validate_photo_metadata(item.get("photo_metadata"), path)
    _validate_photo_provenance(item, path)
    _validate_photo_bytes(item, path)


def _digest(value: Any) -> str:
    return manifest.digest_of(value)


def _load(path: Path) -> Any:
    try:
        return registry.read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise QueueError(f"cannot read JSON {path}: {exc}") from exc


def _index_records(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    records = document.get("records")
    if not isinstance(records, list):
        raise QueueError("canonical registry.records must be an array")
    indexed: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise QueueError(f"canonical registry.records[{index}] must be an object")
        registry_id = record.get("registry_id")
        if not isinstance(registry_id, str) or registry_id in indexed:
            raise QueueError(
                f"canonical registry.records[{index}] has an invalid or duplicate registry_id"
            )
        indexed[registry_id] = record
    return indexed


def _validate_photo_run_references(
    item: Mapping[str, Any], path: str, runs_by_id: Mapping[str, Mapping[str, Any]]
) -> None:
    for provenance in item["source_provenance"]:
        source_run = runs_by_id.get(provenance["source_run_id"])
        if source_run is None:
            raise QueueError(f"{path}.source_provenance references an undeclared source run")
        details = source_run.get("outcome_details")
        candidates = details.get("photo_candidates") if isinstance(details, Mapping) else None
        matching_candidates = [
            candidate
            for candidate in candidates or []
            if isinstance(candidate, Mapping)
            and candidate.get("candidate_id") == provenance["source_record_id"]
        ]
        if len(matching_candidates) != 1:
            raise QueueError(f"{path}.source_provenance has no unique source-run photo candidate")
        if provenance != _photo_run_provenance(source_run, matching_candidates[0]):
            raise QueueError(f"{path}.source_provenance does not match its source candidate")


def _validate_incoming(
    incoming: Any,
    records: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    if not isinstance(incoming, Mapping) or incoming.get("schema_revision") != INCOMING_SCHEMA:
        raise QueueError(f"incoming schema_revision must be {INCOMING_SCHEMA}")
    if incoming.get("campaign_key") != registry.CAMPAIGN_KEY:
        raise QueueError("incoming campaign_key does not match the studio registry")
    runs = incoming.get("source_runs")
    observations = incoming.get("observations")
    if not isinstance(runs, list) or not isinstance(observations, list):
        raise QueueError("incoming source_runs and observations must be arrays")
    run_ids: set[str] = set()
    runs_by_id: dict[str, Mapping[str, Any]] = {}
    for index, run in enumerate(runs):
        if not isinstance(run, Mapping) or not isinstance(run.get("run_id"), str):
            raise QueueError(f"incoming.source_runs[{index}] has no run_id")
        if run["run_id"] in run_ids:
            raise QueueError(f"incoming.source_runs[{index}] repeats run_id")
        run_ids.add(run["run_id"])
        runs_by_id[run["run_id"]] = run
        coverage = run.get("coverage")
        if not isinstance(coverage, Mapping):
            raise QueueError(f"incoming.source_runs[{index}].coverage must be an object")
        for count in (
            "requested",
            "obtained",
            "matched",
            "new",
            "ambiguous",
            "unavailable",
            "denied",
        ):
            value = coverage.get(count)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise QueueError(
                    f"incoming.source_runs[{index}].coverage.{count} must be a non-negative integer"
                )
        if coverage["obtained"] > coverage["requested"]:
            raise QueueError(f"incoming.source_runs[{index}] obtained exceeds requested")
        if sum(coverage[key] for key in ("matched", "new", "ambiguous")) > coverage["obtained"]:
            raise QueueError(f"incoming.source_runs[{index}] outcomes exceed obtained records")
        if registry.parse_utc(run.get("requested_at_utc")) is None:
            raise QueueError(f"incoming.source_runs[{index}].requested_at_utc is invalid")
    observation_ids: set[str] = set()
    for index, item in enumerate(observations):
        path = f"incoming.observations[{index}]"
        if not isinstance(item, Mapping):
            raise QueueError(f"{path} must be an object")
        observation_id = item.get("observation_id")
        if not isinstance(observation_id, str) or not observation_id:
            raise QueueError(f"{path}.observation_id is required")
        if observation_id in observation_ids:
            raise QueueError(f"{path}.observation_id is duplicated")
        observation_ids.add(observation_id)
        registry_id = item.get("registry_id")
        if registry_id is not None and registry_id not in records:
            raise QueueError(f"{path}.registry_id is not present in the canonical registry")
        source_run_id = item.get("source_run_id")
        if not isinstance(source_run_id, str) or source_run_id not in run_ids:
            raise QueueError(f"{path}.source_run_id must identify a declared source run")
        subject_match = item.get("subject_match")
        if subject_match is not None and not isinstance(subject_match, Mapping):
            raise QueueError(f"{path}.subject_match must be an object or null")
        if isinstance(subject_match, Mapping):
            candidate_registry_ids = subject_match.get("candidate_registry_ids")
            if candidate_registry_ids is not None and (
                not isinstance(candidate_registry_ids, list)
                or any(
                    not isinstance(candidate_id, str) or candidate_id not in records
                    for candidate_id in candidate_registry_ids
                )
            ):
                raise QueueError(
                    f"{path}.subject_match.candidate_registry_ids must reference canonical records"
                )
        source = item.get("source")
        if not isinstance(source, Mapping):
            raise QueueError(f"{path}.source must be an object")
        for key in ("source_key", "source_url", "acquired_at_utc"):
            value = source.get(key)
            if not isinstance(value, str) or not value:
                raise QueueError(f"{path}.source.{key} is required")
        for key in ("source_record_id", "dataset_release"):
            value = source.get(key)
            if value is not None and (not isinstance(value, str) or not value):
                raise QueueError(f"{path}.source.{key} must be a non-empty string or null")
        verification_status = source.get("verification_status")
        digest = source.get("response_digest_sha256")
        if verification_status is not None and verification_status not in {
            "verified",
            "unverified",
            "pending_verification",
            "source_observed_official_site_verification_pending",
        }:
            raise QueueError(f"{path}.source.verification_status is unsupported")
        if digest is None:
            if verification_status != "unverified":
                raise QueueError(
                    f"{path}.source.response_digest_sha256 may be absent only with explicit "
                    "verification_status='unverified'"
                )
        elif not isinstance(digest, str) or registry.HEX64_RE.fullmatch(digest) is None:
            raise QueueError(f"{path}.source.response_digest_sha256 is invalid")
        if registry.parse_utc(source["acquired_at_utc"]) is None:
            raise QueueError(f"{path}.source.acquired_at_utc is invalid")
        for date_field in ("source_effective_at_utc", "source_updated_at_utc"):
            date_value = source.get(date_field)
            if date_value is not None and registry.parse_utc(date_value) is None:
                raise QueueError(f"{path}.source.{date_field} is invalid")
        if not registry.is_http_url(source["source_url"]):
            raise QueueError(f"{path}.source.source_url must be an absolute HTTP URL")
        if item.get("baseline_record_digest_sha256") is not None:
            if registry_id is None:
                raise QueueError(f"{path} cannot bind a baseline digest without registry_id")
            if registry.HEX64_RE.fullmatch(item["baseline_record_digest_sha256"]) is None:
                raise QueueError(f"{path}.baseline_record_digest_sha256 is invalid")
        field_path = item.get("field_path")
        if not isinstance(field_path, str) or not any(
            field_path == prefix or field_path.startswith(prefix)
            for prefix in OBSERVATION_FIELD_PREFIXES
        ):
            raise QueueError(f"{path}.field_path is not a registry patch path")
        if not isinstance(item.get("normalized_value"), (Mapping, list, str, int, float, bool)):
            raise QueueError(f"{path}.normalized_value must be a JSON value")
        try:
            json.dumps(item, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise QueueError(f"{path} is not finite JSON: {exc}") from exc
        if field_path == "photo_candidates":
            _validate_photo_observation(item, path)
            _validate_photo_run_references(item, path, runs_by_id)
    baseline_states = incoming.get("record_baselines", {})
    if not isinstance(baseline_states, Mapping):
        raise QueueError("incoming.record_baselines must be an object")
    for registry_id, state in baseline_states.items():
        if registry_id not in records or not isinstance(state, Mapping):
            raise QueueError(f"incoming.record_baselines[{registry_id!r}] is invalid")
        if registry.HEX64_RE.fullmatch(state.get("baseline_record_digest_sha256", "")) is None:
            raise QueueError(
                f"incoming.record_baselines[{registry_id!r}]."
                "baseline_record_digest_sha256 is invalid"
            )
        for key in ("field_evidence_ids", "pending_policy_evidence_ids", "review_ids"):
            values = state.get(key)
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)
                or values != sorted(set(values))
            ):
                raise QueueError(f"incoming.record_baselines[{registry_id!r}].{key} is invalid")
    return observations


def _source_evidence(item: Mapping[str, Any]) -> dict[str, Any]:
    source = item["source"]
    return {
        "source_key": source["source_key"],
        "source_record_id": source.get("source_record_id"),
        "source_url": source["source_url"],
        "dataset_release": source.get("dataset_release"),
        "source_effective_at_utc": source.get("source_effective_at_utc"),
        "source_updated_at_utc": source.get("source_updated_at_utc"),
        "acquired_at_utc": source["acquired_at_utc"],
        "response_digest_sha256": source.get("response_digest_sha256"),
        "verification_status": source.get("verification_status", "unverified"),
    }


def _field_current(record: Mapping[str, Any], field_path: str) -> Any:
    current: Any = record
    for part in field_path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _subject_key(item: Mapping[str, Any]) -> str:
    registry_id = item.get("registry_id")
    if isinstance(registry_id, str):
        return f"registry:{registry_id}"
    match = item.get("subject_match") or {}
    for key in ("stable_identity_key", "identity_key", "source_identity"):
        value = match.get(key)
        if isinstance(value, str) and value:
            return f"subject:{value}"
    source = item["source"]
    source_identity = source.get("source_record_id") or source["source_url"]
    return f"source:{source['source_key']}:{source_identity}"


def _conflict_id(subject_key: str, field_path: str, scope: str) -> str:
    if subject_key.startswith("registry:"):
        return registry.enrichment_conflict_id(
            subject_key.removeprefix("registry:"), field_path, scope
        )
    identity = "\n".join((subject_key, field_path, scope))
    return "conflict-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _field_scope(item: Mapping[str, Any]) -> str:
    scope = item.get("scope")
    return scope if isinstance(scope, str) and scope else item["field_path"]


def _value_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _repeatable_key(value: Any) -> str:
    return registry.repeatable_item_key(value)


def _effective_date(item: Mapping[str, Any]) -> tuple[str, str] | None:
    source = item["source"]
    for field in ("source_effective_at_utc", "source_updated_at_utc"):
        value = source.get(field)
        if isinstance(value, str) and registry.parse_utc(value) is not None:
            return value, field
    return None


def _accepted_value_date(
    record: Mapping[str, Any], field_path: str, scope: str, accepted_value: Any
) -> dict[str, Any] | None:
    expected_digest = registry.enrichment_value_digest(field_path, scope, accepted_value)
    reviews = [
        review
        for review in record.get("reviews", [])
        if isinstance(review, Mapping)
        and review.get("kind") in {"fact", "identity"}
        and review.get("text_digest_sha256") == expected_digest
        and isinstance(review.get("sequence"), int)
    ]
    if not reviews:
        return None
    review = max(reviews, key=lambda item: item["sequence"])
    if review.get("decision") != "approved":
        return None
    refs = review.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        return None
    evidence_by_id = {
        evidence.get("evidence_id"): evidence
        for evidence in record.get("field_evidence", [])
        if isinstance(evidence, Mapping)
    }
    evidence = [evidence_by_id.get(ref) for ref in refs]
    if any(
        not isinstance(item, Mapping) or item.get("field_path") != field_path for item in evidence
    ):
        return None
    source_digest = registry.sha256_hex(
        registry.canonical_json(
            sorted(str(item.get("response_digest_sha256")) for item in evidence)
        )
    )
    if review.get("source_evidence_digest_sha256") != source_digest:
        return None
    dates = []
    for item in evidence:
        for key in ("source_effective_at_utc", "source_updated_at_utc"):
            value = item.get(key)
            if isinstance(value, str) and registry.parse_utc(value) is not None:
                dates.append((value, key))
                break
        else:
            return None
    date = max(dates, key=lambda item: item[0])
    return {"date": date[0], "basis": date[1], "evidence_refs": refs}


def _suggested_winner(
    candidates: Sequence[Mapping[str, Any]],
    accepted_value: Any,
    *,
    record: Mapping[str, Any],
    field_path: str,
    scope: str,
) -> dict[str, Any] | None:
    contenders: list[dict[str, Any]] = []
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for item in candidates:
        if accepted_value not in (None, "", [], {}) and _value_key(
            item["normalized_value"]
        ) == _value_key(accepted_value):
            continue
        grouped.setdefault(_value_key(item["normalized_value"]), []).append(item)
    for same_value in grouped.values():
        dates = [_effective_date(item) for item in same_value]
        if any(date is None for date in dates):
            return None
        latest = max(date[0] for date in dates if date is not None)
        basis = next(date[1] for date in dates if date is not None and date[0] == latest)
        contenders.append(
            {
                "value": same_value[0]["normalized_value"],
                "date": latest,
                "basis": basis,
                "observation_ids": [item["observation_id"] for item in same_value],
            }
        )
    if accepted_value not in (None, "", [], {}):
        accepted_date = _accepted_value_date(record, field_path, scope, accepted_value)
        if accepted_date is None:
            return None
        contenders.append(
            {
                "value": accepted_value,
                "date": accepted_date["date"],
                "basis": accepted_date["basis"],
                "accepted_in_registry": True,
                "evidence_refs": accepted_date["evidence_refs"],
            }
        )
    if not contenders:
        return None
    latest = max(item["date"] for item in contenders)
    winners = [item for item in contenders if item["date"] == latest]
    return winners[0] if len(winners) == 1 else None


def _canonical_value_digest(field_path: str, scope: str, value: Any) -> str:
    return registry.enrichment_value_digest(field_path, scope, value)


def _resolution_recorded(
    item: Mapping[str, Any],
    *,
    conflict_id: str,
    record: Mapping[str, Any] | None,
) -> bool:
    if record is None:
        return False
    resolution = item.get("resolution")
    if not isinstance(resolution, Mapping):
        return False
    resolution_baseline = resolution.get("baseline_record_digest_sha256")
    if (
        not isinstance(resolution_baseline, str)
        or registry.HEX64_RE.fullmatch(resolution_baseline) is None
    ):
        return False
    # An append-only item never overwrites an accepted value, so a later record change cannot
    # make its resolution unsafe; the canonical review still binds the exact decided value.
    baseline_matches = item.get(
        "operation"
    ) == "add_collection_item" or resolution_baseline == item.get("baseline_record_digest_sha256")
    if (
        resolution.get("conflict_id") != conflict_id
        or resolution.get("field_path") != item.get("field_path")
        or resolution.get("scope") != _field_scope(item)
        or not baseline_matches
    ):
        return False
    accepted_value = resolution.get("accepted_value")
    current_value = _field_current(record, item["field_path"])
    if item.get("operation") == "add_collection_item":
        if not isinstance(current_value, list) or not any(
            _repeatable_key(value) == _repeatable_key(accepted_value) for value in current_value
        ):
            return False
    elif current_value != accepted_value:
        return False
    refs = resolution.get("evidence_refs")
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
        return False
    evidence_by_id = {
        evidence.get("evidence_id"): evidence
        for evidence in record.get("field_evidence", [])
        if isinstance(evidence, Mapping)
    }
    if any(ref not in evidence_by_id for ref in refs):
        return False
    evidence_digests = sorted(
        str(evidence_by_id[ref].get("response_digest_sha256")) for ref in refs
    )
    expected_source_digest = registry.sha256_hex(registry.canonical_json(evidence_digests))
    baseline = resolution["baseline_record_digest_sha256"]
    marker = f"studio-enrichment-resolution:{conflict_id}:{baseline}"
    expected_text_digest = _canonical_value_digest(
        item["field_path"], _field_scope(item), accepted_value
    )
    return any(
        isinstance(review, Mapping)
        and review.get("decision") == "approved"
        and review.get("kind") in {"fact", "identity"}
        and review.get("text_digest_sha256") == expected_text_digest
        and review.get("source_evidence_digest_sha256") == expected_source_digest
        and review.get("evidence_refs") == refs
        and marker in str(review.get("note", ""))
        for review in record.get("reviews", [])
    )


def _canonical_photo_decision_recorded(
    observation: Mapping[str, Any], record: Mapping[str, Any] | None
) -> bool:
    registry_id = observation.get("registry_id")
    proposed_value = observation.get("normalized_value")
    canonical_values = record.get("photo_candidates") if record is not None else None
    if (
        not isinstance(registry_id, str)
        or not isinstance(proposed_value, Mapping)
        or not isinstance(canonical_values, list)
    ):
        return False
    conflict_id = registry.enrichment_conflict_id(
        registry_id, "photo_candidates", observation["scope"]
    )
    marker_prefix = f"studio-enrichment-resolution:{conflict_id}:"
    for accepted_value in canonical_values:
        if (
            not isinstance(accepted_value, Mapping)
            or accepted_value.get("candidate_id") != proposed_value.get("candidate_id")
            or _photo_semantic_value(accepted_value) != _photo_semantic_value(proposed_value)
        ):
            continue
        for review in record.get("reviews", []):
            if not isinstance(review, Mapping):
                continue
            note = review.get("note")
            if not isinstance(note, str) or marker_prefix not in note:
                continue
            marker_start = note.index(marker_prefix) + len(marker_prefix)
            baseline = note[marker_start : marker_start + 64]
            if registry.HEX64_RE.fullmatch(baseline) is None:
                continue
            resolution = {
                "conflict_id": conflict_id,
                "field_path": "photo_candidates",
                "scope": observation["scope"],
                "baseline_record_digest_sha256": baseline,
                "accepted_value": accepted_value,
                "evidence_refs": review.get("evidence_refs"),
            }
            candidate = {
                **observation,
                "normalized_value": accepted_value,
                "resolution": resolution,
            }
            if _resolution_recorded(candidate, conflict_id=conflict_id, record=record):
                return True
    return False


def build_projection(
    registry_document: Mapping[str, Any],
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    records = _index_records(registry_document)
    observations = _validate_incoming(incoming, records)
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    unmatched: dict[str, list[Mapping[str, Any]]] = {}
    for item in observations:
        registry_id = item.get("registry_id")
        subject_key = _subject_key(item)
        if registry_id is None:
            unmatched.setdefault(subject_key, []).append(item)
            continue
        key = (subject_key, item["field_path"], _field_scope(item))
        grouped.setdefault(key, []).append(item)

    conflicts: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    unmatched_address_conflicts: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for subject_key, candidates in sorted(unmatched.items()):
        conflict_id = _conflict_id(subject_key, "identity", "studio")
        subjects = [item.get("subject_match") or {} for item in candidates]
        identity_states = {subject.get("state") for subject in subjects}
        if identity_states == {"confirmed_new_identity"}:
            reason = "new_identity_pending_registry_admission"
        elif "address_disagreement_ambiguous" in identity_states:
            reason = "address_mismatch_requires_identity_review"
        elif "ambiguous_multiple_registry_ids" in identity_states:
            reason = "identity_matches_multiple_registry_records"
        elif "shared_address_not_merged" in identity_states:
            reason = "shared_address_identity_not_merged"
        elif "name_only_review_required" in identity_states:
            reason = "identity_match_not_supported_by_address"
        else:
            reason = "identity_unmatched_or_ambiguous"

        candidate_registry_ids = sorted(
            {
                registry_id
                for subject in subjects
                for registry_id in subject.get("candidate_registry_ids", [])
                if isinstance(registry_id, str) and registry_id in records
            }
        )
        accepted_identity_candidates = []
        for registry_id in candidate_registry_ids:
            record = records[registry_id]
            accepted_value = {
                "registry_id": registry_id,
                "canonical_name": record.get("canonical_name"),
                "address": record.get("address"),
                "baseline_record_digest_sha256": _digest(record),
            }
            if "restrictions" in record:
                accepted_value["restrictions"] = record["restrictions"]
            accepted_identity_candidates.append(accepted_value)

        specific_actions = sorted(
            {
                subject["operator_action"]
                for subject in subjects
                if subject.get("state")
                in {
                    "address_disagreement_ambiguous",
                    "ambiguous_multiple_registry_ids",
                    "shared_address_not_merged",
                    "name_only_review_required",
                }
                and isinstance(subject.get("operator_action"), str)
                and subject["operator_action"]
            }
        )
        required_action = (
            "; ".join(specific_actions)
            if specific_actions
            else "verify one physical Berlin studio identity and its services; allocate a "
            "registry_id only through the canonical registry owner"
        )
        conflicts.append(
            {
                "conflict_id": conflict_id,
                "registry_id": None,
                "identity": [item.get("subject_match") for item in candidates],
                "field_path": "identity",
                "scope": "studio",
                "accepted_old_value": accepted_identity_candidates or None,
                "competing_values": [
                    {
                        "observation_id": item["observation_id"],
                        "field_path": item["field_path"],
                        "value": item["normalized_value"],
                        "evidence": _source_evidence(item),
                    }
                    for item in candidates
                ],
                "suggested_winner": None,
                "reason": reason,
                "required_verification_action": required_action,
                "baseline_record_digest_sha256": None,
            }
        )

        for item in candidates:
            subject = item.get("subject_match") or {}
            if subject.get("state") != "address_disagreement_ambiguous":
                continue
            field_path = item.get("field_path")
            if not isinstance(field_path, str) or not field_path.startswith("address."):
                continue
            stable_identity = subject.get("stable_identity_key") or item["source"].get(
                "source_record_id"
            )
            if not isinstance(stable_identity, str) or not stable_identity:
                continue
            scope = f"identity_candidate:{stable_identity}"
            for registry_id in subject.get("candidate_registry_ids", []):
                record = records.get(registry_id) if isinstance(registry_id, str) else None
                if record is None:
                    continue
                accepted_value = _field_current(record, field_path)
                if accepted_value is None or accepted_value == item["normalized_value"]:
                    continue
                key = (registry_id, field_path, scope)
                unmatched_address_conflicts.setdefault(key, []).append(item)

    for (registry_id, field_path, scope), candidates in sorted(unmatched_address_conflicts.items()):
        record = records[registry_id]
        conflict_id = _conflict_id(f"registry:{registry_id}", field_path, scope)
        conflicts.append(
            {
                "conflict_id": conflict_id,
                "registry_id": registry_id,
                "identity": [item.get("subject_match") for item in candidates],
                "field_path": field_path,
                "scope": scope,
                "accepted_old_value": _field_current(record, field_path),
                "competing_values": [
                    {
                        "observation_id": item["observation_id"],
                        "value": item["normalized_value"],
                        "evidence": _source_evidence(item),
                    }
                    for item in candidates
                ],
                "suggested_winner": None,
                "reason": "address_disagreement_ambiguous_identity",
                "required_verification_action": (
                    "verify the exact source address and physical studio identity; do not merge "
                    "or overwrite the accepted address automatically"
                ),
                "baseline_record_digest_sha256": _digest(record),
            }
        )

    for (subject_key, field_path, scope), candidates in sorted(grouped.items()):
        registry_id = candidates[0]["registry_id"]
        record = records[registry_id]
        conflict_id = _conflict_id(subject_key, field_path, scope)
        recorded_ids = {
            item["observation_id"]
            for item in candidates
            if item.get("status") in {"resolved", "consumed"}
            and _resolution_recorded(item, conflict_id=conflict_id, record=record)
        }
        candidates = [item for item in candidates if item["observation_id"] not in recorded_ids]
        if not candidates:
            continue
        baseline = candidates[0].get("baseline_record_digest_sha256")
        current_digest = _digest(record)
        current = _field_current(record, field_path)
        stale = [
            item
            for item in candidates
            if item.get("baseline_record_digest_sha256") is not None
            and item["baseline_record_digest_sha256"] != current_digest
        ]
        unrecorded_resolutions = [
            item
            for item in candidates
            if item.get("status") in {"resolved", "consumed"}
            and not _resolution_recorded(item, conflict_id=conflict_id, record=record)
        ]
        if stale or unrecorded_resolutions:
            relevant = candidates
            conflicts.append(
                {
                    "conflict_id": conflict_id,
                    "registry_id": registry_id,
                    "identity": [item.get("subject_match") for item in candidates],
                    "field_path": field_path,
                    "scope": scope,
                    "accepted_old_value": current,
                    "competing_values": [
                        {
                            "observation_id": item["observation_id"],
                            "value": item["normalized_value"],
                            "evidence": _source_evidence(item),
                        }
                        for item in relevant
                    ],
                    "suggested_winner": None,
                    "reason": (
                        "baseline_record_digest_changed"
                        if stale
                        else "resolution_not_recorded_in_canonical_history"
                    ),
                    "required_verification_action": (
                        "refresh the baseline or record the exact reviewed decision in the "
                        "canonical registry before this item can leave the projection"
                    ),
                    "baseline_record_digest_sha256": baseline,
                    "current_record_digest_sha256": current_digest,
                }
            )
            continue

        additive = all(item.get("operation") == "add_collection_item" for item in candidates)
        if additive:
            if not isinstance(current, list):
                conflicts.append(
                    {
                        "conflict_id": conflict_id,
                        "registry_id": registry_id,
                        "identity": [item.get("subject_match") for item in candidates],
                        "field_path": field_path,
                        "scope": scope,
                        "accepted_old_value": current,
                        "competing_values": [
                            {
                                "observation_id": item["observation_id"],
                                "value": item["normalized_value"],
                                "evidence": _source_evidence(item),
                            }
                            for item in candidates
                        ],
                        "suggested_winner": None,
                        "reason": "repeatable_field_has_invalid_canonical_shape",
                        "required_verification_action": "repair canonical field shape first",
                        "baseline_record_digest_sha256": baseline,
                    }
                )
                continue
            semantic_values: dict[str, list[Mapping[str, Any]]] = {}
            for item in candidates:
                value = item["normalized_value"]
                semantic_values.setdefault(_repeatable_key(value), []).append(item)
            current_keys = {_repeatable_key(value) for value in current}
            pending_values: list[dict[str, Any]] = []
            for semantic_key, same_value in semantic_values.items():
                value = same_value[0]["normalized_value"]
                if semantic_key not in current_keys:
                    pending_values.append(
                        {
                            "value": value,
                            "observation_ids": [item["observation_id"] for item in same_value],
                            "evidence": [_source_evidence(item) for item in same_value],
                            "state": "candidate_addition_pending",
                        }
                    )
                else:
                    pending_values.append(
                        {
                            "value": value,
                            "observation_ids": [item["observation_id"] for item in same_value],
                            "evidence": [_source_evidence(item) for item in same_value],
                            "state": "canonical_value_present_evidence_merge_pending",
                        }
                    )
            gaps.append(
                {
                    "gap_id": "gap-" + hashlib.sha256(conflict_id.encode("utf-8")).hexdigest()[:20],
                    "registry_id": registry_id,
                    "field_path": field_path,
                    "scope": scope,
                    "accepted_old_value": current,
                    "candidate_values": pending_values,
                    "baseline_record_digest_sha256": baseline,
                    "required_verification_action": candidates[0].get(
                        "required_verification_action",
                        "verify collection items and attach evidence via the canonical patch path",
                    ),
                }
            )
            continue

        distinct: dict[str, list[Mapping[str, Any]]] = {}
        for item in candidates:
            distinct.setdefault(_value_key(item["normalized_value"]), []).append(item)
        current_present = current not in (None, "", [], {})
        differs_from_current = current_present and any(
            item["normalized_value"] != current for item in candidates
        )
        explicit_correction = any(
            item.get("operation") in {"correct_value", "explicit_correction"} for item in candidates
        )
        if len(distinct) > 1 or differs_from_current or explicit_correction:
            competing = [item for group in distinct.values() for item in group]
            conflicts.append(
                {
                    "conflict_id": conflict_id,
                    "registry_id": registry_id,
                    "identity": [item.get("subject_match") for item in candidates],
                    "field_path": field_path,
                    "scope": scope,
                    "accepted_old_value": current,
                    "competing_values": [
                        {
                            "observation_ids": [item["observation_id"] for item in same_value],
                            "value": same_value[0]["normalized_value"],
                            "evidence": [_source_evidence(item) for item in same_value],
                        }
                        for same_value in distinct.values()
                    ],
                    "suggested_winner": _suggested_winner(
                        competing,
                        current,
                        record=record,
                        field_path=field_path,
                        scope=scope,
                    ),
                    "reason": (
                        "explicit_source_correction_claim"
                        if explicit_correction
                        else "competing_single_value_observations"
                        if len(distinct) > 1 and not current_present
                        else "competing_single_value_observation"
                    ),
                    "required_verification_action": (
                        "compare field meaning, scope, source-effective dates and exact evidence; "
                        "submit a baseline-bound resolution without overwriting automatically"
                    ),
                    "baseline_record_digest_sha256": baseline,
                }
            )
            continue

        same_value = next(iter(distinct.values()))
        gaps.append(
            {
                "gap_id": "gap-" + hashlib.sha256(conflict_id.encode("utf-8")).hexdigest()[:20],
                "registry_id": registry_id,
                "field_path": field_path,
                "scope": scope,
                "accepted_old_value": current,
                "candidate_values": [
                    {
                        "value": same_value[0]["normalized_value"],
                        "observation_ids": [item["observation_id"] for item in same_value],
                        "evidence": [_source_evidence(item) for item in same_value],
                        "state": (
                            "canonical_value_present_evidence_merge_pending"
                            if current_present
                            else "candidate_value_pending_verification"
                        ),
                    }
                ],
                "baseline_record_digest_sha256": baseline,
                "required_verification_action": candidates[0].get(
                    "required_verification_action",
                    "verify the candidate value and submit an evidence-backed canonical patch",
                ),
            }
        )
    return {
        "schema_revision": CONFLICTS_SCHEMA,
        "generated_from": {
            "canonical_registry": str(DEFAULT_REGISTRY).replace("\\", "/"),
            "canonical_registry_digest_sha256": _digest(registry_document),
            "incoming_queue": str(DEFAULT_INCOMING).replace("\\", "/"),
            "incoming_digest_sha256": _digest(incoming),
            "generated_at_utc": max(
                (run.get("requested_at_utc", "") for run in incoming.get("source_runs", [])),
                default="",
            ),
        },
        "conflicts": sorted(conflicts, key=lambda item: item["conflict_id"]),
        "field_gaps": sorted(gaps, key=lambda item: item["gap_id"]),
    }


def validate_projection(
    registry_document: Mapping[str, Any],
    incoming: Mapping[str, Any],
    conflicts: Any,
) -> None:
    expected = build_projection(registry_document, incoming)
    if not isinstance(conflicts, Mapping) or conflicts.get("schema_revision") != CONFLICTS_SCHEMA:
        raise QueueError(f"conflicts schema_revision must be {CONFLICTS_SCHEMA}")
    expected_rows = (expected["conflicts"], expected["field_gaps"])
    actual_rows = (conflicts.get("conflicts"), conflicts.get("field_gaps"))
    if actual_rows != expected_rows:
        raise QueueError(
            "conflicts projection is stale or differs from canonical registry and incoming queue"
        )
    generated_from = conflicts.get("generated_from")
    if not isinstance(generated_from, Mapping):
        raise QueueError("conflicts.generated_from must be an object")
    for key, value in expected["generated_from"].items():
        if generated_from.get(key) != value:
            raise QueueError(f"conflicts.generated_from.{key} does not match current inputs")


def _record_baseline_state(record: Mapping[str, Any]) -> dict[str, Any]:
    evidence = record.get("field_evidence", [])
    reviews = record.get("reviews", [])
    return {
        "baseline_record_digest_sha256": _digest(record),
        "field_evidence_ids": sorted(
            str(item["evidence_id"]) for item in evidence if isinstance(item, Mapping)
        ),
        "pending_policy_evidence_ids": sorted(
            str(item["evidence_id"])
            for item in evidence
            if isinstance(item, Mapping)
            and item.get("source_kind") == "official_site"
            and item.get("policy_decision") is None
        ),
        "review_ids": sorted(
            str(item["review_id"]) for item in reviews if isinstance(item, Mapping)
        ),
    }


def _capture_baseline_states(
    registry_document: Mapping[str, Any], incoming: dict[str, Any]
) -> bool:
    records = _index_records(registry_document)
    observations = _validate_incoming(incoming, records)
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for item in observations:
        registry_id = item.get("registry_id")
        if isinstance(registry_id, str):
            groups.setdefault(registry_id, []).append(item)
    states = incoming.setdefault("record_baselines", {})
    if not isinstance(states, dict):
        raise QueueError("incoming.record_baselines must be an object")
    changed = False
    for registry_id, items in groups.items():
        current_digest = _digest(records[registry_id])
        if any(item.get("baseline_record_digest_sha256") != current_digest for item in items):
            continue
        state = _record_baseline_state(records[registry_id])
        if states.get(registry_id) != state:
            states[registry_id] = state
            changed = True
    return changed


def rebase_metadata_batch(
    registry_document: Mapping[str, Any],
    incoming: dict[str, Any],
    *,
    policy_digest_sha256: str,
    reviewer: str,
    decided_at_utc: str,
    registry_ids: Sequence[str] | None = None,
) -> dict[str, str]:
    """Rebase whole-record observation groups across only recorded evidence/policy changes.

    For each record, the tool reverses precisely the evidence append and official-site policy
    decision described by the saved pre-run baseline metadata. The reconstructed canonical record
    must hash to the original baseline before any pending observation for that record is rebased.
    """
    records = _index_records(registry_document)
    observations = _validate_incoming(incoming, records)
    states = incoming.get("record_baselines")
    if not isinstance(states, Mapping):
        raise QueueError("refresh must capture incoming.record_baselines before collection changes")
    wanted = set(registry_ids) if registry_ids else None
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for item in observations:
        registry_id = item.get("registry_id")
        if (
            isinstance(registry_id, str)
            and item.get("status") in {"pending_verification", "pending_review"}
            and (wanted is None or registry_id in wanted)
        ):
            groups.setdefault(registry_id, []).append(item)

    updated = copy.deepcopy(incoming)
    updated_observations = {
        item["observation_id"]: item for item in updated.get("observations", [])
    }
    rebased: dict[str, str] = {}
    expected_policy = {
        "decision": "approved",
        "reviewer": reviewer,
        "decided_at_utc": decided_at_utc,
        "scope": "official_site facts_only",
        "policy_digest_sha256": policy_digest_sha256,
    }
    for registry_id, items in groups.items():
        state = states.get(registry_id)
        if not isinstance(state, Mapping):
            raise QueueError(f"record {registry_id} has no captured pre-change baseline state")
        baseline = state.get("baseline_record_digest_sha256")
        if any(item.get("baseline_record_digest_sha256") != baseline for item in items):
            raise QueueError(f"record {registry_id} has observations from different baselines")
        record = records[registry_id]
        current_digest = _digest(record)
        if current_digest == baseline:
            rebased[registry_id] = current_digest
            continue

        baseline_evidence_ids = set(state.get("field_evidence_ids", []))
        baseline_pending_ids = set(state.get("pending_policy_evidence_ids", []))
        baseline_review_ids = set(state.get("review_ids", []))
        evidence = record.get("field_evidence", [])
        evidence_by_id = {
            entry.get("evidence_id"): entry for entry in evidence if isinstance(entry, Mapping)
        }
        if len(evidence_by_id) != len(evidence):
            raise QueueError(f"record {registry_id} has duplicate evidence ids")
        if not baseline_evidence_ids.issubset(evidence_by_id):
            raise QueueError(f"record {registry_id} lost evidence from its captured baseline")

        expected_added_ids: set[str] = set()
        for item in items:
            source = item["source"]
            if source.get("source_key") != "official_site":
                continue
            if source.get("verification_status") != "verified":
                continue
            digest = source.get("response_digest_sha256")
            if not isinstance(digest, str):
                raise QueueError("verified observations require a source response digest")
            evidence_id = registry.evidence_id_for(
                registry_id, source["source_url"], digest, item["field_path"]
            )
            if evidence_id not in baseline_evidence_ids:
                expected_added_ids.add(evidence_id)
        added_ids = set(evidence_by_id) - baseline_evidence_ids
        if added_ids != expected_added_ids:
            raise QueueError(
                f"record {registry_id} evidence additions do not exactly match verified queue items"
            )
        if not added_ids:
            raise QueueError(f"record {registry_id} has no matching evidence append to rebase")
        for item in items:
            source = item["source"]
            if (
                source.get("source_key") != "official_site"
                or source.get("verification_status") != "verified"
            ):
                continue
            evidence_id = registry.evidence_id_for(
                registry_id,
                source["source_url"],
                source["response_digest_sha256"],
                item["field_path"],
            )
            if evidence_id in added_ids:
                entry = evidence_by_id[evidence_id]
                if (
                    entry.get("field_path") != item["field_path"]
                    or entry.get("source_kind") != source.get("source_key")
                    or entry.get("source_url") != source.get("source_url")
                    or entry.get("response_digest_sha256") != source.get("response_digest_sha256")
                    or entry.get("policy_decision") != expected_policy
                ):
                    raise QueueError(
                        f"record {registry_id} appended evidence does not match the decided source"
                    )

        prior_record = copy.deepcopy(record)
        prior_record["field_evidence"] = [
            entry
            for entry in prior_record.get("field_evidence", [])
            if not isinstance(entry, Mapping) or entry.get("evidence_id") not in added_ids
        ]
        prior_evidence = {
            entry.get("evidence_id"): entry
            for entry in prior_record.get("field_evidence", [])
            if isinstance(entry, Mapping)
        }
        for evidence_id in baseline_pending_ids:
            entry = prior_evidence.get(evidence_id)
            if entry is None:
                raise QueueError(f"record {registry_id} lost pending policy evidence {evidence_id}")
            decision = entry.get("policy_decision")
            if decision is None:
                continue
            if decision != expected_policy:
                raise QueueError(
                    f"record {registry_id} pending evidence received a different policy decision"
                )
            entry["policy_decision"] = None

        reviews = prior_record.get("reviews", [])
        review_ids = {item.get("review_id") for item in reviews if isinstance(item, Mapping)}
        if not baseline_review_ids.issubset(review_ids):
            raise QueueError(f"record {registry_id} lost review history from its baseline")
        new_reviews = [
            item
            for item in reviews
            if isinstance(item, Mapping) and item.get("review_id") not in baseline_review_ids
        ]
        expected_review = [
            item
            for item in new_reviews
            if item.get("kind") == "source_policy"
            and item.get("decision") == "approved"
            and item.get("actor_id") == reviewer
            and item.get("decided_at_utc") == decided_at_utc
            and item.get("source_evidence_digest_sha256") == policy_digest_sha256
            and item.get("note")
            == "operator approved official-site facts-only usage for this campaign"
        ]
        if len(new_reviews) != 1 or len(expected_review) != 1:
            raise QueueError(
                f"record {registry_id} has concurrent or unexpected review history changes"
            )
        expected_review_id = expected_review[0].get("review_id")
        prior_record["reviews"] = [
            item
            for item in reviews
            if not isinstance(item, Mapping) or item.get("review_id") != expected_review_id
        ]
        if _digest(prior_record) != baseline:
            raise QueueError(
                f"record {registry_id} changed beyond "
                "record-evidence and decide; batch rebase refused"
            )

        for item in items:
            updated_observations[item["observation_id"]]["baseline_record_digest_sha256"] = (
                current_digest
            )
        updated["record_baselines"][registry_id] = _record_baseline_state(record)
        rebased[registry_id] = current_digest

    incoming.clear()
    incoming.update(updated)
    return rebased


def _photo_run_provenance(
    source_run: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "source_run_id": source_run["run_id"],
        "source_key": source_run.get("source_key"),
        "source_record_id": candidate.get("candidate_id"),
        "source_url": candidate.get("source_page_url"),
        "dataset_release": source_run.get("dataset_release"),
        "acquired_at_utc": source_run.get("requested_at_utc"),
        "response_digest_sha256": source_run.get("response_digest_sha256"),
        "candidate_evidence_digest_sha256": candidate.get("evidence_digest_sha256"),
    }


def _photo_candidate_observation(
    source_run: Mapping[str, Any],
    candidate: Mapping[str, Any],
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    candidate_id = candidate.get("candidate_id")
    source_key = source_run.get("source_key")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise QueueError("photo candidate candidate_id is required")
    if not isinstance(source_key, str) or not source_key:
        raise QueueError(f"source run {source_run.get('run_id')} has no source_key")
    if candidate.get("rights_state") != "candidate_unreviewed":
        raise QueueError(
            f"photo candidate {candidate_id} must remain candidate_unreviewed until reviewed"
        )
    registry_id = candidate.get("subject_registry_id")
    if registry_id is not None and registry_id not in records:
        raise QueueError(f"photo candidate {candidate_id} references an unknown registry_id")
    match_status = candidate.get("subject_match_status")
    if not isinstance(match_status, str) or not match_status:
        raise QueueError(f"photo candidate {candidate_id} subject_match_status is required")

    attribution = candidate.get("attribution_required")
    if isinstance(attribution, str) and attribution in {"true", "false"}:
        attribution = attribution == "true"
    metadata = {
        "source_page_url": candidate.get("source_page_url"),
        "source_title": candidate.get("source_title"),
        "image_description": candidate.get("image_description"),
        "image_taken_at": candidate.get("image_taken_at"),
        "image_metadata_recorded_at": candidate.get("image_metadata_recorded_at"),
        "rights_holder_profile_url": candidate.get("rights_holder_profile_url"),
        "credit": candidate.get("credit"),
        "attribution_required": attribution,
        "mime": candidate.get("mime"),
        "size_bytes": candidate.get("size_bytes"),
        "width": candidate.get("width"),
        "height": candidate.get("height"),
    }
    normalized_value = {
        "candidate_id": candidate_id,
        "source_url": candidate.get("source_url"),
        "rights_holder": candidate.get("rights_holder"),
        "license_url": candidate.get("license_url"),
        "license_version": candidate.get("license_version"),
        "rights_state": "candidate_unreviewed",
        "observed_at_utc": candidate.get("observed_at_utc"),
        "evidence_digest_sha256": candidate.get("evidence_digest_sha256"),
        "omission_reason": candidate.get("omission_reason"),
    }
    source_run_id = source_run["run_id"]
    source_page_url = candidate.get("source_page_url")
    acquired_at = source_run.get("requested_at_utc")
    observation = {
        "observation_id": f"{source_key}:photo:{candidate_id}",
        "source_run_id": source_run_id,
        "registry_id": registry_id,
        "subject_match": {
            "state": "matched_review_required" if registry_id else "candidate_unverified",
            "method": "commons_description_subject_claim",
            "identity_verification_status": match_status,
            "source_label": metadata["image_description"],
            "candidate_registry_ids": [registry_id] if registry_id else [],
            "stable_identity_key": f"{source_key}:photo:{candidate_id}",
        },
        "field_path": "photo_candidates",
        "scope": f"photo:{candidate_id}",
        "operation": "add_collection_item",
        "normalized_value": normalized_value,
        "photo_metadata": metadata,
        "image_bytes_downloaded": candidate.get("image_bytes_downloaded"),
        "image_bytes_sha256": candidate.get("image_bytes_sha256"),
        "source": {
            "source_key": source_key,
            "source_record_id": candidate_id,
            "dataset_release": source_run.get("dataset_release"),
            "source_url": source_page_url,
            "acquired_at_utc": acquired_at,
            "source_effective_at_utc": None,
            "source_updated_at_utc": None,
            "response_digest_sha256": source_run.get("response_digest_sha256"),
            "verification_status": "pending_verification",
        },
        "source_provenance": [_photo_run_provenance(source_run, candidate)],
        "baseline_record_digest_sha256": (
            registry.registry_record_digest(records[registry_id]) if registry_id else None
        ),
        "status": "pending_verification",
        "required_verification_action": (
            "verify depicted subject and rights; candidate_unreviewed is not publication permission"
        ),
    }
    _validate_photo_observation(observation, f"photo candidate {candidate_id}")
    return observation


def _photo_provenance_history(
    source_runs: Sequence[Mapping[str, Any]], *, source_key: str, candidate_id: str
) -> list[dict[str, Any]]:
    by_run_id: dict[str, dict[str, Any]] = {}
    for source_run in source_runs:
        if source_run.get("source_key") != source_key:
            continue
        details = source_run.get("outcome_details")
        candidates = details.get("photo_candidates") if isinstance(details, Mapping) else None
        if not isinstance(candidates, list):
            continue
        matches = [
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping) and candidate.get("candidate_id") == candidate_id
        ]
        if len(matches) > 1:
            raise QueueError(
                f"source run {source_run.get('run_id')} repeats photo candidate {candidate_id}"
            )
        if not matches:
            continue
        provenance = _photo_run_provenance(source_run, matches[0])
        run_id = provenance["source_run_id"]
        previous = by_run_id.get(run_id)
        if previous is not None and previous != provenance:
            raise QueueError(
                f"source run {run_id} changed provenance for photo candidate {candidate_id}"
            )
        by_run_id[run_id] = provenance
    return sorted(
        by_run_id.values(), key=lambda item: (item["acquired_at_utc"], item["source_run_id"])
    )


def _photo_semantic_value(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: child
        for key, child in value.items()
        if key not in {"observed_at_utc", "evidence_digest_sha256"}
    }


def _same_source_photo(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> bool:
    existing_source = existing.get("source")
    incoming_source = incoming.get("source")
    existing_value = existing.get("normalized_value")
    incoming_value = incoming.get("normalized_value")
    return (
        existing.get("registry_id") == incoming.get("registry_id")
        and existing.get("subject_match") == incoming.get("subject_match")
        and existing.get("field_path") == incoming.get("field_path")
        and existing.get("scope") == incoming.get("scope")
        and existing.get("operation") == incoming.get("operation")
        and isinstance(existing_source, Mapping)
        and isinstance(incoming_source, Mapping)
        and all(
            existing_source.get(key) == incoming_source.get(key)
            for key in ("source_key", "source_record_id", "source_url")
        )
        and isinstance(existing_value, Mapping)
        and isinstance(incoming_value, Mapping)
        and _photo_semantic_value(existing_value) == _photo_semantic_value(incoming_value)
        and existing.get("photo_metadata") == incoming.get("photo_metadata")
        and existing.get("image_bytes_downloaded") == incoming.get("image_bytes_downloaded")
        and existing.get("image_bytes_sha256") == incoming.get("image_bytes_sha256")
    )


def materialize_photo_candidates(
    registry_document: Mapping[str, Any], incoming: dict[str, Any], *, run_id: str
) -> dict[str, int]:
    """Upsert photo candidates by stable provider identity without losing acquisition history."""
    records = _index_records(registry_document)
    _validate_incoming(incoming, records)
    working = copy.deepcopy(incoming)
    source_run = next(
        (run for run in working["source_runs"] if run.get("run_id") == run_id),
        None,
    )
    if not isinstance(source_run, Mapping):
        raise QueueError(f"source run {run_id} was not found")
    details = source_run.get("outcome_details")
    if not isinstance(details, dict):
        raise QueueError(f"source run {run_id} has no mutable outcome_details")
    candidates = details.get("photo_candidates")
    if not isinstance(candidates, list) or not candidates:
        raise QueueError(f"source run {run_id} has no photo_candidates to materialize")

    generated: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise QueueError(f"source run {run_id} contains a malformed photo candidate")
        item = _photo_candidate_observation(source_run, candidate, records)
        item["source_provenance"] = _photo_provenance_history(
            working["source_runs"],
            source_key=item["source"]["source_key"],
            candidate_id=candidate["candidate_id"],
        )
        _validate_photo_observation(item, f"photo candidate {candidate['candidate_id']}")
        generated.append(item)
    generated_ids = [item["observation_id"] for item in generated]
    if len(generated_ids) != len(set(generated_ids)):
        raise QueueError(f"source run {run_id} repeats a photo candidate identity")

    current = {item["observation_id"]: item for item in working["observations"]}
    prior_ids = details.get("photo_observation_ids", [])
    if (
        not isinstance(prior_ids, list)
        or any(not isinstance(value, str) for value in prior_ids)
        or len(prior_ids) != len(set(prior_ids))
    ):
        raise QueueError(f"source run {run_id} photo_observation_ids is invalid")
    recorded_ids = set(prior_ids)
    added = restored = updated = unchanged = canonical_decisions = 0

    for item in generated:
        observation_id = item["observation_id"]
        existing = current.get(observation_id)
        if existing is not None:
            if not _same_source_photo(existing, item):
                raise QueueError(
                    f"photo candidate {observation_id} changed; keep the new source run for review "
                    "instead of overwriting a pending proposal"
                )
            prior_provenance = existing.get("source_provenance")
            if not isinstance(prior_provenance, list):
                raise QueueError(
                    f"photo observation {observation_id} has no source provenance history"
                )
            by_run_id = {entry["source_run_id"]: entry for entry in prior_provenance}
            for entry in item["source_provenance"]:
                previous = by_run_id.get(entry["source_run_id"])
                if previous is not None and previous != entry:
                    raise QueueError(
                        f"photo observation {observation_id} has changed provenance for "
                        f"source run {entry['source_run_id']}"
                    )
                by_run_id[entry["source_run_id"]] = entry
            merged_provenance = sorted(
                by_run_id.values(),
                key=lambda entry: (entry["acquired_at_utc"], entry["source_run_id"]),
            )
            if merged_provenance != prior_provenance:
                existing["source_provenance"] = merged_provenance
                updated += 1
            else:
                unchanged += 1
            recorded_ids.add(observation_id)
        else:
            canonical_record = records.get(item.get("registry_id"))
            if _canonical_photo_decision_recorded(item, canonical_record):
                canonical_decisions += 1
            else:
                working["observations"].append(item)
                current[observation_id] = item
                added += 1
                if observation_id in recorded_ids:
                    restored += 1
                recorded_ids.add(observation_id)

    details["photo_observation_ids"] = sorted(recorded_ids)
    details["photo_observations_written"] = len(recorded_ids)
    details["blocker"] = (
        "Candidates remain candidate_unreviewed. Missing or present image-byte digests do not "
        "establish depicted-subject verification or publication permission."
    )
    _validate_incoming(working, records)
    incoming.clear()
    incoming.update(working)
    return {
        "added": added,
        "restored": restored,
        "updated": updated,
        "unchanged": unchanged,
        "already_materialized": canonical_decisions,
        "total": len(generated),
    }


def refresh(registry_path: Path, incoming_path: Path, conflicts_path: Path) -> dict[str, Any]:
    registry_document = _load(registry_path)
    incoming = _load(incoming_path)
    if not isinstance(registry_document, Mapping) or not isinstance(incoming, dict):
        raise QueueError("registry and incoming queue roots must be objects")
    captured = _capture_baseline_states(registry_document, incoming)
    projection = build_projection(registry_document, incoming)
    if captured:
        registry.write_text_atomic(incoming_path, registry.canonical_json(incoming))
    registry.write_text_atomic(conflicts_path, registry.canonical_json(projection))
    return projection


def materialize_photo_candidates_file(
    registry_path: Path, incoming_path: Path, *, run_id: str
) -> dict[str, int]:
    registry_document = _load(registry_path)
    incoming = _load(incoming_path)
    if not isinstance(registry_document, Mapping) or not isinstance(incoming, dict):
        raise QueueError("registry and incoming queue roots must be objects")
    before = copy.deepcopy(incoming)
    result = materialize_photo_candidates(registry_document, incoming, run_id=run_id)
    if incoming != before:
        registry.write_text_atomic(incoming_path, registry.canonical_json(incoming))
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("refresh", "validate"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
        sub.add_argument("--incoming", type=Path, default=DEFAULT_INCOMING)
        sub.add_argument("--conflicts", type=Path, default=DEFAULT_CONFLICTS)
    rebase = subparsers.add_parser(
        "rebase-metadata-batch",
        help="rebase matched observations after verified evidence and policy decisions",
    )
    rebase.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    rebase.add_argument("--incoming", type=Path, default=DEFAULT_INCOMING)
    rebase.add_argument("--policy-file", type=Path, required=True)
    rebase.add_argument("--reviewer", required=True)
    rebase.add_argument("--decided-at-utc", required=True)
    rebase.add_argument("--registry-id", action="append")
    materialize = subparsers.add_parser(
        "materialize-photo-candidates",
        help="materialize source-run photo metadata as unreviewed queue observations",
    )
    materialize.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    materialize.add_argument("--incoming", type=Path, default=DEFAULT_INCOMING)
    materialize.add_argument("--run-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        registry_document = _load(args.registry)
        incoming = _load(args.incoming)
        if not isinstance(registry_document, Mapping) or not isinstance(incoming, Mapping):
            raise QueueError("registry and incoming queue roots must be objects")
        if args.command == "materialize-photo-candidates":
            result = materialize_photo_candidates_file(
                args.registry, args.incoming, run_id=args.run_id
            )
            print(
                f"photo_observations_added={result['added']} "
                f"restored={result['restored']} updated={result['updated']} "
                f"unchanged={result['unchanged']} "
                f"canonical_decisions={result['already_materialized']} "
                f"total={result['total']}"
            )
            return 0
        if args.command == "rebase-metadata-batch":
            if not isinstance(incoming, dict):
                raise QueueError("incoming queue root must be an object")
            if not args.policy_file.is_file():
                raise QueueError(f"policy file not found: {args.policy_file}")
            if registry.parse_utc(args.decided_at_utc) is None or not args.reviewer.strip():
                raise QueueError("reviewer and a valid UTC policy decision time are required")
            policy_digest = hashlib.sha256(args.policy_file.read_bytes()).hexdigest()
            rebased = rebase_metadata_batch(
                registry_document,
                incoming,
                policy_digest_sha256=policy_digest,
                reviewer=args.reviewer,
                decided_at_utc=args.decided_at_utc,
                registry_ids=args.registry_id,
            )
            registry.write_text_atomic(args.incoming, registry.canonical_json(incoming))
            print(f"rebased_records={len(rebased)}")
            return 0
        if args.command == "refresh":
            if not isinstance(incoming, dict):
                raise QueueError("incoming queue root must be an object")
            captured = _capture_baseline_states(registry_document, incoming)
            projection = build_projection(registry_document, incoming)
            if captured:
                registry.write_text_atomic(args.incoming, registry.canonical_json(incoming))
            registry.write_text_atomic(args.conflicts, registry.canonical_json(projection))
            print(
                f"observations={len(incoming['observations'])} "
                f"conflicts={len(projection['conflicts'])} "
                f"field_gaps={len(projection['field_gaps'])} output={args.conflicts}"
            )
            return 0
        conflicts = _load(args.conflicts)
        validate_projection(registry_document, incoming, conflicts)
        print(
            f"valid observations={len(incoming['observations'])} "
            f"conflicts={len(conflicts['conflicts'])} "
            f"field_gaps={len(conflicts['field_gaps'])}"
        )
        return 0
    except QueueError as exc:
        print(f"studio enrichment queue: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
