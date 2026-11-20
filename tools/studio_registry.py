"""Versioned studio research registry for the Berlin recording services campaign.

Two committed files, one record identity:

* ``campaigns/berlin_recording_services/discovery/studios.json``
  master worklist: every candidate, incomplete facts, evidence references.
* ``campaigns/berlin_recording_services/discovery/studios-verified.json``
  generated read-only projection of records that satisfy the rich policy.

The master is the only writable registry. The companion is produced by
``build-verified`` and is never edited by hand.

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
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_REVISION = "studio-registry-v1"
VERIFIED_SCHEMA_REVISION = "studio-verified-projection-v1"
PATCH_SCHEMA_REVISION = "studio-registry-patch-v1"
CAMPAIGN_KEY = "berlin_recording_services"
RICH_POLICY_KEY = "rich-verified-v1"
CONTENT_LOCALES = ("de-DE", "en-GB", "ru-RU")
CAMPAIGN_LOCALES = ("de-DE", "en-GB")

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "campaigns" / CAMPAIGN_KEY / "discovery" / "studios.json"
VERIFIED_PATH = REPO_ROOT / "campaigns" / CAMPAIGN_KEY / "discovery" / "studios-verified.json"

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
DISTRICT_BASES = ("observed_source", "operator_reviewed", "postal_code", "unknown")
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
STREET_NUMBER_RE = re.compile(
    r"^(?P<street>.+?)\s+(?P<number>\d+[A-Za-z]?(?:\s*[-/]\s*\d+[A-Za-z]?)?)$"
)
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
    return f"{kind}:{value.strip().lower()}"


def derive_registry_id(kind: str, value: str) -> str:
    """Derive the immutable id once from the authoritative identity key."""
    return str(uuid.uuid5(REGISTRY_ID_NAMESPACE, registry_identity_key(kind, value)))


def derive_evidence_id(source_url: str, digest: str, field_path: str) -> str:
    material = f"{source_url}\n{digest}\n{field_path}".encode()
    return "ev-" + hashlib.sha256(material).hexdigest()[:16]


def sha256_hex(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def canonical_json(document: Any) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


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
        ),
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

    validate_restrictions(problems, item.get("restrictions"), f"{path}.restrictions")


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


def validate_restrictions(problems: list[Problem], restrictions: Any, path: str) -> None:
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
                allowed=("code", "severity", "detail"),
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


def _blocking_codes(record: Mapping[str, Any]) -> list[str]:
    restrictions = record.get("restrictions") or {}
    codes = []
    for bucket in ("rejected", "conflicting"):
        for entry in restrictions.get(bucket) or []:
            if entry.get("severity") == "blocking":
                codes.append(str(entry.get("code")))
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

    reasons.extend(f"restriction:{code}" for code in _blocking_codes(record))
    return (not reasons, reasons)


def _approved_policy(evidence: Mapping[str, Any], domain: str) -> bool:
    if evidence.get("usage_policy") == "licensed_reuse":
        return True
    decision = evidence.get("policy_decision") or {}
    if decision.get("decision") != "approved":
        return False
    scope = str(decision.get("scope") or "").lower()
    return domain.lower() in scope or "official_site" in scope


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
        for ref in claim.get("evidence_refs") or []:
            evidence = evidence_by_id.get(ref) or {}
            policy = evidence.get("usage_policy")
            if policy in ("blocked", "research_only"):
                blockers.append(f"source_usage_blocked:{ref}")
            elif not _approved_policy(evidence, domain):
                blockers.append(f"source_policy_decision_missing:{domain}")

    address = record.get("address") or {}
    if address.get("district_basis") == "postal_code":
        blockers.append("district_derivation_not_confirmed")

    for restriction in (record.get("restrictions") or {}).get("review") or []:
        blockers.append(f"review_required:{restriction.get('code')}")

    if not record.get("last_refresh", {}).get("at_utc"):
        blockers.append("freshness_not_recorded")

    descriptions = record.get("descriptions") or {}
    approved = [locale for locale, text in descriptions.items() if text.get("state") == "approved"]
    if approved and len(approved) != len(CONTENT_LOCALES):
        blockers.append("partial_approved_descriptions")

    for photo in record.get("photo_candidates") or []:
        if photo.get("rights_state") == "candidate_unreviewed":
            blockers.append("media_rights_unreviewed")

    return sorted(set(blockers))


def rich_blockers(record: Mapping[str, Any]) -> list[str]:
    blockers = publication_blockers(record)
    descriptions = record.get("descriptions") or {}
    reviews = record.get("reviews") or []

    for locale in CONTENT_LOCALES:
        description = descriptions.get(locale)
        if not description:
            blockers.append(f"description_missing:{locale}")
            continue
        if description.get("state") != "approved":
            blockers.append(f"description_{description.get('state')}:{locale}")
            continue
        decision = latest_review_decision(
            reviews,
            kind="translation",
            review_id=description.get("review_ref"),
            locale=locale,
            text_digest=description.get("text_digest_sha256"),
            source_digest=description.get("source_evidence_digest_sha256"),
        )
        if decision != "approved":
            blockers.append(f"description_review_{decision or 'absent'}:{locale}")

    contacts = record.get("contacts") or []
    if not any(contact.get("validation_state") == "validated" for contact in contacts):
        blockers.append("validated_contact_missing")

    photos = record.get("photo_candidates") or []
    if not photos:
        blockers.append("media_state_not_recorded")
    for photo in photos:
        if photo.get("rights_state") == "candidate_unreviewed":
            blockers.append("media_rights_unreviewed")

    if (record.get("last_refresh") or {}).get("status") != "observed":
        blockers.append("freshness_not_current")

    return sorted(set(blockers))


def rich_subset(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(record) for record in records if not rich_blockers(record)]


def build_verified_projection(document: Mapping[str, Any], master_bytes: bytes) -> dict[str, Any]:
    records = rich_subset(document.get("records") or [])
    records.sort(key=lambda record: record["registry_id"])
    return {
        "schema_revision": VERIFIED_SCHEMA_REVISION,
        "policy": RICH_POLICY_KEY,
        "campaign_key": CAMPAIGN_KEY,
        "source_registry_sha256": hashlib.sha256(master_bytes).hexdigest(),
        "records": records,
    }


def write_verified(registry_path: Path, verified_path: Path, *, check: bool) -> int:
    master_bytes = registry_path.read_bytes()
    document = json.loads(master_bytes.decode("utf-8"))
    projection = build_verified_projection(document, master_bytes)
    payload = canonical_json(projection)
    current = verified_path.read_text(encoding="utf-8") if verified_path.exists() else None
    if check:
        if current != payload:
            print(f"generated projection is stale: {verified_path}")
            return 1
        print(f"generated projection is current: {len(projection['records'])} rich record(s)")
        return 0
    write_text_atomic(verified_path, payload)
    print(
        f"wrote {verified_path} with {len(projection['records'])} rich record(s); "
        f"registry digest {projection['source_registry_sha256']}"
    )
    return 0


def write_text_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    temporary.replace(path)


# --------------------------------------------------------------------------------------
# Lead import: dossier-shaped probe batches become registry records.
# --------------------------------------------------------------------------------------


def split_street_address(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return (None, None)
    match = STREET_NUMBER_RE.match(value.strip())
    if not match:
        return (value.strip(), None)
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
) -> dict[str, Any]:
    return {
        "evidence_id": derive_evidence_id(source_url, digest, field_path),
        "field_path": field_path,
        "source_kind": source_kind,
        "source_url": source_url,
        "external_id": external_id,
        "observed_at_utc": observed_at,
        "response_digest_sha256": digest,
        "excerpt": excerpt[:400],
        "usage_policy": usage_policy,
        "policy_decision": None,
        "attribution": None,
    }


def _lead_evidence(
    lead: Mapping[str, Any],
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
            )
            evidence.append(entry)
            role_refs.setdefault(role or "unknown", []).append(entry["evidence_id"])
    return (evidence, role_refs)


def _lead_address(lead: Mapping[str, Any]) -> dict[str, Any] | None:
    postal_code = lead.get("plz")
    street_value = lead.get("street")
    if not street_value and not postal_code:
        return None
    street, number = split_street_address(street_value)
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

    evidence, _role_refs = _lead_evidence(lead)
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
)


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


def apply_patch(document: dict[str, Any], patch: Mapping[str, Any]) -> list[Problem]:
    problems: list[Problem] = []
    if patch.get("schema_revision") != PATCH_SCHEMA_REVISION:
        problems.append(
            Problem("patch_schema", "$.schema_revision", f"expected {PATCH_SCHEMA_REVISION}")
        )
        return problems

    by_id = {record.get("registry_id"): record for record in document.get("records") or []}
    for index, entry in enumerate(patch.get("patches") or []):
        path = f"patches[{index}]"
        if not isinstance(entry, dict):
            problems.append(Problem("patch_shape", path, "expected object"))
            continue
        registry_id = entry.get("registry_id")
        field_path = entry.get("field_path")
        reviewer = entry.get("reviewer")
        evidence_refs = entry.get("evidence_refs") or []

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

        known_evidence = {item.get("evidence_id") for item in record.get("field_evidence") or []}
        if field_path.startswith(("address.", "contacts", "service_claims", "attributes")):
            if not evidence_refs:
                problems.append(
                    Problem(
                        "patch_evidence", f"{path}.evidence_refs", "observed facts need evidence"
                    )
                )
                continue
            for ref in evidence_refs:
                if ref not in known_evidence:
                    problems.append(
                        Problem(
                            "patch_evidence_unknown",
                            f"{path}.evidence_refs",
                            f"unknown evidence {ref}",
                        )
                    )
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

        _set_by_path(record, field_path, entry.get("value"))
        if not problems:
            record.setdefault("collector", {}).setdefault("revisions", []).append(
                f"patch:{field_path}:{reviewer}"
            )
    return problems


def enrich(registry_path: Path, patch_path: Path, *, dry_run: bool) -> int:
    document = read_json(registry_path)
    patch = read_json(patch_path)
    problems = apply_patch(document, patch)
    problems.extend(validate_registry(document))
    for problem in problems:
        print(problem.render(), file=sys.stderr)
    if problems:
        return 1
    if dry_run:
        print(f"dry run: patch applies cleanly to {len(document['records'])} record(s)")
        return 0
    write_text_atomic(registry_path, canonical_json(document))
    print(f"applied patch to {len(document['records'])} record(s)")
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
    rich = 0
    for record in records:
        qualified += 0 if qualification(record)[0] else 1
        publishable += 1 if not publication_blockers(record) else 0
        rich += 1 if not rich_blockers(record) else 0
    return [
        f"records={len(records)}",
        f"qualified={len(records) - qualified} publishable={publishable} rich={rich}",
    ]


DECIDED_IDENTITY_ACTIONS = ("create_new_pending_review", "link_existing")


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
) -> None:
    reviews = record.setdefault("reviews", [])
    sequence = _next_review_sequence(record)
    reviews.append(
        {
            "review_id": str(
                uuid.uuid5(
                    REGISTRY_ID_NAMESPACE,
                    f"{record.get('registry_id')}:{kind}:{sequence}:{source_digest}",
                )
            ),
            "kind": kind,
            "decision": decision,
            "actor_id": actor_id,
            "sequence": sequence,
            "decided_at_utc": decided_at_utc,
            "text_digest_sha256": text_digest,
            "source_evidence_digest_sha256": source_digest,
            "locale": None,
            "note": note,
        }
    )


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
) -> dict[str, int]:
    """Record operator decisions on already reviewed records.

    Every decision is written next to the fact it is about and mirrored in the record's
    append-only review ledger. Existing decisions are never re-decided: an approved policy,
    a confirmed district or a resolved identity survives later runs.
    """
    if identity_decision is not None and identity_decision not in DECIDED_IDENTITY_ACTIONS:
        raise ValueError(f"unsupported identity decision: {identity_decision}")
    wanted = set(registry_ids) if registry_ids else None
    counts = {"policy": 0, "district": 0, "market": 0, "identity": 0}
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
            address["district_basis"] = "operator_reviewed"
            _clear_review_restriction(record, "district_derivation_unconfirmed")
            _append_review(
                record,
                kind="fact",
                decision="approved",
                actor_id=reviewer,
                decided_at_utc=decided_at_utc,
                text_digest=digest,
                source_digest=digest,
                note="operator confirmed the postal-code derived district_key",
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
    return counts


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
    counts = apply_decisions(
        document,
        policy_digest=policy_digest,
        reviewer=reviewer,
        decided_at_utc=decided_at_utc,
        confirm_district=confirm_district,
        confirm_market=confirm_market,
        identity_decision=identity_decision,
        registry_ids=registry_ids,
    )
    problems = validate_registry(document)
    if problems:
        for problem in problems:
            print(f"{problem.code} {problem.path}: {problem.message}", file=sys.stderr)
        return 1
    print(
        "decisions policy={policy} district={district} market={market} identity={identity}".format(
            **counts
        )
    )
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
    parser.add_argument("--verified", type=Path, default=VERIFIED_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="validate the master registry")
    subparsers.add_parser("build-verified", help="regenerate the rich projection")
    subparsers.add_parser("check-verified", help="verify the committed projection is fresh")
    subparsers.add_parser("report", help="report computed counts and blockers")
    subparsers.add_parser("reconcile", help="report duplicate domain/address candidates")
    subparsers.add_parser("dry-run", help="validate and diff the projection without writing")

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
    decide_parser.add_argument("--confirm-market", action="store_true")
    decide_parser.add_argument("--identity", choices=DECIDED_IDENTITY_ACTIONS)
    decide_parser.add_argument("--registry-id", action="append")
    decide_parser.add_argument("--dry-run", action="store_true")

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
            confirm_market=args.confirm_market,
            identity_decision=args.identity,
            registry_ids=args.registry_id,
            dry_run=args.dry_run,
        )

    if args.command == "enrich":
        return enrich(args.registry, args.patch, dry_run=args.dry_run)

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
    if args.command == "build-verified":
        return write_verified(args.registry, args.verified, check=False)
    if args.command == "check-verified":
        return write_verified(args.registry, args.verified, check=True)
    if args.command == "dry-run":
        master_bytes = args.registry.read_bytes()
        projection = canonical_json(build_verified_projection(document, master_bytes))
        current_text = args.verified.read_text(encoding="utf-8") if args.verified.exists() else None
        print("no validation problems")
        print("projection is current" if projection == current_text else "projection change")
        if projection != current_text and current_text is not None:
            for line in diff_documents(json.loads(current_text), json.loads(projection)):
                print(line)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
