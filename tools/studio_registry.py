"""Versioned studio research registry for the Berlin recording services campaign.

One committed file, one record identity:

* ``campaigns/berlin_recording_services/discovery/studios.json``
  master worklist: every candidate, incomplete facts, evidence references.

The master is the only registry, and it is the only input of the Catalog import
manifest. Rich material leaves this file per field: an approved description or a
validated contact travels on its own, so one unapproved field never withholds the
rest of the record and never becomes an instruction to delete stored data.

This registry is a versioned worklist/snapshot of DCPF results, not a review
authority. Apparent review state inside the file never enables publication on its
own: Catalog owns revision approval, translation review and publication, Ingestion
owns validation and delivery. ``publication_eligibility`` computed here only tells
whether the file holds the evidence those owners need.

No network, database or object-store access happens in this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_REVISION = "studio-registry-v1"
PATCH_SCHEMA_REVISION = "studio-registry-patch-v1"
CAMPAIGN_KEY = "berlin_recording_services"
CONTENT_LOCALES = ("de-DE", "en-GB", "ru-RU")
CAMPAIGN_LOCALES = ("de-DE", "en-GB")

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "campaigns" / CAMPAIGN_KEY / "discovery" / "studios.json"

# Immutable namespace: a registry id is derived once from the authoritative identity
# key and then never recomputed. Changing this value would rewrite every identity.
REGISTRY_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, f"aggregator:studio-registry:{CAMPAIGN_KEY}")

ENTITY_KINDS = ("place",)
IDENTITY_KEY_KINDS = (
    "official_domain",
    "catalog_listing",
    "catalog_subject",
    "manual_seed",
    "osm_element",
    "wikidata_entity",
)
IDENTITY_KEY_AUTHORITIES = ("authoritative", "supporting")
SUBJECT_MATCH_DECISIONS = (
    "undecided",
    "link_existing",
    "create_new_pending_review",
    "rejected",
)
MARKET_CLASSIFICATIONS = ("primary_market", "nearby_market", "out_of_market")
DISTRICT_BASES = (
    "address_source_verified",
    "observed_source",
    "operator_reviewed",
    "postal_code",
    "unknown",
)
DESCRIPTION_STATES = ("missing", "draft", "approved")
DESCRIPTION_ORIGINS = ("editorial", "argos_draft", "owner_submission", "imported")
REVIEW_DECISIONS = ("approved", "rejected")
REVIEW_KINDS = ("identity", "translation", "source_policy", "media_rights", "fact")
REFRESH_STATUSES = (
    "not_attempted",
    "observed",
    "stale",
    "failed",
    "blocked",
)
EVIDENCE_SOURCE_KINDS = (
    "official_site",
    "licensed_directory",
    "openstreetmap",
    "wikidata",
    "public_registry",
    "manual_operator",
    "owner_submission",
)
EVIDENCE_USAGE_POLICIES = (
    "facts_only",
    "licensed_reuse",
    "research_only",
    "blocked",
)
CONTACT_KINDS = (
    "website",
    "email",
    "phone",
    "whatsapp",
    "instagram",
    "booking_reference",
    "map_reference",
)
CONTACT_VALIDATION_STATES = ("unvalidated", "validated", "invalid")
PHOTO_RIGHTS_STATES = (
    "candidate_unreviewed",
    "rights_cleared",
    "omitted_rights_unclear",
    "omitted_no_image",
)
RESTRICTION_SEVERITIES = ("blocking", "review", "info")

# The Catalog category keys authored against the pinned product configuration
# revision in ``catalog_mapping.yaml``. A service claim must name one of these; a
# claim whose meaning has no category yet is recorded as a blocking restriction
# instead of being forced into a wrong key.
CATALOG_CATEGORY_KEYS = (
    "recording-studio",
    "vocal-recording-studio",
    "band-recording-studio",
    "drum-recording-studio",
    "podcast-studio",
    "voice-over-adr-studio",
    "music-producer",
    "recording-engineer",
    "mixing-engineer",
    "mastering-studio",
    "mixing-studio",
    "rehearsal-recording-space",
)
PLACE_CATEGORY_KEYS = (
    "recording-studio",
    "vocal-recording-studio",
    "band-recording-studio",
    "drum-recording-studio",
    "podcast-studio",
    "voice-over-adr-studio",
    "mastering-studio",
    "mixing-studio",
    "rehearsal-recording-space",
)

# Direct service wording, mapped to the place category it literally states. Production
# wording stays unclassified: the Catalog has no place category for production-only
# facilities, and a provider category cannot stand in for a physical studio.
SERVICE_KEYWORD_CATEGORY: Mapping[str, str] = {
    "recording": "recording-studio",
    "aufnahme": "recording-studio",
    "synchron": "voice-over-adr-studio",
    "mix": "mixing-studio",
    "mixing": "mixing-studio",
}
UNMAPPED_SERVICE_KEYWORDS = {"produktion", "production"}

EVIDENCE_ID_RE = re.compile(r"^ev-[0-9a-f]{16}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
POSTAL_CODE_RE = re.compile(r"^\d{5}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()/.-]{5,30}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# The house number may be glued to the street name: official sources write both
# "Hammerstr. 17" and "Dunckerstr.21".
STREET_NUMBER_RE = re.compile(
    r"^(?P<street>.+?)\s*(?P<number>\d+[A-Za-z]?(?:\s*[-/]\s*\d+[A-Za-z]?)?)$"
)
# German addresses are written "... 17 D- 14167 Berlin": the postal country prefix can be
# captured into the street fragment, leaving a detached letter after the house number.
POSTAL_COUNTRY_PREFIX_RE = re.compile(r"\s*[Dd]\s*-\s*\d{0,5}$")
DETACHED_COUNTRY_LETTER_RE = re.compile(r"^(?P<street>.+?\d+[A-Za-z]?)\s+[A-Za-z]$")
HTML_TAG_RE = re.compile(r"<[^>]+>")

RECORD_FIELDS = (
    "registry_id",
    "entity_kind",
    "canonical_name",
    "aliases",
    "identity_keys",
    "subject_match",
    "possible_duplicate_of",
    "official_domains",
    "source_original_name",
    "address",
    "market",
    "service_claims",
    "descriptions",
    "attributes",
    "contacts",
    "photo_candidates",
    "field_evidence",
    "reviews",
    "collector",
    "last_refresh",
    "restrictions",
)


@dataclass(frozen=True)
class Problem:
    code: str
    path: str
    message: str

    def render(self) -> str:
        return f"{self.code} {self.path}: {self.message}"


def registry_identity_key(kind: str, value: str) -> str:
    """Identity keys are case-insensitive and treat www./non-www as the same host.

    A host is one studio, so the two spellings must not produce two records; the stored
    domain keeps the spelling it was observed with.
    """
    normalized = value.strip().lower()
    if kind == "official_domain":
        normalized = re.sub(r"^www\.", "", normalized)
    return f"{kind}:{normalized}"


def derive_registry_id(kind: str, value: str) -> str:
    """Derive the immutable id once from the authoritative identity key."""
    return str(uuid.uuid5(REGISTRY_ID_NAMESPACE, registry_identity_key(kind, value)))


def derive_evidence_id(source_url: str, digest: str, field_path: str, owner_id: str = "") -> str:
    """Evidence ids are unique per owner: two records may cite the same page."""
    material = f"{owner_id}\n{source_url}\n{digest}\n{field_path}".encode()
    return "ev-" + hashlib.sha256(material).hexdigest()[:16]


def sha256_hex(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _nfc_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {_nfc_json_value(key): _nfc_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_nfc_json_value(item) for item in value]
    return value


def canonical_bytes(document: Any) -> bytes:
    """Stable compact JSON bytes shared by registry, queue and import manifests."""
    return json.dumps(
        _nfc_json_value(document), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_digest(document: Any) -> str:
    return sha256_hex(canonical_bytes(document))


def canonical_json(document: Any) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def registry_record_digest(record: Mapping[str, Any]) -> str:
    return canonical_digest(record)


def repeatable_item_key(value: Any) -> str:
    """Stable semantic identity for append-only collection values."""
    if (
        isinstance(value, Mapping)
        and isinstance(value.get("kind"), str)
        and isinstance(value.get("target"), str)
    ):
        return canonical_json({"kind": value["kind"], "target": value["target"]})
    if isinstance(value, Mapping):
        for key in ("candidate_id", "stable_id", "identity_key", "id"):
            if isinstance(value.get(key), str) and value[key]:
                return canonical_json({key: value[key]})
    return canonical_json(value)


def enrichment_conflict_id(registry_id: str, field_path: str, scope: str) -> str:
    material = "\n".join((f"registry:{registry_id}", field_path, scope))
    return "conflict-" + sha256_hex(material)[:20]


def enrichment_value_digest(field_path: str, scope: str, value: Any) -> str:
    return canonical_digest({"field_path": field_path, "scope": scope, "value": value})


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def is_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if any(ch in value for ch in (" ", "\n", "\t")):
        return False
    lowered = value.lower()
    return lowered.startswith("https://") or lowered.startswith("http://")


def normalize_domain(value: str) -> str:
    candidate = value.strip().lower()
    if candidate.startswith("http://") or candidate.startswith("https://"):
        candidate = candidate.split("://", 1)[1]
    return candidate.split("/", 1)[0]


def non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _problem(problems: list[Problem], code: str, path: str, message: str) -> None:
    problems.append(Problem(code, path, message))


def _check_object(
    problems: list[Problem],
    value: Any,
    path: str,
    required: Sequence[str],
    allowed: Sequence[str] | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _problem(problems, "type", path, "expected object")
        return {}
    keys = set(value)
    for key in required:
        if key not in value:
            _problem(problems, "missing_field", f"{path}.{key}", "required field is absent")
    if allowed is not None:
        for key in sorted(keys - set(allowed)):
            _problem(problems, "unknown_field", f"{path}.{key}", "field is not in the schema")
    return value


def _check_enum(problems: list[Problem], value: Any, path: str, allowed: Sequence[str]) -> None:
    if value not in allowed:
        _problem(problems, "enum", path, f"expected one of {sorted(allowed)}")


def _check_evidence_ref(
    problems: list[Problem],
    value: Any,
    path: str,
    evidence_ids: set[str],
) -> None:
    if not isinstance(value, str) or not EVIDENCE_ID_RE.match(value):
        _problem(problems, "evidence_ref_shape", path, "expected ev-<16 hex>")
        return
    if value not in evidence_ids:
        _problem(problems, "evidence_ref_unknown", path, "reference is not defined")


def validate_evidence(
    problems: list[Problem],
    evidence: Any,
    path: str,
    seen_ids: set[str],
) -> str | None:
    item = _check_object(
        problems,
        evidence,
        path,
        required=(
            "evidence_id",
            "field_path",
            "source_kind",
            "observed_at_utc",
            "response_digest_sha256",
            "excerpt",
            "usage_policy",
            "policy_decision",
            "attribution",
        ),
        allowed=(
            "evidence_id",
            "field_path",
            "source_kind",
            "source_url",
            "external_id",
            "observed_at_utc",
            "response_digest_sha256",
            "excerpt",
            "usage_policy",
            "policy_decision",
            "attribution",
            "extraction",
            "source_effective_at_utc",
            "source_updated_at_utc",
        ),
    )
    extraction = item.get("extraction")
    if extraction is not None:
        parsed = _check_object(
            problems,
            extraction,
            f"{path}.extraction",
            required=("medium", "blockIndex", "pointers"),
            allowed=("medium", "blockIndex", "pointers"),
        )
        if parsed.get("medium") != "application/ld+json":
            _problem(
                problems,
                "extraction_medium",
                f"{path}.extraction.medium",
                "expected application/ld+json",
            )
        block_index = parsed.get("blockIndex")
        if not isinstance(block_index, int) or isinstance(block_index, bool) or block_index < 0:
            _problem(
                problems,
                "extraction_block",
                f"{path}.extraction.blockIndex",
                "expected a non-negative block index",
            )
        pointers = parsed.get("pointers")
        if not isinstance(pointers, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str) or not value.startswith("/")
            for key, value in pointers.items()
        ):
            _problem(
                problems,
                "extraction_pointers",
                f"{path}.extraction.pointers",
                "expected JSON pointers keyed by the stated literal",
            )
    evidence_id = item.get("evidence_id")
    if isinstance(evidence_id, str):
        if not EVIDENCE_ID_RE.match(evidence_id):
            _problem(problems, "evidence_id_shape", f"{path}.evidence_id", "expected ev-<16 hex>")
        elif evidence_id in seen_ids:
            _problem(problems, "evidence_id_duplicate", f"{path}.evidence_id", "id repeats")
        elif isinstance(evidence_id, str):
            seen_ids.add(evidence_id)

    _check_enum(problems, item.get("source_kind"), f"{path}.source_kind", EVIDENCE_SOURCE_KINDS)
    _check_enum(problems, item.get("usage_policy"), f"{path}.usage_policy", EVIDENCE_USAGE_POLICIES)

    if not non_empty(item.get("field_path")):
        _problem(problems, "evidence_field_path", f"{path}.field_path", "field path is empty")
    if parse_utc(item.get("observed_at_utc")) is None:
        _problem(
            problems,
            "evidence_observed_at",
            f"{path}.observed_at_utc",
            "expected YYYY-MM-DDTHH:MM:SSZ",
        )
    for date_field in ("source_effective_at_utc", "source_updated_at_utc"):
        date_value = item.get(date_field)
        if date_value is not None and parse_utc(date_value) is None:
            _problem(
                problems,
                "evidence_source_date",
                f"{path}.{date_field}",
                "expected YYYY-MM-DDTHH:MM:SSZ or null",
            )
    digest = item.get("response_digest_sha256")
    if not isinstance(digest, str) or not HEX64_RE.match(digest):
        _problem(
            problems,
            "evidence_digest",
            f"{path}.response_digest_sha256",
            "expected 64 lowercase hex characters",
        )
    excerpt = item.get("excerpt")
    if not isinstance(excerpt, str) or not excerpt.strip():
        _problem(problems, "evidence_excerpt", f"{path}.excerpt", "excerpt is empty")
    else:
        if len(excerpt) > 400:
            _problem(
                problems, "evidence_excerpt_length", f"{path}.excerpt", "excerpt exceeds 400 chars"
            )
        if HTML_TAG_RE.search(excerpt):
            _problem(
                problems, "evidence_excerpt_markup", f"{path}.excerpt", "excerpt must be plain text"
            )

    source_url = item.get("source_url")
    external_id = item.get("external_id")
    if source_url is None and external_id is None:
        _problem(problems, "evidence_locator", path, "source_url or external_id is required")
    if source_url is not None and not is_http_url(source_url):
        _problem(problems, "evidence_url", f"{path}.source_url", "expected http(s) url")
    if external_id is not None and not non_empty(external_id):
        _problem(problems, "evidence_external_id", f"{path}.external_id", "external id is empty")

    decision = item.get("policy_decision")
    if decision is not None:
        decision_obj = _check_object(
            problems,
            decision,
            f"{path}.policy_decision",
            required=("decision", "reviewer", "decided_at_utc", "scope", "policy_digest_sha256"),
        )
        _check_enum(
            problems,
            decision_obj.get("decision"),
            f"{path}.policy_decision.decision",
            ("approved", "blocked"),
        )
        if not non_empty(decision_obj.get("reviewer")):
            _problem(
                problems, "policy_reviewer", f"{path}.policy_decision.reviewer", "reviewer absent"
            )
        if parse_utc(decision_obj.get("decided_at_utc")) is None:
            _problem(
                problems,
                "policy_decided_at",
                f"{path}.policy_decision.decided_at_utc",
                "expected YYYY-MM-DDTHH:MM:SSZ",
            )
        if not non_empty(decision_obj.get("scope")):
            _problem(problems, "policy_scope", f"{path}.policy_decision.scope", "scope absent")
        pd_digest = decision_obj.get("policy_digest_sha256")
        if not isinstance(pd_digest, str) or not HEX64_RE.match(pd_digest):
            _problem(
                problems,
                "policy_digest",
                f"{path}.policy_decision.policy_digest_sha256",
                "expected 64 lowercase hex characters",
            )

    attribution = item.get("attribution")
    if attribution is not None and not isinstance(attribution, str):
        _problem(problems, "attribution", f"{path}.attribution", "expected string or null")

    return evidence_id if isinstance(evidence_id, str) else None


def validate_description(
    problems: list[Problem],
    locale: str,
    description: Any,
    path: str,
    reviews: Sequence[Mapping[str, Any]],
) -> None:
    if locale not in CONTENT_LOCALES:
        _problem(problems, "description_locale", path, f"unsupported locale {locale!r}")
    item = _check_object(
        problems,
        description,
        path,
        required=(
            "state",
            "text",
            "text_digest_sha256",
            "origin",
            "review_state",
            "review_ref",
            "reason",
        ),
        allowed=(
            "state",
            "text",
            "text_digest_sha256",
            "origin",
            "review_state",
            "review_ref",
            "reason",
            "source_evidence_digest_sha256",
        ),
    )
    state = item.get("state")
    _check_enum(problems, state, f"{path}.state", DESCRIPTION_STATES)
    if state == "missing":
        if not non_empty(item.get("reason")):
            _problem(
                problems,
                "description_missing_reason",
                f"{path}.reason",
                "missing text needs a reason",
            )
        return

    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        _problem(problems, "description_text", f"{path}.text", "text is empty")
    else:
        if len(text) > 2000:
            _problem(problems, "description_length", f"{path}.text", "text exceeds 2000 chars")
        if HTML_TAG_RE.search(text):
            _problem(problems, "description_markup", f"{path}.text", "text must be plain text")
        digest = item.get("text_digest_sha256")
        if not isinstance(digest, str) or not HEX64_RE.match(digest):
            _problem(
                problems, "description_digest", f"{path}.text_digest_sha256", "expected sha256"
            )
        elif sha256_hex(text) != digest:
            _problem(
                problems,
                "description_digest_mismatch",
                f"{path}.text_digest_sha256",
                "digest does not match the text",
            )
    _check_enum(problems, item.get("origin"), f"{path}.origin", DESCRIPTION_ORIGINS)

    if state == "draft":
        if item.get("review_state") != "pending_human":
            _problem(
                problems,
                "description_draft_review_state",
                f"{path}.review_state",
                "draft text must stay pending_human",
            )
        return

    review_ref = item.get("review_ref")
    if not isinstance(review_ref, str) or not review_ref:
        _problem(
            problems, "description_review_ref", f"{path}.review_ref", "approved text needs a review"
        )
        return
    decision = latest_review_decision(
        reviews,
        kind="translation",
        review_id=review_ref,
        locale=locale,
        text_digest=item.get("text_digest_sha256"),
        source_digest=item.get("source_evidence_digest_sha256"),
    )
    if decision != "approved":
        _problem(
            problems,
            "description_review_not_approving",
            f"{path}.review_ref",
            f"latest matching review decision is {decision or 'absent'}",
        )


def latest_review_decision(
    reviews: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    review_id: str | None,
    locale: str | None = None,
    text_digest: str | None = None,
    source_digest: str | None = None,
) -> str | None:
    """Latest valid decision for the exact reviewed bytes, or None.

    A later rejection of the same text overrides an earlier approval, so the answer
    is the decision of the highest-sequence matching review, never a mix of records.
    """
    if review_id is not None and not any(
        review.get("review_id") == review_id for review in reviews
    ):
        return None
    matches = []
    for review in reviews:
        if review.get("kind") != kind:
            continue
        if locale is not None and review.get("locale") != locale:
            continue
        if text_digest is not None and review.get("text_digest_sha256") != text_digest:
            continue
        if (
            source_digest is not None
            and review.get("source_evidence_digest_sha256") != source_digest
        ):
            continue
        sequence = review.get("sequence")
        if isinstance(sequence, int):
            matches.append((sequence, review.get("decision")))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    decision = matches[-1][1]
    return decision if decision in REVIEW_DECISIONS else None


def validate_record(
    problems: list[Problem],
    record: Any,
    index: int,
    evidence_ids: set[str],
    identity_owner: dict[str, str],
    registry_id_owner: dict[str, str],
) -> None:
    path = f"records[{index}]"
    item = _check_object(problems, record, path, RECORD_FIELDS, RECORD_FIELDS)
    registry_id = item.get("registry_id")
    if not isinstance(registry_id, str) or not _is_uuid(registry_id):
        _problem(problems, "registry_id", f"{path}.registry_id", "expected uuid")
    else:
        if registry_id in registry_id_owner:
            _problem(problems, "registry_id_duplicate", f"{path}.registry_id", "id repeats")
        registry_id_owner[registry_id] = path

    _check_enum(problems, item.get("entity_kind"), f"{path}.entity_kind", ENTITY_KINDS)
    if not non_empty(item.get("canonical_name")):
        _problem(problems, "canonical_name", f"{path}.canonical_name", "name is empty")

    aliases = item.get("aliases")
    if not isinstance(aliases, list) or any(not non_empty(alias) for alias in aliases):
        _problem(problems, "aliases", f"{path}.aliases", "expected list of non-empty strings")

    identity_keys = item.get("identity_keys")
    if not isinstance(identity_keys, list) or not identity_keys:
        _problem(problems, "identity_keys", f"{path}.identity_keys", "at least one key is required")
        identity_keys = []
    authoritative = 0
    for key_index, key in enumerate(identity_keys):
        key_path = f"{path}.identity_keys[{key_index}]"
        key_obj = _check_object(
            problems,
            key,
            key_path,
            required=("kind", "value", "authority"),
            allowed=("kind", "value", "authority", "note"),
        )
        _check_enum(problems, key_obj.get("kind"), f"{key_path}.kind", IDENTITY_KEY_KINDS)
        _check_enum(
            problems, key_obj.get("authority"), f"{key_path}.authority", IDENTITY_KEY_AUTHORITIES
        )
        value = key_obj.get("value")
        if not non_empty(value):
            _problem(problems, "identity_key_value", f"{key_path}.value", "value is empty")
        kind = key_obj.get("kind")
        if isinstance(kind, str) and isinstance(value, str) and value.strip():
            canonical_key = registry_identity_key(kind, value)
            owner = identity_owner.get(canonical_key)
            if owner is not None:
                _problem(
                    problems,
                    "identity_key_conflict",
                    f"{key_path}.value",
                    f"already claimed by {owner}",
                )
            else:
                identity_owner[canonical_key] = path
        authority = key_obj.get("authority")
        if authority == "authoritative":
            authoritative += 1
    if identity_keys and authoritative == 0:
        _problem(
            problems,
            "identity_key_authority",
            f"{path}.identity_keys",
            "at least one authoritative identity key is required",
        )

    subject_match = _check_object(
        problems,
        item.get("subject_match"),
        f"{path}.subject_match",
        required=("decision", "reason", "subject_id", "listing_id"),
        allowed=("decision", "reason", "subject_id", "listing_id"),
    )
    _check_enum(
        problems,
        subject_match.get("decision"),
        f"{path}.subject_match.decision",
        SUBJECT_MATCH_DECISIONS,
    )
    if subject_match.get("decision") in (
        "link_existing",
        "create_new_pending_review",
    ) and not non_empty(subject_match.get("reason")):
        _problem(
            problems,
            "subject_match_reason",
            f"{path}.subject_match.reason",
            "decision needs a recorded reason",
        )
    for field in ("subject_id", "listing_id"):
        value = subject_match.get(field)
        if value is not None and (not isinstance(value, str) or not _is_uuid(value)):
            _problem(problems, "subject_match_id", f"{path}.subject_match.{field}", "expected uuid")
    if subject_match.get("decision") == "link_existing" and not subject_match.get("listing_id"):
        _problem(
            problems,
            "subject_match_listing",
            f"{path}.subject_match.listing_id",
            "link_existing requires the existing listing id",
        )

    duplicates = item.get("possible_duplicate_of")
    if not isinstance(duplicates, list) or any(
        not isinstance(dup, str) or not _is_uuid(dup) for dup in duplicates
    ):
        _problem(
            problems,
            "possible_duplicate_of",
            f"{path}.possible_duplicate_of",
            "expected list of uuids",
        )
    elif registry_id in duplicates:
        _problem(
            problems,
            "possible_duplicate_self",
            f"{path}.possible_duplicate_of",
            "record cannot duplicate itself",
        )

    domains = _check_object(
        problems,
        item.get("official_domains"),
        f"{path}.official_domains",
        required=("primary", "alternates"),
        allowed=("primary", "alternates"),
    )
    primary = domains.get("primary")
    if primary is not None and (
        not non_empty(primary) or normalize_domain(primary) != primary.strip().lower()
    ):
        _problem(
            problems,
            "official_domain_shape",
            f"{path}.official_domains.primary",
            "expected a bare lowercase domain",
        )
    alternates = domains.get("alternates")
    if not isinstance(alternates, list) or any(not non_empty(alt) for alt in alternates):
        _problem(
            problems,
            "official_domain_alternates",
            f"{path}.official_domains.alternates",
            "expected list of domains",
        )

    source_original_name = item.get("source_original_name")
    if source_original_name is not None and not non_empty(source_original_name):
        _problem(
            problems,
            "source_original_name",
            f"{path}.source_original_name",
            "expected string or null",
        )

    validate_address(problems, item.get("address"), f"{path}.address")
    validate_market(problems, item.get("market"), f"{path}.market")

    evidence = item.get("field_evidence")
    if not isinstance(evidence, list):
        _problem(problems, "field_evidence", f"{path}.field_evidence", "expected list")
    else:
        for evidence_index, entry in enumerate(evidence):
            validate_evidence(
                problems, entry, f"{path}.field_evidence[{evidence_index}]", evidence_ids
            )

    claims = item.get("service_claims")
    if not isinstance(claims, list):
        _problem(problems, "service_claims", f"{path}.service_claims", "expected list")
        claims = []
    for claim_index, claim in enumerate(claims):
        claim_path = f"{path}.service_claims[{claim_index}]"
        claim_obj = _check_object(
            problems,
            claim,
            claim_path,
            required=("catalog_category_key", "evidence_refs", "source_text"),
            allowed=("catalog_category_key", "evidence_refs", "source_text"),
        )
        key = claim_obj.get("catalog_category_key")
        if key is None:
            pass
        else:
            _check_enum(problems, key, f"{claim_path}.catalog_category_key", PLACE_CATEGORY_KEYS)
        refs = claim_obj.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            _problem(
                problems, "claim_evidence", f"{claim_path}.evidence_refs", "claims need evidence"
            )
        else:
            for ref_index, ref in enumerate(refs):
                _check_evidence_ref(
                    problems, ref, f"{claim_path}.evidence_refs[{ref_index}]", evidence_ids
                )
        if not non_empty(claim_obj.get("source_text")):
            _problem(problems, "claim_source_text", f"{claim_path}.source_text", "expected text")

    reviews = item.get("reviews")
    if not isinstance(reviews, list):
        _problem(problems, "reviews", f"{path}.reviews", "expected list")
        reviews = []
    validate_reviews(problems, reviews, f"{path}.reviews")
    for review_index, review in enumerate(reviews):
        if not isinstance(review, Mapping) or "evidence_refs" not in review:
            continue
        refs = review.get("evidence_refs")
        refs_path = f"{path}.reviews[{review_index}].evidence_refs"
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            _problem(
                problems, "review_evidence_refs", refs_path, "expected unique evidence references"
            )
            continue
        for ref_index, ref in enumerate(refs):
            _check_evidence_ref(problems, ref, f"{refs_path}[{ref_index}]", evidence_ids)

    descriptions = item.get("descriptions")
    if not isinstance(descriptions, dict):
        _problem(
            problems, "descriptions", f"{path}.descriptions", "expected object keyed by locale"
        )
        descriptions = {}
    for locale in sorted(descriptions):
        validate_description(
            problems, locale, descriptions[locale], f"{path}.descriptions.{locale}", reviews
        )

    attributes = item.get("attributes")
    if not isinstance(attributes, list):
        _problem(problems, "attributes", f"{path}.attributes", "expected list")
        attributes = []
    for attr_index, attribute in enumerate(attributes):
        attr_path = f"{path}.attributes[{attr_index}]"
        attr_obj = _check_object(
            problems,
            attribute,
            attr_path,
            required=("attribute_key", "value", "evidence_refs"),
            allowed=("attribute_key", "value", "evidence_refs", "observed_at_utc"),
        )
        if not non_empty(attr_obj.get("attribute_key")):
            _problem(problems, "attribute_key", f"{attr_path}.attribute_key", "expected key")
        refs = attr_obj.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            _problem(
                problems,
                "attribute_evidence",
                f"{attr_path}.evidence_refs",
                "attributes need evidence",
            )
        else:
            for ref_index, ref in enumerate(refs):
                _check_evidence_ref(
                    problems, ref, f"{attr_path}.evidence_refs[{ref_index}]", evidence_ids
                )

    validate_contacts(problems, item.get("contacts"), f"{path}.contacts")
    validate_photos(problems, item.get("photo_candidates"), f"{path}.photo_candidates")

    collector = _check_object(
        problems,
        item.get("collector"),
        f"{path}.collector",
        required=("candidate_ids", "revisions", "import_batch"),
        allowed=("candidate_ids", "revisions", "import_batch"),
    )
    for field in ("candidate_ids", "revisions"):
        value = collector.get(field)
        if not isinstance(value, list) or any(not non_empty(entry) for entry in value):
            _problem(
                problems, "collector_list", f"{path}.collector.{field}", "expected list of ids"
            )

    refresh = _check_object(
        problems,
        item.get("last_refresh"),
        f"{path}.last_refresh",
        required=("status", "at_utc"),
        allowed=("status", "at_utc"),
    )
    _check_enum(problems, refresh.get("status"), f"{path}.last_refresh.status", REFRESH_STATUSES)
    if refresh.get("at_utc") is not None and parse_utc(refresh.get("at_utc")) is None:
        _problem(problems, "refresh_at", f"{path}.last_refresh.at_utc", "expected UTC timestamp")

    validate_restrictions(
        problems, item.get("restrictions"), f"{path}.restrictions", evidence_ids=evidence_ids
    )


def validate_address(problems: list[Problem], address: Any, path: str) -> None:
    if address is None:
        return
    item = _check_object(
        problems,
        address,
        path,
        required=(
            "street",
            "street_number",
            "postal_code",
            "city",
            "district_key",
            "district_basis",
            "country_code",
            "coordinate",
        ),
        allowed=(
            "street",
            "street_number",
            "postal_code",
            "city",
            "district_key",
            "district_basis",
            "country_code",
            "coordinate",
        ),
    )
    street = item.get("street")
    number = item.get("street_number")
    postal_code = item.get("postal_code")
    city = item.get("city")
    country = item.get("country_code")
    district = item.get("district_key")

    if street is not None and not non_empty(street):
        _problem(problems, "address_street", f"{path}.street", "street is empty")
    if number is not None and (not isinstance(number, str) or not number.strip()):
        _problem(problems, "address_number", f"{path}.street_number", "number is empty")
    if street is not None or postal_code is not None or district is not None:
        if not non_empty(street):
            _problem(
                problems,
                "address_incomplete",
                f"{path}.street",
                "street is required with an address",
            )
        if not isinstance(postal_code, str) or not POSTAL_CODE_RE.match(postal_code):
            _problem(problems, "address_postal_code", f"{path}.postal_code", "expected 5 digits")
        if not non_empty(city):
            _problem(problems, "address_city", f"{path}.city", "city is required with an address")
        if not isinstance(country, str) or len(country) != 2 or country != country.upper():
            _problem(
                problems, "address_country", f"{path}.country_code", "expected ISO 3166-1 alpha-2"
            )
        if not isinstance(district, str) or not SLUG_RE.match(district):
            _problem(
                problems, "address_district", f"{path}.district_key", "expected lowercase slug"
            )
        _check_enum(problems, item.get("district_basis"), f"{path}.district_basis", DISTRICT_BASES)

    coordinate = item.get("coordinate")
    if coordinate is not None:
        coord = _check_object(
            problems,
            coordinate,
            f"{path}.coordinate",
            required=("latitude", "longitude", "precision_meters"),
            allowed=("latitude", "longitude", "precision_meters"),
        )
        latitude = coord.get("latitude")
        longitude = coord.get("longitude")
        precision = coord.get("precision_meters")
        if not isinstance(latitude, (int, float)) or not -90 <= latitude <= 90:
            _problem(
                problems, "coordinate_latitude", f"{path}.coordinate.latitude", "expected -90..90"
            )
        if not isinstance(longitude, (int, float)) or not -180 <= longitude <= 180:
            _problem(
                problems,
                "coordinate_longitude",
                f"{path}.coordinate.longitude",
                "expected -180..180",
            )
        if not isinstance(precision, (int, float)) or precision <= 0:
            _problem(
                problems,
                "coordinate_precision",
                f"{path}.coordinate.precision_meters",
                "expected positive meters",
            )


def validate_market(problems: list[Problem], market: Any, path: str) -> None:
    item = _check_object(
        problems,
        market,
        path,
        required=("classification", "basis", "source_kind"),
        allowed=("classification", "basis", "source_kind"),
    )
    _check_enum(
        problems, item.get("classification"), f"{path}.classification", MARKET_CLASSIFICATIONS
    )
    if not non_empty(item.get("basis")):
        _problem(problems, "market_basis", f"{path}.basis", "basis is required")
    _check_enum(problems, item.get("source_kind"), f"{path}.source_kind", EVIDENCE_SOURCE_KINDS)


def validate_reviews(problems: list[Problem], reviews: Any, path: str) -> None:
    if not isinstance(reviews, list):
        _problem(problems, "reviews", path, "expected list")
        return
    seen_ids: set[str] = set()
    sequences: set[int] = set()
    for index, review in enumerate(reviews):
        review_path = f"{path}[{index}]"
        item = _check_object(
            problems,
            review,
            review_path,
            required=(
                "review_id",
                "kind",
                "actor_id",
                "sequence",
                "decided_at_utc",
                "decision",
                "text_digest_sha256",
                "source_evidence_digest_sha256",
                "locale",
                "note",
            ),
            allowed=(
                "review_id",
                "kind",
                "actor_id",
                "sequence",
                "decided_at_utc",
                "decision",
                "text_digest_sha256",
                "source_evidence_digest_sha256",
                "locale",
                "note",
                "evidence_refs",
            ),
        )
        review_id = item.get("review_id")
        if not isinstance(review_id, str) or not review_id.strip():
            _problem(problems, "review_id", f"{review_path}.review_id", "expected id")
        elif review_id in seen_ids:
            _problem(problems, "review_id_duplicate", f"{review_path}.review_id", "id repeats")
        else:
            seen_ids.add(review_id)
        _check_enum(problems, item.get("kind"), f"{review_path}.kind", REVIEW_KINDS)
        _check_enum(problems, item.get("decision"), f"{review_path}.decision", REVIEW_DECISIONS)
        if not non_empty(item.get("actor_id")):
            _problem(problems, "review_actor", f"{review_path}.actor_id", "actor is required")
        sequence = item.get("sequence")
        if not isinstance(sequence, int) or sequence < 1:
            _problem(
                problems, "review_sequence", f"{review_path}.sequence", "expected positive integer"
            )
        elif sequence in sequences:
            _problem(
                problems, "review_sequence_duplicate", f"{review_path}.sequence", "sequence repeats"
            )
        else:
            sequences.add(sequence)
        if parse_utc(item.get("decided_at_utc")) is None:
            _problem(
                problems,
                "review_decided_at",
                f"{review_path}.decided_at_utc",
                "expected YYYY-MM-DDTHH:MM:SSZ",
            )
        for field in ("text_digest_sha256", "source_evidence_digest_sha256"):
            value = item.get(field)
            if not isinstance(value, str) or not HEX64_RE.match(value):
                _problem(problems, "review_digest", f"{review_path}.{field}", "expected sha256")
        locale = item.get("locale")
        if item.get("kind") == "translation":
            if locale not in CONTENT_LOCALES:
                _problem(
                    problems,
                    "review_locale",
                    f"{review_path}.locale",
                    f"translation review needs one of {CONTENT_LOCALES}",
                )
        elif locale is not None and locale not in CONTENT_LOCALES:
            _problem(problems, "review_locale", f"{review_path}.locale", "unsupported locale")
        if item.get("decision") == "rejected" and not non_empty(item.get("note")):
            _problem(
                problems, "review_rejection_note", f"{review_path}.note", "rejection needs a note"
            )


def validate_contacts(problems: list[Problem], contacts: Any, path: str) -> None:
    if not isinstance(contacts, list):
        _problem(problems, "contacts", path, "expected list")
        return
    for index, contact in enumerate(contacts):
        contact_path = f"{path}[{index}]"
        item = _check_object(
            problems,
            contact,
            contact_path,
            required=("kind", "target", "label", "validation_state", "evidence_refs"),
            allowed=("kind", "target", "label", "validation_state", "evidence_refs"),
        )
        kind = item.get("kind")
        _check_enum(problems, kind, f"{contact_path}.kind", CONTACT_KINDS)
        _check_enum(
            problems,
            item.get("validation_state"),
            f"{contact_path}.validation_state",
            CONTACT_VALIDATION_STATES,
        )
        target = item.get("target")
        if not isinstance(target, str) or not target.strip():
            _problem(problems, "contact_target", f"{contact_path}.target", "target is empty")
        elif kind == "email" and not EMAIL_RE.match(target):
            _problem(problems, "contact_email", f"{contact_path}.target", "expected email")
        elif kind == "phone" and not PHONE_RE.match(target):
            _problem(problems, "contact_phone", f"{contact_path}.target", "expected phone")
        elif kind in (
            "website",
            "instagram",
            "booking_reference",
            "map_reference",
        ) and not is_http_url(target):
            _problem(problems, "contact_url", f"{contact_path}.target", "expected http(s) url")
        label = item.get("label")
        if label is not None and not isinstance(label, str):
            _problem(problems, "contact_label", f"{contact_path}.label", "expected string or null")
        refs = item.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            _problem(
                problems,
                "contact_evidence",
                f"{contact_path}.evidence_refs",
                "contacts need evidence",
            )


def validate_photos(problems: list[Problem], photos: Any, path: str) -> None:
    if not isinstance(photos, list):
        _problem(problems, "photos", path, "expected list")
        return
    for index, photo in enumerate(photos):
        photo_path = f"{path}[{index}]"
        item = _check_object(
            problems,
            photo,
            photo_path,
            required=(
                "candidate_id",
                "source_url",
                "rights_holder",
                "license_url",
                "license_version",
                "rights_state",
                "observed_at_utc",
                "evidence_digest_sha256",
                "omission_reason",
            ),
            allowed=(
                "candidate_id",
                "source_url",
                "rights_holder",
                "license_url",
                "license_version",
                "rights_state",
                "observed_at_utc",
                "evidence_digest_sha256",
                "omission_reason",
            ),
        )
        state = item.get("rights_state")
        _check_enum(problems, state, f"{photo_path}.rights_state", PHOTO_RIGHTS_STATES)
        source_url = item.get("source_url")
        if source_url is not None and not is_http_url(source_url):
            _problem(problems, "photo_url", f"{photo_path}.source_url", "expected http(s) url")
        if state == "rights_cleared":
            for field in ("rights_holder", "license_url", "license_version"):
                if not non_empty(item.get(field)):
                    _problem(
                        problems, "photo_rights", f"{photo_path}.{field}", "required when cleared"
                    )
            if not is_http_url(item.get("license_url")):
                _problem(
                    problems,
                    "photo_license_url",
                    f"{photo_path}.license_url",
                    "expected http(s) url",
                )
        if state in ("omitted_rights_unclear", "omitted_no_image") and not non_empty(
            item.get("omission_reason")
        ):
            _problem(
                problems,
                "photo_omission_reason",
                f"{photo_path}.omission_reason",
                "reason required",
            )
        digest = item.get("evidence_digest_sha256")
        if digest is not None and (not isinstance(digest, str) or not HEX64_RE.match(digest)):
            _problem(
                problems, "photo_digest", f"{photo_path}.evidence_digest_sha256", "expected sha256"
            )


def validate_restrictions(
    problems: list[Problem],
    restrictions: Any,
    path: str,
    *,
    evidence_ids: set[str],
) -> None:
    item = _check_object(
        problems,
        restrictions,
        path,
        required=("rejected", "missing", "conflicting"),
        allowed=("rejected", "missing", "conflicting"),
    )
    for field in ("rejected", "missing", "conflicting"):
        entries = item.get(field)
        if not isinstance(entries, list):
            _problem(problems, "restrictions_shape", f"{path}.{field}", "expected list")
            continue
        for index, entry in enumerate(entries):
            entry_path = f"{path}.{field}[{index}]"
            entry_obj = _check_object(
                problems,
                entry,
                entry_path,
                required=("code", "severity", "detail"),
                allowed=(
                    "code",
                    "severity",
                    "detail",
                    "source_url",
                    "field_path",
                    "evidence_id",
                    "previous_digest_sha256",
                    "confirmed_digest_sha256",
                ),
            )
            if not non_empty(entry_obj.get("code")):
                _problem(problems, "restriction_code", f"{entry_path}.code", "code is empty")
            _check_enum(
                problems,
                entry_obj.get("severity"),
                f"{entry_path}.severity",
                RESTRICTION_SEVERITIES,
            )
            if not non_empty(entry_obj.get("detail")):
                _problem(problems, "restriction_detail", f"{entry_path}.detail", "detail is empty")
            if entry_obj.get("field_path") is not None and not non_empty(
                entry_obj.get("field_path")
            ):
                _problem(
                    problems,
                    "restriction_field_path",
                    f"{entry_path}.field_path",
                    "expected a non-empty field path",
                )
            if entry_obj.get("source_url") is not None and not is_http_url(
                entry_obj.get("source_url")
            ):
                _problem(
                    problems,
                    "restriction_source_url",
                    f"{entry_path}.source_url",
                    "expected an absolute HTTP URL",
                )
            evidence_id = entry_obj.get("evidence_id")
            if evidence_id is not None and evidence_id not in evidence_ids:
                _problem(
                    problems,
                    "restriction_evidence_ref",
                    f"{entry_path}.evidence_id",
                    "evidence reference is not defined",
                )
            for digest_field in ("previous_digest_sha256", "confirmed_digest_sha256"):
                digest = entry_obj.get(digest_field)
                if digest is not None and (
                    not isinstance(digest, str) or not HEX64_RE.fullmatch(digest)
                ):
                    _problem(
                        problems,
                        "restriction_digest",
                        f"{entry_path}.{digest_field}",
                        "expected 64 lowercase hex characters",
                    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def validate_registry(document: Any) -> list[Problem]:
    problems: list[Problem] = []
    if not isinstance(document, dict):
        return [Problem("type", "$", "registry document must be an object")]
    self_problem = _check_object(
        problems,
        document,
        "$",
        required=("schema_revision", "campaign_key", "records"),
        allowed=("schema_revision", "campaign_key", "records"),
    )
    if self_problem.get("schema_revision") != SCHEMA_REVISION:
        _problem(
            problems,
            "schema_revision",
            "$.schema_revision",
            f"expected {SCHEMA_REVISION}",
        )
    if self_problem.get("campaign_key") != CAMPAIGN_KEY:
        _problem(problems, "campaign_key", "$.campaign_key", f"expected {CAMPAIGN_KEY}")

    records = self_problem.get("records")
    if not isinstance(records, list):
        _problem(problems, "records", "$.records", "expected list")
        return problems

    evidence_ids: set[str] = set()
    identity_owner: dict[str, str] = {}
    registry_id_owner: dict[str, str] = {}
    for index, record in enumerate(records):
        validate_record(
            problems,
            record,
            index,
            evidence_ids,
            identity_owner,
            registry_id_owner,
        )

    order = [
        str(record.get("registry_id"))
        for record in records
        if isinstance(record, dict) and isinstance(record.get("registry_id"), str)
    ]
    if order != sorted(order):
        _problem(
            problems,
            "record_order",
            "$.records",
            "records must be sorted by registry_id for deterministic diffs",
        )

    return problems


# --------------------------------------------------------------------------------------
# Computed state: completeness, qualification, publication and rich eligibility.
# None of these are stored in the file; they are recomputed from the record every time.
# --------------------------------------------------------------------------------------


# A `missing` restriction names evidence that was never observed. These two codes say the record
# itself must not stand as a public studio, so publication reads them by code instead of by
# severity: a `review` severity describes the operator's next step, not what may be shown.
UNPUBLISHABLE_MISSING_CODES = frozenset(
    {
        "identity_or_location_unconfirmed",
        "studio_use_not_confirmed",
        "duplicate_of_published_record",
    }
)


def _unpublishable_codes(record: Mapping[str, Any]) -> list[str]:
    restrictions = record.get("restrictions") or {}
    codes = []
    for bucket in ("rejected", "conflicting"):
        for entry in restrictions.get(bucket) or []:
            if entry.get("severity") == "blocking":
                codes.append(str(entry.get("code")))
    for entry in restrictions.get("missing") or []:
        code = str(entry.get("code"))
        if code in UNPUBLISHABLE_MISSING_CODES:
            codes.append(code)
    return codes


def qualification(record: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Structural qualification: identifiable Berlin physical studio with one service."""
    reasons: list[str] = []
    identity_keys = record.get("identity_keys") or []
    if not any(key.get("authority") == "authoritative" for key in identity_keys):
        reasons.append("authoritative_identity_missing")

    decision = (record.get("subject_match") or {}).get("decision")
    if decision in (None, "undecided", "rejected"):
        reasons.append("identity_decision_missing")

    address = record.get("address") or {}
    for field in ("street", "postal_code", "city", "district_key", "country_code"):
        if not address.get(field):
            reasons.append(f"address_{field}_missing")
    if address.get("city") and address.get("city") != "Berlin":
        reasons.append("outside_berlin")
    if address.get("country_code") and address.get("country_code") != "DE":
        reasons.append("outside_germany")
    market = record.get("market") or {}
    if market.get("classification") != "primary_market":
        reasons.append("market_classification_not_primary")

    claims = [
        claim for claim in record.get("service_claims") or [] if claim.get("catalog_category_key")
    ]
    if not claims:
        reasons.append("service_claim_missing")

    reasons.extend(f"restriction:{code}" for code in _unpublishable_codes(record))
    return (not reasons, reasons)


def _approved_policy(evidence: Mapping[str, Any], domain: str) -> bool:
    if evidence.get("usage_policy") == "licensed_reuse":
        return True
    decision = evidence.get("policy_decision") or {}
    if decision.get("decision") != "approved":
        return False
    scope = str(decision.get("scope") or "").lower()
    return domain.lower() in scope or "official_site" in scope


def evidence_allows_publication(record: Mapping[str, Any], evidence: Mapping[str, Any]) -> bool:
    """Whether one evidence artifact's recorded policy authorizes publishing the facts it proves.

    The rule lives here, next to the review decisions it reads, so that a consumer cannot
    invent a second, more permissive interpretation of the same evidence.
    """
    if evidence.get("usage_policy") in {"blocked", "research_only"}:
        return False
    domain = normalize_domain((record.get("official_domains") or {}).get("primary") or "")
    return _approved_policy(evidence, domain)


def publication_blockers(record: Mapping[str, Any]) -> list[str]:
    """Basic-safe publication prerequisites that live outside this file's own state."""
    blockers = list(qualification(record)[1])

    evidence_by_id = {
        entry.get("evidence_id"): entry for entry in record.get("field_evidence") or []
    }
    domain = normalize_domain((record.get("official_domains") or {}).get("primary") or "")

    for claim in record.get("service_claims") or []:
        if not claim.get("catalog_category_key"):
            continue
        refs = claim.get("evidence_refs") or []
        if not any(
            ref in evidence_by_id and evidence_allows_publication(record, evidence_by_id[ref])
            for ref in refs
        ):
            blockers.append(f"source_policy_decision_missing:{domain}")

    address_evidence = [
        evidence
        for evidence in record.get("field_evidence") or []
        if str(evidence.get("field_path") or "").startswith("address.")
    ]
    if not any(evidence_allows_publication(record, evidence) for evidence in address_evidence):
        blockers.append("source_policy_decision_missing:address")

    address = record.get("address") or {}
    if address.get("district_basis") == "postal_code":
        blockers.append("district_derivation_not_confirmed")

    if not record.get("last_refresh", {}).get("at_utc"):
        blockers.append("freshness_not_recorded")

    descriptions = record.get("descriptions") or {}
    approved = [locale for locale, text in descriptions.items() if text.get("state") == "approved"]
    if approved and len(approved) != len(CONTENT_LOCALES):
        blockers.append("partial_approved_descriptions")

    return sorted(set(blockers))


def write_text_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    temporary.replace(path)


# --------------------------------------------------------------------------------------
# Lead import: dossier-shaped probe batches become registry records.
# --------------------------------------------------------------------------------------


def split_street_address(
    value: str | None, postal_code: str | None = None
) -> tuple[str | None, str | None]:
    """Split a captured address fragment into street name and house number.

    The fragment comes from a source page, so it can carry the postal country prefix
    ("Hammerstr. 17 D- 14167") or no space before the number ("Dunckerstr.21"). The detached
    single letter is only dropped when the record also carries the postal code that the prefix
    belonged to; without that context the letter can be a real site suffix and is kept.
    """
    if not value:
        return (None, None)
    text = " ".join(value.split())
    text = POSTAL_COUNTRY_PREFIX_RE.sub("", text)
    if postal_code:
        detached = DETACHED_COUNTRY_LETTER_RE.match(text)
        if detached:
            text = detached.group("street")
    match = STREET_NUMBER_RE.match(text)
    if not match:
        return (text, None)
    return (match.group("street").strip(), match.group("number").strip())


def _evidence_entry(
    *,
    field_path: str,
    source_url: str,
    digest: str,
    observed_at: str,
    excerpt: str,
    usage_policy: str = "facts_only",
    source_kind: str = "official_site",
    external_id: str | None = None,
    owner_id: str = "",
    policy_decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "evidence_id": derive_evidence_id(source_url, digest, field_path, owner_id),
        "field_path": field_path,
        "source_kind": source_kind,
        "source_url": source_url,
        "external_id": external_id,
        "observed_at_utc": observed_at,
        "response_digest_sha256": digest,
        "excerpt": excerpt[:400],
        "usage_policy": usage_policy,
        "policy_decision": dict(policy_decision) if policy_decision else None,
        "attribution": None,
    }


def _lead_evidence(
    lead: Mapping[str, Any],
    owner_id: str = "",
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    evidence: list[dict[str, Any]] = []
    role_refs: dict[str, list[str]] = {}
    for item in lead.get("evidence") or []:
        role = item.get("role")
        url = item.get("url")
        digest = item.get("sha256")
        observed_at = item.get("observed_at_utc")
        excerpt = item.get("excerpt") or ""
        if not (
            isinstance(url, str)
            and is_http_url(url)
            and isinstance(digest, str)
            and HEX64_RE.match(digest)
        ):
            continue
        if parse_utc(observed_at) is None:
            continue
        if role == "address":
            field_paths = ["address.street", "address.postal_code", "address.district_key"]
        else:
            field_paths = ["service_claims"]
        for field_path in field_paths:
            entry = _evidence_entry(
                field_path=field_path,
                source_url=url,
                digest=digest,
                observed_at=observed_at,
                excerpt=excerpt,
                owner_id=owner_id,
            )
            evidence.append(entry)
            role_refs.setdefault(role or "unknown", []).append(entry["evidence_id"])
    return (evidence, role_refs)


def _lead_address(lead: Mapping[str, Any]) -> dict[str, Any] | None:
    postal_code = lead.get("plz")
    street_value = lead.get("street")
    if not street_value and not postal_code:
        return None
    street, number = split_street_address(street_value, postal_code)
    return {
        "street": street,
        "street_number": number,
        "postal_code": postal_code,
        "city": "Berlin",
        "district_key": lead.get("bezirk"),
        "district_basis": lead.get("bezirk_basis") or "unknown",
        "country_code": "DE",
        "coordinate": None,
    }


def _lead_claims(
    lead: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    service_refs = [
        entry["evidence_id"] for entry in evidence if entry["field_path"] == "service_claims"
    ]
    service_excerpts = [
        entry["excerpt"] for entry in evidence if entry["field_path"] == "service_claims"
    ]
    claims: list[dict[str, Any]] = []
    restrictions: list[dict[str, Any]] = []
    seen: set[str] = set()

    for category in lead.get("categories") or []:
        if category in PLACE_CATEGORY_KEYS and category not in seen and service_refs:
            seen.add(category)
            claims.append(
                {
                    "catalog_category_key": category,
                    "evidence_refs": list(service_refs),
                    "source_text": (
                        lead.get("service") or service_excerpts[0] if service_excerpts else ""
                    )[:400],
                }
            )

    keyword = (lead.get("service_keyword") or "").strip().lower()
    if not keyword:
        for entry in lead.get("evidence") or []:
            if entry.get("role") == "service" and entry.get("keyword"):
                keyword = str(entry["keyword"]).strip().lower()
                break
    mapped = SERVICE_KEYWORD_CATEGORY.get(keyword)
    if mapped and service_refs and mapped not in seen:
        seen.add(mapped)
        claims.append(
            {
                "catalog_category_key": mapped,
                "evidence_refs": list(service_refs),
                "source_text": (service_excerpts[0] if service_excerpts else "")[:400],
            }
        )
    if not claims and keyword:
        restrictions.append(
            {
                "code": "catalog_category_missing",
                "severity": "blocking",
                "detail": (
                    f"observed service keyword {keyword!r} has no place category in the pinned "
                    "Catalog taxonomy; a category decision is required before qualification"
                ),
            }
        )
    return (claims, restrictions)


def _lead_descriptions(lead: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    descriptions: dict[str, dict[str, Any]] = {}
    for locale, value in (lead.get("locales") or {}).items():
        if locale not in CONTENT_LOCALES or not isinstance(value, dict):
            continue
        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        digest = value.get("text_digest_sha256") or value.get("sha256") or sha256_hex(text)
        origin = (
            value.get("origin") if value.get("origin") in DESCRIPTION_ORIGINS else "argos_draft"
        )
        descriptions[locale] = {
            "state": "draft",
            "text": text,
            "text_digest_sha256": digest,
            "origin": origin,
            "review_state": "pending_human",
            "review_ref": None,
            "reason": None,
        }
    return descriptions


def lead_to_record(lead: Mapping[str, Any], batch: str) -> dict[str, Any] | None:
    name = (lead.get("canonical_name") or lead.get("name") or "").strip()
    domain = (lead.get("domain") or "").strip().lower()
    if not name or not domain:
        return None

    registry_id = lead.get("registry_id")
    if not (isinstance(registry_id, str) and _is_uuid(registry_id)):
        registry_id = derive_registry_id("official_domain", domain)

    evidence, _role_refs = _lead_evidence(lead, registry_id)
    claims, restrictions = _lead_claims(lead, evidence)

    robots = lead.get("robots") or {}
    if robots.get("status") == 200 and isinstance(robots.get("sha256"), str):
        robots_excerpt = (
            "robots.txt status 200, disallow_all_for_star="
            f"{str(bool(robots.get('disallow_all_for_star'))).lower()}, "
            f"crawl_delay_seconds={robots.get('crawl_delay_seconds')}"
        )
        evidence.append(
            _evidence_entry(
                field_path="source.robots",
                source_url=robots.get("url") or f"https://{domain}/robots.txt",
                digest=robots["sha256"],
                observed_at=(evidence[0]["observed_at_utc"] if evidence else now_utc()),
                excerpt=robots_excerpt,
                owner_id=registry_id,
            )
        )

    address = _lead_address(lead)
    if address and address.get("district_basis") == "postal_code":
        restrictions.append(
            {
                "code": "district_derivation_unconfirmed",
                "severity": "review",
                "detail": (
                    "district_key is derived from the postal code and needs operator confirmation"
                ),
            }
        )
    restrictions.append(
        {
            "code": "market_classification_unconfirmed",
            "severity": "review",
            "detail": (
                "primary market classification is campaign-level and needs operator confirmation"
            ),
        }
    )
    restrictions.append(
        {
            "code": "official_contacts_missing",
            "severity": "info",
            "detail": "no typed contact target was observed in this import batch",
        }
    )
    restrictions.append(
        {
            "code": "media_rights_unclear",
            "severity": "info",
            "detail": (
                "no media candidate was imported; site terms forbid reuse without written consent"
            ),
        }
    )

    decision_map = {
        "LinkExisting": "link_existing",
        "CreateNewPendingReview": "create_new_pending_review",
    }
    decision = decision_map.get(str(lead.get("identityAction")), "undecided")
    if decision == "link_existing" and not lead.get("existingListingId"):
        decision = "undecided"

    usage_constraints = lead.get("usage_constraints") or {}
    if usage_constraints.get("text_reuse") == "forbidden_without_written_permission":
        restrictions.append(
            {
                "code": "site_text_reuse_forbidden",
                "severity": "info",
                "detail": (
                    "source terms forbid text reuse; only independently written facts "
                    "may be published"
                ),
            }
        )

    observed_times = [
        entry["observed_at_utc"]
        for entry in evidence
        if isinstance(entry.get("observed_at_utc"), str)
    ]
    return {
        "registry_id": registry_id,
        "entity_kind": "place",
        "canonical_name": name,
        "aliases": list(lead.get("aliases") or []),
        "identity_keys": [
            {"kind": "official_domain", "value": domain, "authority": "authoritative"},
        ],
        "subject_match": {
            "decision": decision,
            "reason": (
                "identity decision carried over from the probe batch"
                if decision != "undecided"
                else "no reviewed identity decision recorded yet"
            ),
            "subject_id": None,
            "listing_id": lead.get("existingListingId"),
        },
        "possible_duplicate_of": [],
        "official_domains": {"primary": domain, "alternates": []},
        "source_original_name": lead.get("source_name") or name,
        "address": address,
        "market": {
            "classification": "primary_market",
            "basis": "campaign_geography_revision:berlin_official_boundary_v1",
            "source_kind": "manual_operator",
        },
        "service_claims": claims,
        "descriptions": _lead_descriptions(lead),
        "attributes": [],
        "contacts": [],
        "photo_candidates": [],
        "field_evidence": evidence,
        "reviews": [],
        "collector": {"candidate_ids": [], "revisions": [], "import_batch": batch},
        "last_refresh": {
            "status": "observed" if evidence else "not_attempted",
            "at_utc": max(observed_times) if observed_times else None,
        },
        "restrictions": {"rejected": [], "missing": [], "conflicting": restrictions},
    }


def _identity_index(records: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for record in records:
        for key in record.get("identity_keys") or []:
            kind = key.get("kind")
            value = key.get("value")
            if isinstance(kind, str) and isinstance(value, str):
                index.setdefault(registry_identity_key(kind, value), record)
    return index


def merge_lead_into_record(
    existing: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge without overwriting human decisions or reviewed content."""
    merged: dict[str, Any] = json.loads(json.dumps(existing))
    for field in ("canonical_name", "source_original_name", "address", "market"):
        if merged.get(field) in (None, {}, "") and incoming.get(field):
            merged[field] = incoming[field]

    known_evidence = {entry.get("evidence_id") for entry in merged.get("field_evidence") or []}
    disagreements = []
    for entry in incoming.get("field_evidence") or []:
        if entry.get("evidence_id") in known_evidence:
            continue
        same_field = [
            known
            for known in merged.get("field_evidence") or []
            if known.get("field_path") == entry.get("field_path")
            and known.get("source_url") == entry.get("source_url")
        ]
        if same_field and same_field[0].get("response_digest_sha256") != entry.get(
            "response_digest_sha256"
        ):
            disagreements.append(
                {
                    "code": "evidence_digest_changed",
                    "severity": "review",
                    "detail": (
                        f"new digest for {entry.get('source_url')} on {entry.get('field_path')}"
                    ),
                    # The conflict names the exact artifact it is about, so a later
                    # re-verification can confirm this observation instead of clearing
                    # every digest conflict of the record at once.
                    "source_url": entry.get("source_url"),
                    "field_path": entry.get("field_path"),
                    "evidence_id": entry.get("evidence_id"),
                    "previous_digest_sha256": same_field[0].get("response_digest_sha256"),
                    "confirmed_digest_sha256": entry.get("response_digest_sha256"),
                }
            )
        merged.setdefault("field_evidence", []).append(entry)

    known_claims = {
        claim.get("catalog_category_key")
        for claim in merged.get("service_claims") or []
        if claim.get("catalog_category_key")
    }
    for claim in incoming.get("service_claims") or []:
        if claim.get("catalog_category_key") in known_claims:
            continue
        merged.setdefault("service_claims", []).append(claim)

    # `catalog_category_missing` states a fact about the pinned taxonomy, not a human
    # decision: once the current mapping classifies the service, keeping the old blocker
    # would permanently exclude an already qualified record.
    if merged.get("service_claims"):
        conflicting = merged.get("restrictions", {}).get("conflicting") or []
        merged.setdefault("restrictions", {})["conflicting"] = [
            restriction
            for restriction in conflicting
            if restriction.get("code") != "catalog_category_missing"
        ]

    for locale, description in (incoming.get("descriptions") or {}).items():
        current = (merged.get("descriptions") or {}).get(str(locale))
        if current and current.get("state") in ("draft", "approved"):
            if current.get("text_digest_sha256") != description.get("text_digest_sha256"):
                disagreements.append(
                    {
                        "code": "description_digest_changed",
                        "severity": "review",
                        "detail": f"new {locale} draft text conflicts with the recorded text",
                    }
                )
            continue
        merged.setdefault("descriptions", {})[locale] = description

    if disagreements:
        merged.setdefault("restrictions", {}).setdefault("conflicting", []).extend(disagreements)
    if not merged.get("last_refresh", {}).get("at_utc") and incoming.get("last_refresh", {}).get(
        "at_utc"
    ):
        merged["last_refresh"] = incoming["last_refresh"]
    return merged


def load_lead_documents(paths: Sequence[Path]) -> list[dict[str, Any]]:
    leads: list[dict[str, Any]] = []
    for path in paths:
        document = read_json(path)
        if isinstance(document, list):
            leads.extend(document)
        elif isinstance(document, dict) and isinstance(document.get("records"), list):
            leads.extend(document["records"])
        else:
            raise ValueError(f"{path}: expected a list of studio leads or a registry document")
    return leads


def import_leads(
    registry_path: Path,
    leads: Sequence[Mapping[str, Any]],
    batch: str,
    *,
    dry_run: bool,
) -> int:
    document = (
        read_json(registry_path)
        if registry_path.exists()
        else {"schema_revision": SCHEMA_REVISION, "campaign_key": CAMPAIGN_KEY, "records": []}
    )
    records = list(document.get("records") or [])
    index = _identity_index(records)

    added = 0
    merged = 0
    skipped = 0
    for lead in leads:
        record = lead_to_record(lead, batch)
        if record is None:
            skipped += 1
            continue
        existing = None
        for key in record["identity_keys"]:
            existing = index.get(registry_identity_key(key["kind"], key["value"]))
            if existing is not None:
                break
        if existing is None:
            records.append(record)
            for key in record["identity_keys"]:
                index[registry_identity_key(key["kind"], key["value"])] = record
            added += 1
        else:
            records[records.index(existing)] = merge_lead_into_record(existing, record)
            index = _identity_index(records)
            merged += 1

    records.sort(key=lambda record: record["registry_id"])
    document["records"] = records
    problems = validate_registry(document)
    for problem in problems:
        print(problem.render(), file=sys.stderr)
    print(
        f"leads={len(leads)} added={added} merged={merged} skipped={skipped} "
        f"records={len(records)} problems={len(problems)}"
    )
    if problems:
        return 1
    if dry_run:
        print("dry run: registry was not written")
        return 0
    write_text_atomic(registry_path, canonical_json(document))
    return 0


# --------------------------------------------------------------------------------------
# Enrichment patches: the only way a human adds observed facts after import.
# --------------------------------------------------------------------------------------


PATCHABLE_PREFIXES = (
    "address.",
    "official_domains.",
    "service_claims",
    "contacts",
    "descriptions.",
    "attributes",
    "restrictions.missing",
    "canonical_name",
    "aliases",
)

# A rename changes what the record is called, not which organization it is: it needs the
# organization's own wording as evidence and an explicit operator decision, and the old name
# becomes an alias only when the organization really used it.
RENAME_FIELD_PATHS = ("canonical_name", "aliases")


def _set_by_path(document: dict[str, Any], field_path: str, value: Any) -> None:
    parts = field_path.split(".")
    current = document
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def _value_at_path(document: Mapping[str, Any], field_path: str) -> Any:
    current: Any = document
    for part in field_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _absent(value: Any) -> bool:
    return value is None or value == {} or value == [] or value == ""


def _conflict_resolution_details(
    entry: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    field_path: str,
    value: Any,
    current_value: Any,
    expected_record_digest: str,
) -> dict[str, Any] | Problem | None:
    resolution = entry.get("conflict_resolution")
    if resolution is None:
        return None
    if not isinstance(resolution, Mapping):
        return Problem("patch_resolution", "conflict_resolution", "expected object")
    conflict_id = resolution.get("conflict_id")
    scope = resolution.get("scope")
    if resolution.get("field_path") != field_path:
        return Problem(
            "patch_resolution_field", "conflict_resolution.field_path", "does not match patch field"
        )
    baseline = resolution.get("baseline_record_digest_sha256")
    refs = resolution.get("evidence_refs")
    if not isinstance(scope, str) or not scope:
        return Problem("patch_resolution_scope", "conflict_resolution.scope", "scope is required")
    expected_conflict_id = enrichment_conflict_id(str(record.get("registry_id")), field_path, scope)
    if conflict_id != expected_conflict_id:
        return Problem(
            "patch_resolution_id",
            "conflict_resolution.conflict_id",
            "does not match subject/field/scope",
        )
    if canonical_bytes(resolution.get("accepted_value")) != canonical_bytes(value):
        return Problem(
            "patch_resolution_value",
            "conflict_resolution.accepted_value",
            "must exactly match the patch value",
        )
    if not isinstance(refs, list) or not refs or refs != entry.get("evidence_refs"):
        return Problem(
            "patch_resolution_evidence",
            "conflict_resolution.evidence_refs",
            "must exactly match the patch evidence_refs",
        )
    evidence_by_id = {
        evidence.get("evidence_id"): evidence
        for evidence in record.get("field_evidence", [])
        if isinstance(evidence, Mapping)
    }
    evidence = [evidence_by_id.get(ref) for ref in refs]
    if any(
        not isinstance(item, Mapping) or item.get("field_path") != field_path for item in evidence
    ):
        return Problem(
            "patch_resolution_evidence",
            "conflict_resolution.evidence_refs",
            "must reference canonical evidence for the exact field",
        )
    digests = sorted(str(item.get("response_digest_sha256")) for item in evidence)
    source_digest = sha256_hex(canonical_json(digests))
    text_digest = enrichment_value_digest(field_path, scope, value)
    marker = f"studio-enrichment-resolution:{conflict_id}:{baseline}"
    value_present = (
        isinstance(current_value, list)
        and isinstance(value, Mapping)
        and any(repeatable_item_key(item) == repeatable_item_key(value) for item in current_value)
        if field_path == "contacts"
        else canonical_bytes(current_value) == canonical_bytes(value)
    )
    already_recorded = value_present and any(
        isinstance(review, Mapping)
        and review.get("decision") == "approved"
        and review.get("kind") in {"fact", "identity"}
        and review.get("text_digest_sha256") == text_digest
        and review.get("source_evidence_digest_sha256") == source_digest
        and review.get("evidence_refs") == refs
        and marker in str(review.get("note", ""))
        for review in record.get("reviews", [])
    )
    if not already_recorded and baseline != expected_record_digest:
        return Problem(
            "patch_resolution_stale",
            "conflict_resolution.baseline_record_digest_sha256",
            "canonical record changed since the conflict was reviewed",
        )
    if parse_utc(entry.get("decided_at_utc")) is None:
        return Problem(
            "patch_resolution_time",
            "decided_at_utc",
            "a conflict resolution requires an exact UTC decision time",
        )
    return {
        "conflict_id": conflict_id,
        "scope": scope,
        "baseline": baseline,
        "evidence_refs": refs,
        "text_digest": text_digest,
        "source_digest": source_digest,
        "already_recorded": already_recorded,
    }


def _prepare_contact_addition(
    record: Mapping[str, Any],
    value: Any,
    evidence_refs: Sequence[Any],
    *,
    baseline_digest: str | None,
    expected_record_digest: str,
) -> list[dict[str, Any]] | Problem:
    if not isinstance(value, Mapping):
        return Problem("patch_collection_item", "value", "contact addition must be an object")
    if value.get("validation_state") != "unvalidated":
        return Problem(
            "patch_collection_review",
            "value.validation_state",
            "new contacts remain unvalidated until the human review path approves them",
        )
    current = record.get("contacts")
    if not isinstance(current, list):
        return Problem("patch_collection_shape", "contacts", "canonical contacts must be a list")
    known_evidence = {
        item.get("evidence_id"): item
        for item in record.get("field_evidence") or []
        if isinstance(item, Mapping)
    }
    refs = list(dict.fromkeys([*(value.get("evidence_refs") or []), *evidence_refs]))
    if not refs or any(
        not isinstance(ref, str)
        or ref not in known_evidence
        or known_evidence[ref].get("field_path") != "contacts"
        for ref in refs
    ):
        return Problem(
            "patch_collection_evidence",
            "evidence_refs",
            "contact additions require canonical evidence for contacts",
        )
    candidate = dict(value)
    candidate["evidence_refs"] = refs
    key = repeatable_item_key(candidate)
    matching = [index for index, item in enumerate(current) if repeatable_item_key(item) == key]
    if len(matching) > 1:
        return Problem(
            "patch_collection_duplicate",
            "contacts",
            "canonical collection contains duplicate semantic contacts",
        )
    if matching and set(refs) <= set(current[matching[0]].get("evidence_refs") or []):
        return [dict(item) for item in current]
    if not isinstance(baseline_digest, str) or not HEX64_RE.fullmatch(baseline_digest):
        return Problem(
            "patch_collection_baseline_required",
            "baseline_record_digest_sha256",
            "append-only collection patches require a canonical baseline",
        )
    if baseline_digest != expected_record_digest:
        return Problem(
            "patch_collection_stale",
            "baseline_record_digest_sha256",
            "canonical record changed since the collection addition was reviewed",
        )
    updated = [dict(item) for item in current]
    if matching:
        index = matching[0]
        updated[index]["evidence_refs"] = list(
            dict.fromkeys([*(updated[index].get("evidence_refs") or []), *refs])
        )
    else:
        updated.append(candidate)
    return updated


class PatchOutcome:
    """What one patch did to the registry: written, already equal, or refused."""

    def __init__(self) -> None:
        self.problems: list[Problem] = []
        self.applied = 0
        self.unchanged = 0

    def extend(self, problems: list[Problem]) -> None:
        self.problems.extend(problems)


def apply_patch(document: dict[str, Any], patch: Mapping[str, Any]) -> PatchOutcome:
    """Apply an evidence-backed patch file record by record.

    A patch is a proposal, so it may only add a fact the record does not carry yet. Re-applying
    the same patch writes nothing, and a value that already differs on the record is a conflict
    for the owner of that field instead of being overwritten by whoever ran the patch last.
    """
    outcome = PatchOutcome()
    problems = outcome.problems
    if patch.get("schema_revision") != PATCH_SCHEMA_REVISION:
        problems.append(
            Problem("patch_schema", "$.schema_revision", f"expected {PATCH_SCHEMA_REVISION}")
        )
        return outcome

    by_id = {record.get("registry_id"): record for record in document.get("records") or []}
    initial_digests = {
        registry_id: registry_record_digest(record) for registry_id, record in by_id.items()
    }
    for index, entry in enumerate(patch.get("patches") or []):
        path = f"patches[{index}]"
        if not isinstance(entry, dict):
            problems.append(Problem("patch_shape", path, "expected object"))
            continue
        registry_id = entry.get("registry_id")
        field_path = entry.get("field_path")
        reviewer = entry.get("reviewer")
        evidence_refs = entry.get("evidence_refs") or []
        if "replace_existing" in entry and not isinstance(entry["replace_existing"], bool):
            problems.append(
                Problem("patch_replace_flag", f"{path}.replace_existing", "expected boolean")
            )
            continue

        record = by_id.get(registry_id)
        if record is None:
            problems.append(Problem("patch_record", f"{path}.registry_id", "unknown registry id"))
            continue
        if not isinstance(field_path, str) or not any(
            field_path == prefix or field_path.startswith(prefix) for prefix in PATCHABLE_PREFIXES
        ):
            problems.append(Problem("patch_field", f"{path}.field_path", "field is not patchable"))
            continue
        if not non_empty(reviewer):
            problems.append(Problem("patch_reviewer", f"{path}.reviewer", "reviewer is required"))
            continue
        operation = entry.get("operation", "set_value")
        if operation not in {"set_value", "add_collection_item"}:
            problems.append(
                Problem("patch_operation", f"{path}.operation", "unsupported operation")
            )
            continue
        if operation == "add_collection_item" and field_path != "contacts":
            problems.append(
                Problem(
                    "patch_operation_field",
                    f"{path}.field_path",
                    "add_collection_item currently supports the contacts collection",
                )
            )
            continue
        if field_path == "restrictions.missing":
            if not isinstance(entry.get("value"), list):
                problems.append(
                    Problem(
                        "patch_value",
                        f"{path}.value",
                        "restrictions.missing is the full list the record keeps",
                    )
                )
                continue
            if parse_utc(entry.get("decided_at_utc")) is None:
                problems.append(
                    Problem(
                        "patch_restriction_time",
                        f"{path}.decided_at_utc",
                        "a restriction decision records when it was made",
                    )
                )
                continue
            if not non_empty(entry.get("note")):
                problems.append(
                    Problem(
                        "patch_restriction_reason",
                        f"{path}.note",
                        "a restriction decision states the confirmed reason",
                    )
                )
                continue
        known_evidence = {
            item.get("evidence_id"): item for item in record.get("field_evidence") or []
        }
        if field_path.startswith(
            ("address.", "contacts", "service_claims", "attributes", "canonical_name", "aliases")
        ):
            if not evidence_refs:
                problems.append(
                    Problem(
                        "patch_evidence", f"{path}.evidence_refs", "observed facts need evidence"
                    )
                )
                continue
            unknown_refs = [ref for ref in evidence_refs if ref not in known_evidence]
            if unknown_refs:
                problems.extend(
                    Problem(
                        "patch_evidence_unknown",
                        f"{path}.evidence_refs",
                        f"unknown evidence {ref}",
                    )
                    for ref in unknown_refs
                )
                continue
            if field_path in RENAME_FIELD_PATHS and not any(
                (known_evidence.get(ref) or {}).get("source_kind") == "official_site"
                for ref in evidence_refs
            ):
                problems.append(
                    Problem(
                        "patch_rename_evidence",
                        f"{path}.evidence_refs",
                        "a name is confirmed by the organization's own site, not a directory",
                    )
                )
                continue
            if field_path == "canonical_name" and not isinstance(
                entry.get("previous_name_used_by_organization"), bool
            ):
                problems.append(
                    Problem(
                        "patch_rename_alias",
                        f"{path}.previous_name_used_by_organization",
                        "state whether the organization used the previous name",
                    )
                )
                continue
        if field_path.startswith("descriptions."):
            value = entry.get("value")
            if not isinstance(value, dict) or value.get("state") == "approved":
                problems.append(
                    Problem(
                        "patch_self_review",
                        f"{path}.value",
                        "descriptions cannot be self-approved",
                    )
                )
                continue

        incoming = entry.get("value")
        resolution_value = incoming
        current = _value_at_path(record, field_path)
        if operation == "add_collection_item":
            prepared = _prepare_contact_addition(
                record,
                incoming,
                evidence_refs,
                baseline_digest=entry.get("baseline_record_digest_sha256"),
                expected_record_digest=initial_digests[registry_id],
            )
            if isinstance(prepared, Problem):
                problems.append(Problem(prepared.code, f"{path}.{prepared.path}", prepared.message))
                continue
            incoming = prepared
        if field_path in RENAME_FIELD_PATHS:
            rename_problem = _rename_problem(entry, field_path, record, incoming, current)
            if rename_problem is not None:
                problems.append(rename_problem)
                continue
        resolution_details = _conflict_resolution_details(
            entry,
            record,
            field_path=field_path,
            value=resolution_value,
            current_value=current,
            expected_record_digest=initial_digests[registry_id],
        )
        if isinstance(resolution_details, Problem):
            problems.append(resolution_details)
            continue
        if resolution_details and resolution_details["already_recorded"]:
            outcome.unchanged += 1
            continue
        unchanged_value = canonical_json(current) == canonical_json(incoming)
        if unchanged_value and resolution_details is None:
            outcome.unchanged += 1
            continue
        replacement = (
            operation != "add_collection_item" and not _absent(current) and not unchanged_value
        )
        if replacement and resolution_details is None:
            if field_path == "aliases":
                pass
            elif field_path in {"canonical_name", "restrictions.missing"}:
                baseline = entry.get("baseline_record_digest_sha256")
                if not isinstance(baseline, str):
                    problems.append(
                        Problem(
                            "patch_replace_baseline_required",
                            f"{path}.baseline_record_digest_sha256",
                            "replacing an existing value requires its reviewed baseline",
                        )
                    )
                    continue
                if baseline != initial_digests[registry_id]:
                    problems.append(
                        Problem(
                            "patch_replace_stale",
                            f"{path}.baseline_record_digest_sha256",
                            "canonical record changed since the patch was reviewed",
                        )
                    )
                    continue
            else:
                problems.append(
                    Problem(
                        "patch_resolution_required",
                        f"{path}.conflict_resolution",
                        "replacing an accepted fact requires a baseline-bound conflict resolution",
                    )
                )
                continue
        # A rename still records identity history; restrictions have their dedicated fact review.
        if (
            operation != "add_collection_item"
            and field_path not in {"canonical_name", "aliases"}
            and not _absent(current)
            and not unchanged_value
            and not entry.get("replace_existing")
        ):
            problems.append(
                Problem(
                    "patch_conflict",
                    f"{path}.field_path",
                    f"record already carries {field_path} "
                    f"(digest {sha256_hex(canonical_json(current))[:12]}, "
                    f"incoming {sha256_hex(canonical_json(incoming))[:12]}); "
                    "replacing it needs the field owner",
                )
            )
            continue
        if not unchanged_value:
            _set_by_path(record, field_path, incoming)
        if field_path == "canonical_name" and not unchanged_value:
            _record_rename(record, entry, previous=current, reviewer=str(reviewer))
        if field_path == "restrictions.missing" and not unchanged_value:
            _record_restriction_decision(record, entry, previous=current, reviewer=str(reviewer))
        if resolution_details is not None:
            resolution_note = (
                f"studio-enrichment-resolution:{resolution_details['conflict_id']}"
                f":{resolution_details['baseline']}; "
                f"field={field_path}; evidence={','.join(resolution_details['evidence_refs'])}"
            )
            _append_review(
                record,
                kind=(
                    "identity"
                    if field_path in {"canonical_name", "aliases", "official_domains.primary"}
                    else "fact"
                ),
                decision="approved",
                actor_id=str(reviewer),
                decided_at_utc=str(entry["decided_at_utc"]),
                text_digest=resolution_details["text_digest"],
                source_digest=resolution_details["source_digest"],
                note=resolution_note,
                evidence_refs=resolution_details["evidence_refs"],
            )
        record.setdefault("collector", {}).setdefault("revisions", []).append(
            f"patch:{field_path}:{reviewer}"
        )
        outcome.applied += 1
    return outcome


def _rename_problem(
    entry: Mapping[str, Any],
    field_path: str,
    record: Mapping[str, Any],
    incoming: Any,
    current: Any,
) -> Problem | None:
    """Reject rename shapes that would edit identity instead of recording a new name."""
    if field_path == "canonical_name":
        if not non_empty(incoming) or not isinstance(incoming, str):
            return Problem("patch_value", "value", "a canonical name is a non-empty string")
        if not parse_utc(entry.get("decided_at_utc")):
            return Problem(
                "patch_rename_time",
                "decided_at_utc",
                "a rename records when the operator decided it (YYYY-MM-DDTHH:MM:SSZ)",
            )
        return None
    if not isinstance(incoming, list) or any(
        not isinstance(alias, str) or not alias.strip() for alias in incoming
    ):
        return Problem("patch_value", "value", "aliases are a list of non-empty strings")
    removed = [alias for alias in current or [] if alias not in incoming]
    if removed:
        return Problem(
            "patch_alias_removal",
            "value",
            f"aliases are append-only; removing {removed} drops recorded name history",
        )
    return None


def _record_rename(
    record: dict[str, Any], entry: Mapping[str, Any], *, previous: Any, reviewer: str
) -> None:
    """Keep the old name as an alias when the organization used it, and log the decision."""
    new_name = record.get("canonical_name")
    if entry.get("previous_name_used_by_organization") is True and non_empty(previous):
        aliases = record.setdefault("aliases", [])
        if previous not in aliases:
            aliases.append(previous)
    digests: list[str] = []
    for ref in entry.get("evidence_refs") or []:
        for item in record.get("field_evidence") or []:
            if item.get("evidence_id") == ref:
                digests.append(str(item.get("sha256") or ""))
    _append_review(
        record,
        kind="identity",
        decision="approved",
        actor_id=reviewer,
        decided_at_utc=str(entry["decided_at_utc"]),
        text_digest=hashlib.sha256(
            canonical_json({"canonical_name": new_name, "previous_name": previous}).encode("utf-8")
        ).hexdigest(),
        source_digest=hashlib.sha256(canonical_json(sorted(digests)).encode("utf-8")).hexdigest(),
        note=(
            f"canonical name changed from {previous!r} to {new_name!r}; "
            f"evidence={','.join(entry.get('evidence_refs') or [])}; "
            "previous name used by the organization="
            f"{entry.get('previous_name_used_by_organization')}; "
            f"source_original_name={record.get('source_original_name')!r} kept"
        ),
    )


def _record_restriction_decision(
    record: dict[str, Any], entry: Mapping[str, Any], *, previous: Any, reviewer: str
) -> None:
    """Keep the reviewer's reason and evidence for every change of the review restrictions."""
    before = [item for item in previous or [] if isinstance(item, Mapping)]
    after = list(record.get("restrictions", {}).get("missing") or [])
    before_codes = sorted(str(item.get("code")) for item in before)
    after_codes = sorted(str(item.get("code")) for item in after)
    digests: list[str] = []
    for ref in entry.get("evidence_refs") or []:
        for item in record.get("field_evidence") or []:
            if item.get("evidence_id") == ref:
                digests.append(str(item.get("sha256") or ""))
    _append_review(
        record,
        kind="fact",
        decision="approved",
        actor_id=reviewer,
        decided_at_utc=str(entry.get("decided_at_utc") or ""),
        text_digest=hashlib.sha256(
            canonical_json({"restrictions_missing_before": before, "after": after}).encode("utf-8")
        ).hexdigest(),
        source_digest=hashlib.sha256(canonical_json(sorted(digests)).encode("utf-8")).hexdigest(),
        note=(
            f"restrictions.missing changed by patch: {','.join(before_codes) or 'none'} -> "
            f"{','.join(after_codes) or 'none'}; "
            f"evidence={','.join(entry.get('evidence_refs') or [])}; reason={entry.get('note')}"
        ),
    )


def enrich(registry_path: Path, patch_path: Path, *, dry_run: bool) -> int:
    document = read_json(registry_path)
    patch = read_json(patch_path)
    outcome = apply_patch(document, patch)
    outcome.extend(validate_registry(document))
    for problem in outcome.problems:
        print(problem.render(), file=sys.stderr)
    if outcome.problems:
        return 1
    if dry_run:
        print(
            f"dry run: {outcome.applied} field(s) would change, "
            f"{outcome.unchanged} already equal, {len(document['records'])} record(s)"
        )
        return 0
    if outcome.applied == 0:
        print(f"nothing to write: {outcome.unchanged} field(s) already carry the patched value")
        return 0
    write_text_atomic(registry_path, canonical_json(document))
    print(
        f"applied {outcome.applied} field(s) to {len(document['records'])} record(s), "
        f"{outcome.unchanged} already equal"
    )
    return 0


def evidence_id_for(owner_id: str, source_url: str, digest: str, field_path: str) -> str:
    """Stable identity of one record's observed artifact for one field.

    The record is part of the seed: two records may cite the same page, and each of them owns its
    own observation of it.
    """
    seed = f"{owner_id}\n{source_url}\n{digest}\n{field_path}".encode()
    return f"ev-{hashlib.sha256(seed).hexdigest()[:16]}"


def record_evidence(registry_path: Path, capture_path: Path, *, dry_run: bool) -> int:
    """Append captured official-site artifacts to the records they were captured for.

    A capture entry becomes one artifact per declared field path. Nothing is decided here: the
    artifact carries no policy decision until an operator records one, and no description state or
    contact validation state changes. Re-recording the same URL and digest is a no-op.
    """
    document = read_json(registry_path)
    capture = read_json(capture_path)
    entries = capture.get("entries") if isinstance(capture, Mapping) else None
    if not isinstance(entries, list) or not entries:
        print("capture manifest carries no entries", file=sys.stderr)
        return 2
    by_id = {record["registry_id"]: record for record in document.get("records") or []}
    appended = 0
    already = 0
    refused: list[str] = []
    for entry in entries:
        if entry.get("state") != "captured":
            refused.append(f"{entry.get('registryId')} {entry.get('url')}: {entry.get('code')}")
            continue
        record = by_id.get(entry.get("registryId"))
        if record is None:
            refused.append(f"unknown registry id {entry.get('registryId')}")
            continue
        source_url = str(entry["url"])
        digest = str(entry["responseDigestSha256"])
        observed_at = str(entry["observedAtUtc"])
        excerpt = str(entry["excerpt"])
        field_paths = [str(item) for item in entry.get("fieldPaths") or []] or ["source.page"]
        artifacts = record.setdefault("field_evidence", [])
        for field_path in field_paths:
            evidence_id = evidence_id_for(
                str(record["registry_id"]), source_url, digest, field_path
            )
            if any(
                item.get("source_url") == source_url
                and item.get("response_digest_sha256") == digest
                and item.get("field_path") == field_path
                for item in artifacts
            ):
                already += 1
                continue
            artifacts.append(
                {
                    "attribution": None,
                    "evidence_id": evidence_id,
                    "excerpt": excerpt,
                    **({"extraction": entry["extraction"]} if entry.get("extraction") else {}),
                    "external_id": None,
                    "field_path": field_path,
                    "observed_at_utc": observed_at,
                    "policy_decision": None,
                    "response_digest_sha256": digest,
                    "source_kind": str(capture.get("sourceKey") or "official_site"),
                    "source_url": source_url,
                    "usage_policy": "facts_only",
                }
            )
            appended += 1
    problems = validate_registry(document)
    for problem in problems:
        print(problem.render(), file=sys.stderr)
    for message in refused:
        print(f"refused {message}", file=sys.stderr)
    if problems:
        return 1
    if dry_run:
        print(f"dry run: would append {appended} artifact(s), {already} already present")
        return 0
    if appended == 0:
        print(f"nothing to write: {already} artifact(s) already present")
        return 0
    write_text_atomic(registry_path, canonical_json(document))
    print(f"appended {appended} artifact(s), {already} already present, {len(refused)} refused")
    return 0


# --------------------------------------------------------------------------------------
# Reconcile, diff, report and CLI.
# --------------------------------------------------------------------------------------


def _address_signature(record: Mapping[str, Any]) -> str | None:
    address = record.get("address") or {}
    street = (address.get("street") or "").strip().lower()
    number = (address.get("street_number") or "").strip().lower()
    postal_code = (address.get("postal_code") or "").strip()
    if not street or not postal_code:
        return None
    return f"{street}|{number}|{postal_code}"


def reconcile_report(document: Mapping[str, Any]) -> list[str]:
    records = document.get("records") or []
    findings: list[str] = []

    domains: dict[str, list[str]] = {}
    addresses: dict[str, list[str]] = {}
    for record in records:
        domain = (record.get("official_domains") or {}).get("primary")
        if isinstance(domain, str) and domain:
            domains.setdefault(domain, []).append(record["registry_id"])
        signature = _address_signature(record)
        if signature:
            addresses.setdefault(signature, []).append(record["registry_id"])

    for domain, ids in sorted(domains.items()):
        if len(ids) > 1:
            findings.append(f"same_domain {domain}: {', '.join(sorted(ids))}")
    for signature, ids in sorted(addresses.items()):
        if len(ids) > 1:
            findings.append(f"same_address {signature}: {', '.join(sorted(ids))}")
    if not findings:
        findings.append("no duplicate domain or address candidates")
    return findings


def diff_documents(current: Mapping[str, Any], other: Mapping[str, Any]) -> list[str]:
    current_records = {record["registry_id"]: record for record in current.get("records") or []}
    other_records = {record["registry_id"]: record for record in other.get("records") or []}
    lines: list[str] = []
    for registry_id in sorted(set(other_records) - set(current_records)):
        lines.append(f"+ {registry_id} {other_records[registry_id].get('canonical_name')}")
    for registry_id in sorted(set(current_records) - set(other_records)):
        lines.append(f"- {registry_id} {current_records[registry_id].get('canonical_name')}")
    for registry_id in sorted(set(current_records) & set(other_records)):
        changed = [
            key
            for key in sorted(set(current_records[registry_id]) | set(other_records[registry_id]))
            if current_records[registry_id].get(key) != other_records[registry_id].get(key)
        ]
        if changed:
            lines.append(f"~ {registry_id} {', '.join(changed)}")
    return lines or ["no differences"]


def report_lines(document: Mapping[str, Any]) -> list[str]:
    records = document.get("records") or []
    qualified = 0
    publishable = 0
    for record in records:
        qualified += 0 if qualification(record)[0] else 1
        publishable += 1 if not publication_blockers(record) else 0
    return [
        f"records={len(records)}",
        f"qualified={len(records) - qualified} publishable={publishable}",
    ]


DECIDED_IDENTITY_ACTIONS = ("create_new_pending_review", "link_existing")

# Re-verification of a changed artifact: an operator confirms that the new digest is a new
# observation of the same fact. Both digests stay recorded and the old artifact stays in place.
REVERIFY_SCHEMA_REVISION = "registry-reverify-v1"


def _matching_digest_restriction(
    record: Mapping[str, Any], entry: Mapping[str, Any], evidence: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """The recorded digest conflict this re-verification is about, if it exists."""
    conflicts: list[Mapping[str, Any]] = (record.get("restrictions") or {}).get("conflicting") or []
    for restriction in conflicts:
        if restriction.get("code") != "evidence_digest_changed":
            continue
        if restriction.get("evidence_id"):
            if restriction.get("evidence_id") == evidence.get("evidence_id") and restriction.get(
                "previous_digest_sha256"
            ) == entry.get("previous_digest_sha256"):
                return restriction
            continue
        # Older conflicts carry only their description; it still names url and field.
        if restriction.get("detail") == (
            f"new digest for {evidence.get('source_url')} on {evidence.get('field_path')}"
        ):
            return restriction
    return None


def reverify_evidence(
    document: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    reviewer: str,
    decided_at_utc: str,
) -> dict[str, int]:
    """Confirm one changed artifact and clear only the conflict it caused.

    The confirmed observation must already be recorded on the record, its digest must be the
    confirmed one, and the previous digest must describe another recorded artifact of the same
    url and field: a re-verification recomputes nothing and invents no evidence.
    """
    if payload.get("schema_revision") != REVERIFY_SCHEMA_REVISION:
        raise ValueError(f"expected schema_revision {REVERIFY_SCHEMA_REVISION}")
    by_id = {record.get("registry_id"): record for record in document.get("records") or []}
    counts = {"reverified": 0, "refused": 0}
    for index, entry in enumerate(payload.get("decisions") or []):
        path = f"decisions[{index}]"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{path}: expected an object")
        record = by_id.get(entry.get("registry_id"))
        if record is None:
            raise ValueError(f"{path}: unknown registry id {entry.get('registry_id')!r}")
        evidence = next(
            (
                item
                for item in record.get("field_evidence") or []
                if item.get("evidence_id") == entry.get("evidence_id")
            ),
            None,
        )
        if evidence is None:
            raise ValueError(f"{path}: no recorded evidence {entry.get('evidence_id')!r}")
        if evidence.get("field_path") != entry.get("field_path") or evidence.get(
            "source_url"
        ) != entry.get("source_url"):
            raise ValueError(f"{path}: evidence is about a different url or field")
        if evidence.get("response_digest_sha256") != entry.get("confirmed_digest_sha256"):
            raise ValueError(f"{path}: confirmed digest does not match the recorded artifact")
        previous = [
            item
            for item in record.get("field_evidence") or []
            if item.get("source_url") == evidence.get("source_url")
            and item.get("field_path") == evidence.get("field_path")
            and item.get("response_digest_sha256") == entry.get("previous_digest_sha256")
        ]
        if not previous:
            raise ValueError(f"{path}: previous digest is not a recorded artifact of this fact")
        restriction = _matching_digest_restriction(record, entry, evidence)
        if restriction is None:
            counts["refused"] += 1
            continue
        restrictions = record.setdefault("restrictions", {})
        restrictions["conflicting"] = [
            item for item in restrictions.get("conflicting") or [] if item is not restriction
        ]
        _append_review(
            record,
            kind="fact",
            decision="approved",
            actor_id=reviewer,
            decided_at_utc=decided_at_utc,
            text_digest=hashlib.sha256(
                canonical_json(
                    {
                        "previous_digest_sha256": entry.get("previous_digest_sha256"),
                        "confirmed_digest_sha256": entry.get("confirmed_digest_sha256"),
                    }
                ).encode("utf-8")
            ).hexdigest(),
            source_digest=str(entry.get("confirmed_digest_sha256")),
            note=(
                f"re-verified changed evidence on {evidence.get('field_path')}: "
                f"previous_digest={entry.get('previous_digest_sha256')} "
                f"confirmed_digest={entry.get('confirmed_digest_sha256')} "
                f"url={evidence.get('source_url')} evidence={evidence.get('evidence_id')} "
                f"observed_at={evidence.get('observed_at_utc')}; "
                "both artifacts are kept"
            ),
        )
        counts["reverified"] += 1
    return counts


def _next_review_sequence(record: Mapping[str, Any]) -> int:
    sequences = [
        item.get("sequence")
        for item in record.get("reviews") or []
        if isinstance(item.get("sequence"), int)
    ]
    return (max(sequences) if sequences else 0) + 1


def _append_review(
    record: dict[str, Any],
    *,
    kind: str,
    decision: str,
    actor_id: str,
    decided_at_utc: str,
    text_digest: str,
    source_digest: str,
    note: str,
    locale: str | None = None,
    evidence_refs: Sequence[str] | None = None,
) -> str:
    reviews = record.setdefault("reviews", [])
    sequence = _next_review_sequence(record)
    review_id = str(
        uuid.uuid5(
            REGISTRY_ID_NAMESPACE,
            f"{record.get('registry_id')}:{kind}:{sequence}:{source_digest}",
        )
    )
    reviews.append(
        {
            "review_id": review_id,
            "kind": kind,
            "decision": decision,
            "actor_id": actor_id,
            "sequence": sequence,
            "decided_at_utc": decided_at_utc,
            "text_digest_sha256": text_digest,
            "source_evidence_digest_sha256": source_digest,
            "locale": locale,
            "note": note,
            **({"evidence_refs": list(evidence_refs)} if evidence_refs is not None else {}),
        }
    )
    return review_id


def _clear_review_restriction(record: dict[str, Any], code: str) -> None:
    restrictions = record.get("restrictions") or {}
    for bucket in ("rejected", "missing", "conflicting"):
        items = restrictions.get(bucket)
        if not items:
            continue
        restrictions[bucket] = [item for item in items if item.get("code") != code]


def _record_digest(record: Mapping[str, Any]) -> str:
    """Digest of the reviewed facts, not of the review ledger that records the decision."""
    reviewed = {
        key: record.get(key)
        for key in ("canonical_name", "address", "market", "service_claims", "official_domains")
    }
    payload = json.dumps(reviewed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_BEZIRK_TRANSLITERATION = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def _normalize_bezirk(value: Any) -> str:
    """Bezirk spellings differ (umlauts, spaces, "Berlin-" prefix); compare by canonical key."""
    text = str(value or "").strip().lower()
    for source, target in _BEZIRK_TRANSLITERATION.items():
        text = text.replace(source, target)
    text = re.sub(r"^berlin[\s-]+", "", text)
    return re.sub(r"[\s_]+", "-", text)


def _district_verification_note(evidence: Mapping[str, Any]) -> str:
    if evidence["method"] == "official_address_source":
        return (
            "district_key verified against the official address register: "
            f"{evidence['source_url']} method={evidence['method']} "
            f"dataset={evidence['dataset_identifier']} "
            f"version={evidence.get('dataset_version', 'unstated')} "
            f"address_record={evidence['address_record_id']} "
            f"bezirk={evidence['bezirk']}"
        )
    return (
        "district_key verified against an address source: "
        f"{evidence['source_url']} method={evidence['method']} "
        f"boundary={evidence['boundary_contract']}@{evidence['boundary_digest']}"
    )


def _district_evidence_matches(
    address: Mapping[str, Any], evidence: Mapping[str, Any] | None
) -> bool:
    """A district confirmation needs evidence that names the same Bezirk it would certify."""
    if not isinstance(evidence, Mapping):
        return False
    required = ("source_url", "sha256", "observed_at_utc", "method", "bezirk", "excerpt")
    if any(not evidence.get(field) for field in required):
        return False
    if evidence["method"] == "official_address_source":
        # The Bezirk comes straight from the official address record, so no boundary
        # geometry is proven here; the address record identifier is the evidence instead.
        if not evidence.get("dataset_identifier") or not evidence.get("address_record_id"):
            return False
    elif not evidence.get("boundary_contract") or not evidence.get("boundary_digest"):
        return False
    if not is_http_url(str(evidence["source_url"])):
        return False
    if not HEX64_RE.match(str(evidence["sha256"])):
        return False
    if parse_utc(str(evidence["observed_at_utc"])) is None:
        return False
    return _normalize_bezirk(evidence["bezirk"]) == _normalize_bezirk(address.get("district_key"))


def apply_decisions(
    document: dict[str, Any],
    *,
    policy_digest: str | None,
    reviewer: str,
    decided_at_utc: str,
    confirm_district: bool,
    confirm_market: bool,
    identity_decision: str | None,
    registry_ids: Sequence[str] | None = None,
    district_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    district_policy_digest: str | None = None,
    media_omission_reason: str | None = None,
) -> dict[str, int]:
    """Record operator decisions on already reviewed records.

    Every decision is written next to the fact it is about and mirrored in the record's
    append-only review ledger. Existing decisions are never re-decided: an approved policy,
    a confirmed district or a resolved identity survives later runs.
    """
    if identity_decision is not None and identity_decision not in DECIDED_IDENTITY_ACTIONS:
        raise ValueError(f"unsupported identity decision: {identity_decision}")
    wanted = set(registry_ids) if registry_ids else None
    counts = {
        "policy": 0,
        "district": 0,
        "district_refused": 0,
        "market": 0,
        "identity": 0,
        "media": 0,
        "media_refused": 0,
    }
    for record in document.get("records") or []:
        if wanted is not None and record.get("registry_id") not in wanted:
            continue
        digest = _record_digest(record)

        if policy_digest is not None:
            pending = [
                entry
                for entry in record.get("field_evidence") or []
                if entry.get("source_kind") == "official_site"
                and entry.get("policy_decision") is None
            ]
            if pending:
                decision = {
                    "decision": "approved",
                    "reviewer": reviewer,
                    "decided_at_utc": decided_at_utc,
                    "scope": "official_site facts_only",
                    "policy_digest_sha256": policy_digest,
                }
                for entry in pending:
                    entry["policy_decision"] = dict(decision)
                _append_review(
                    record,
                    kind="source_policy",
                    decision="approved",
                    actor_id=reviewer,
                    decided_at_utc=decided_at_utc,
                    text_digest=digest,
                    source_digest=policy_digest,
                    note="operator approved official-site facts-only usage for this campaign",
                )
                counts["policy"] += 1

        address = record.get("address") or {}
        if confirm_district and address.get("district_basis") == "postal_code":
            evidence = (district_evidence or {}).get(record.get("registry_id") or "")
            if not _district_evidence_matches(address, evidence):
                counts["district_refused"] += 1
            else:
                observed_at = evidence["observed_at_utc"]
                entry = _evidence_entry(
                    field_path="address.district_key",
                    source_url=evidence["source_url"],
                    digest=evidence["sha256"],
                    observed_at=observed_at,
                    excerpt=evidence["excerpt"],
                    owner_id=record["registry_id"],
                    policy_decision={
                        "decision": "approved",
                        "reviewer": reviewer,
                        "decided_at_utc": decided_at_utc,
                        "scope": "official_site facts_only",
                        "policy_digest_sha256": district_policy_digest,
                    },
                )
                record.setdefault("field_evidence", []).append(entry)
                address["district_basis"] = "address_source_verified"
                _clear_review_restriction(record, "district_derivation_unconfirmed")
                _append_review(
                    record,
                    kind="fact",
                    decision="approved",
                    actor_id=reviewer,
                    decided_at_utc=decided_at_utc,
                    text_digest=hashlib.sha256(
                        canonical_json(evidence).encode("utf-8")
                    ).hexdigest(),
                    source_digest=evidence["sha256"],
                    note=_district_verification_note(evidence),
                )
                counts["district"] += 1

        if confirm_market:
            restrictions = (record.get("restrictions") or {}).get("conflicting") or []
            if any(
                item.get("code") == "market_classification_unconfirmed" for item in restrictions
            ):
                _clear_review_restriction(record, "market_classification_unconfirmed")
                _append_review(
                    record,
                    kind="fact",
                    decision="approved",
                    actor_id=reviewer,
                    decided_at_utc=decided_at_utc,
                    text_digest=digest,
                    source_digest=digest,
                    note="operator confirmed the campaign-level primary market classification",
                )
                counts["market"] += 1

        subject_match = record.get("subject_match") or {}
        if identity_decision and subject_match.get("decision") in (None, "undecided"):
            subject_match["decision"] = identity_decision
            subject_match["reason"] = (
                "operator reviewed the candidate: official domain and street address identify "
                "one Berlin studio with no existing Catalog listing"
            )
            _append_review(
                record,
                kind="identity",
                decision="approved",
                actor_id=reviewer,
                decided_at_utc=decided_at_utc,
                text_digest=digest,
                source_digest=digest,
                note=f"identity decision {identity_decision}",
            )
            record["subject_match"] = subject_match
            counts["identity"] += 1

        if media_omission_reason:
            if record.get("photo_candidates"):
                counts["media_refused"] += 1
            else:
                record["photo_candidates"] = [
                    {
                        "candidate_id": f"photo-omit-{record['registry_id'][:8]}",
                        "source_url": None,
                        "rights_holder": None,
                        "license_url": None,
                        "license_version": None,
                        "rights_state": "omitted_rights_unclear",
                        "observed_at_utc": decided_at_utc,
                        "evidence_digest_sha256": None,
                        "omission_reason": media_omission_reason,
                    }
                ]
                _append_review(
                    record,
                    kind="media_rights",
                    decision="approved",
                    actor_id=reviewer,
                    decided_at_utc=decided_at_utc,
                    text_digest=digest,
                    source_digest=policy_digest or digest,
                    note=media_omission_reason,
                )
                counts["media"] += 1
    return counts


RESEARCH_REVIEWER_PREFIX = "research-"


def record_review(
    registry_path: Path,
    *,
    reviewer: str,
    decided_at_utc: str,
    description_decisions: Sequence[Mapping[str, Any]],
    contact_decisions: Sequence[Mapping[str, Any]],
    dry_run: bool,
) -> int:
    """Record a human decision on a draft text or a contact.

    This is the only path that turns a description into ``approved`` or a contact into
    ``validated``. A research identity is refused: the tooling that drafted or captured a value
    cannot review it. A description decision names the record, the locale and the exact text digest
    the reviewer read, so a later edit cannot inherit an old approval.
    """
    if parse_utc(decided_at_utc) is None:
        print("decided_at_utc must use YYYY-MM-DDTHH:MM:SSZ", file=sys.stderr)
        return 2
    if not non_empty(reviewer) or reviewer.startswith(RESEARCH_REVIEWER_PREFIX):
        print(
            "refused: a research identity cannot review its own draft or capture",
            file=sys.stderr,
        )
        return 2

    document = read_json(registry_path)
    by_id = {record.get("registry_id"): record for record in document.get("records") or []}
    approved = 0
    rejected = 0
    validated = 0
    invalidated = 0
    refusals: list[str] = []

    for decision in description_decisions:
        record = by_id.get(decision.get("registry_id"))
        locale = decision.get("locale")
        outcome = decision.get("decision")
        if record is None:
            refusals.append(f"unknown registry id {decision.get('registry_id')}")
            continue
        description = (record.get("descriptions") or {}).get(locale)
        if description is None:
            refusals.append(f"{record['canonical_name']}: no {locale} description")
            continue
        if outcome not in REVIEW_DECISIONS:
            refusals.append(f"{record['canonical_name']} {locale}: decision {outcome}")
            continue
        if description.get("text_digest_sha256") != decision.get("text_digest_sha256"):
            refusals.append(
                f"{record['canonical_name']} {locale}: text changed since the reviewer read it"
            )
            continue
        review_id = _append_review(
            record,
            kind="translation",
            decision=outcome,
            actor_id=reviewer,
            decided_at_utc=decided_at_utc,
            text_digest=description["text_digest_sha256"],
            source_digest=str(description.get("source_evidence_digest_sha256") or ""),
            note=str(decision.get("note") or "human review of the exact draft text"),
            locale=locale,
        )
        if outcome == "approved":
            description["state"] = "approved"
            description["review_state"] = "approved"
            description["review_ref"] = review_id
            approved += 1
        else:
            description["state"] = "draft"
            description["review_state"] = "rejected"
            description["review_ref"] = review_id
            rejected += 1

    for decision in contact_decisions:
        record = by_id.get(decision.get("registry_id"))
        if record is None:
            refusals.append(f"unknown registry id {decision.get('registry_id')}")
            continue
        state = decision.get("validation_state")
        if state not in ("validated", "invalid"):
            refusals.append(f"{record['canonical_name']}: validation state {state}")
            continue
        match = [
            contact
            for contact in record.get("contacts") or []
            if contact.get("kind") == decision.get("kind")
            and contact.get("target") == decision.get("target")
        ]
        if not match:
            refusals.append(
                f"{record['canonical_name']}: no {decision.get('kind')} contact "
                f"{decision.get('target')}"
            )
            continue
        for contact in match:
            contact["validation_state"] = state
        contact = match[0]
        evidence_by_id = {
            item.get("evidence_id"): item for item in record.get("field_evidence") or []
        }
        evidenced = [
            evidence_by_id.get(ref)
            for ref in contact.get("evidence_refs") or []
            if evidence_by_id.get(ref) is not None
        ]
        supplied_digest = decision.get("source_evidence_digest_sha256")
        if supplied_digest is not None and supplied_digest not in {
            item["response_digest_sha256"] for item in evidenced
        }:
            refusals.append(
                f"{record['canonical_name']}: contact decision cites a digest that is not "
                "this contact's evidence"
            )
            continue
        source_digest = supplied_digest or (
            evidenced[0]["response_digest_sha256"] if evidenced else None
        )
        if not isinstance(source_digest, str) or not HEX64_RE.match(source_digest):
            refusals.append(
                f"{record['canonical_name']}: contact decision carries no evidenced digest"
            )
            continue
        _append_review(
            record,
            kind="fact",
            decision="approved" if state == "validated" else "rejected",
            actor_id=reviewer,
            decided_at_utc=decided_at_utc,
            text_digest=sha256_hex(canonical_json([contact.get("kind"), contact.get("target")])),
            source_digest=source_digest,
            note=str(decision.get("note") or f"human review of the contact: {state}"),
        )
        if state == "validated":
            validated += len(match)
        else:
            invalidated += len(match)

    problems = validate_registry(document)
    for problem in problems:
        print(problem.render(), file=sys.stderr)
    for message in refusals:
        print(f"refused {message}", file=sys.stderr)
    if problems:
        return 1
    print(
        f"reviews approved={approved} rejected={rejected} "
        f"contacts_validated={validated} contacts_invalid={invalidated} "
        f"refused={len(refusals)}"
    )
    if dry_run:
        print("dry run: registry was not written")
        return 1 if refusals else 0
    if refusals:
        print(
            "refused: at least one decision was refused, so the registry was not written",
            file=sys.stderr,
        )
        return 1
    write_text_atomic(registry_path, canonical_json(document))
    print(f"wrote {registry_path}")
    return 0


def _recorded_policy_digests(document: Mapping[str, Any]) -> set[str]:
    """Policy digests an operator already approved somewhere in this registry."""
    digests: set[str] = set()
    for record in document.get("records") or []:
        for entry in record.get("field_evidence") or []:
            decision = entry.get("policy_decision") or {}
            digest = decision.get("policy_digest_sha256")
            if isinstance(digest, str) and HEX64_RE.match(digest):
                digests.add(digest)
    return digests


def decide(
    registry_path: Path,
    *,
    policy_path: Path | None,
    reviewer: str,
    decided_at_utc: str,
    confirm_district: bool,
    confirm_market: bool,
    identity_decision: str | None,
    registry_ids: Sequence[str] | None,
    dry_run: bool,
    district_evidence_path: Path | None = None,
    policy_digest_override: str | None = None,
    media_omission_reason: str | None = None,
    reverify_path: Path | None = None,
) -> int:
    if parse_utc(decided_at_utc) is None:
        print("decided_at_utc must use YYYY-MM-DDTHH:MM:SSZ", file=sys.stderr)
        return 2
    if policy_path is not None and not policy_path.is_file():
        print(f"policy file not found: {policy_path}", file=sys.stderr)
        return 2
    policy_digest = None
    if policy_path is not None:
        policy_digest = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    document = read_json(registry_path)
    if policy_digest_override is not None:
        if policy_path is not None:
            print("refused: pass either --policy or --policy-digest, not both", file=sys.stderr)
            return 2
        if not HEX64_RE.match(policy_digest_override):
            print("policy digest must be a sha256 hex value", file=sys.stderr)
            return 2
        recorded = _recorded_policy_digests(document)
        if policy_digest_override not in recorded:
            # Continuity only: a new decision reuses an approved policy identity or reads the file.
            print(
                "refused: policy digest is not an approved policy already recorded here",
                file=sys.stderr,
            )
            return 2
        policy_digest = policy_digest_override
    district_evidence = None
    district_policy_digest = None
    if district_evidence_path is not None:
        if not district_evidence_path.is_file():
            print(f"district evidence file not found: {district_evidence_path}", file=sys.stderr)
            return 2
        payload = json.loads(district_evidence_path.read_text(encoding="utf-8"))
        district_evidence = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(district_evidence, dict):
            print("district evidence must be an object with a records map", file=sys.stderr)
            return 2
        district_policy_digest = hashlib.sha256(district_evidence_path.read_bytes()).hexdigest()
    if confirm_district and district_evidence is None:
        print(
            "refused: --confirm-district needs --district-evidence; a postal code is not a Bezirk",
            file=sys.stderr,
        )
        return 2

    counts = apply_decisions(
        document,
        policy_digest=policy_digest,
        reviewer=reviewer,
        decided_at_utc=decided_at_utc,
        confirm_district=confirm_district,
        confirm_market=confirm_market,
        identity_decision=identity_decision,
        registry_ids=registry_ids,
        district_evidence=district_evidence,
        district_policy_digest=district_policy_digest,
        media_omission_reason=media_omission_reason,
    )
    reverified = {"reverified": 0, "refused": 0}
    if reverify_path is not None:
        try:
            reverified = reverify_evidence(
                document,
                read_json(reverify_path),
                reviewer=reviewer,
                decided_at_utc=decided_at_utc,
            )
        except ValueError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
    problems = validate_registry(document)
    if problems:
        for problem in problems:
            print(f"{problem.code} {problem.path}: {problem.message}", file=sys.stderr)
        return 1
    print(
        "decisions policy={policy} district={district} district_refused={district_refused} "
        "market={market} identity={identity} media={media} media_refused={media_refused}".format(
            **counts
        )
    )
    if reverify_path is not None:
        print("reverified={reverified} reverify_refused={refused}".format(**reverified))
    if dry_run:
        print("dry run: registry was not written")
        return 0
    write_text_atomic(registry_path, canonical_json(document))
    print(f"wrote {registry_path}")
    return 0


def _load_registry(path: Path) -> tuple[dict[str, Any], list[Problem]]:
    document = read_json(path)
    return (document, validate_registry(document))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Berlin studio research registry")
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="validate the master registry")
    subparsers.add_parser("report", help="report computed counts and blockers")
    subparsers.add_parser("reconcile", help="report duplicate domain/address candidates")

    import_parser = subparsers.add_parser("import-leads", help="import dossier-shaped leads")
    import_parser.add_argument("--input", type=Path, action="append", required=True)
    import_parser.add_argument("--batch", required=True)
    import_parser.add_argument("--dry-run", action="store_true")

    enrich_parser = subparsers.add_parser("enrich", help="apply an evidence-backed patch file")
    enrich_parser.add_argument("--patch", type=Path, required=True)
    enrich_parser.add_argument("--dry-run", action="store_true")

    decide_parser = subparsers.add_parser(
        "decide", help="record operator decisions on reviewed records"
    )
    decide_parser.add_argument("--policy-file", type=Path)
    decide_parser.add_argument("--reviewer", required=True)
    decide_parser.add_argument("--decided-at-utc", required=True)
    decide_parser.add_argument("--confirm-district", action="store_true")
    decide_parser.add_argument("--district-evidence", type=Path, default=None)
    decide_parser.add_argument("--confirm-market", action="store_true")
    decide_parser.add_argument("--identity", choices=DECIDED_IDENTITY_ACTIONS)
    decide_parser.add_argument("--registry-id", action="append")
    decide_parser.add_argument("--dry-run", action="store_true")
    decide_parser.add_argument(
        "--policy-digest",
        dest="policy_digest",
        default=None,
        help="reuse an approved policy digest already recorded in this registry",
    )
    decide_parser.add_argument(
        "--omit-media",
        dest="omit_media",
        default=None,
        help="record that no photo is published, with the reason",
    )
    decide_parser.add_argument(
        "--reverify",
        dest="reverify",
        type=Path,
        default=None,
        help="confirm changed artifacts and clear only their digest conflicts",
    )

    review_parser = subparsers.add_parser(
        "review", help="record a human decision on a description or a contact"
    )
    review_parser.add_argument("--reviewer", required=True)
    review_parser.add_argument("--decided-at-utc", required=True)
    review_parser.add_argument("--descriptions", type=Path, default=None)
    review_parser.add_argument("--contacts", type=Path, default=None)
    review_parser.add_argument("--dry-run", action="store_true")

    evidence_parser = subparsers.add_parser(
        "record-evidence", help="append captured official-site artifacts to their records"
    )
    evidence_parser.add_argument("--capture", type=Path, required=True)
    evidence_parser.add_argument("--dry-run", action="store_true")

    diff_parser = subparsers.add_parser("diff", help="diff the registry against another document")
    diff_parser.add_argument("--other", type=Path, required=True)

    args = parser.parse_args(argv)

    if args.command == "import-leads":
        leads = load_lead_documents(args.input)
        return import_leads(args.registry, leads, args.batch, dry_run=args.dry_run)

    if args.command == "decide":
        return decide(
            args.registry,
            policy_path=args.policy_file,
            reviewer=args.reviewer,
            decided_at_utc=args.decided_at_utc,
            confirm_district=args.confirm_district,
            district_evidence_path=args.district_evidence,
            confirm_market=args.confirm_market,
            identity_decision=args.identity,
            registry_ids=args.registry_id,
            dry_run=args.dry_run,
            policy_digest_override=args.policy_digest,
            media_omission_reason=args.omit_media,
            reverify_path=args.reverify,
        )

    if args.command == "enrich":
        return enrich(args.registry, args.patch, dry_run=args.dry_run)

    if args.command == "review":
        description_decisions = (
            json.loads(args.descriptions.read_text(encoding="utf-8"))
            if args.descriptions is not None
            else []
        )
        contact_decisions = (
            json.loads(args.contacts.read_text(encoding="utf-8"))
            if args.contacts is not None
            else []
        )
        return record_review(
            args.registry,
            reviewer=args.reviewer,
            decided_at_utc=args.decided_at_utc,
            description_decisions=description_decisions,
            contact_decisions=contact_decisions,
            dry_run=args.dry_run,
        )

    if args.command == "record-evidence":
        return record_evidence(args.registry, args.capture, dry_run=args.dry_run)

    if args.command == "diff":
        current = read_json(args.registry)
        other = read_json(args.other)
        for line in diff_documents(current, other):
            print(line)
        return 0

    document, problems = _load_registry(args.registry)
    for problem in problems:
        print(problem.render(), file=sys.stderr)
    if problems:
        return 1

    if args.command == "validate":
        print(f"registry is valid: {len(document['records'])} record(s)")
        return 0
    if args.command == "report":
        for line in report_lines(document):
            print(line)
        return 0
    if args.command == "reconcile":
        for line in reconcile_report(document):
            print(line)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
