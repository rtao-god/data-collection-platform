"""Offline projection of the Berlin studio registry into a Catalog import manifest.

The registry owns verified facts; Catalog owns how those facts become a Place, a Listing,
an editorial decision and a publication. This module is the single producer of the versioned
manifest that crosses that boundary. It performs no network access: the collection and the
policy reviews already happened, and re-fetching would rebuild facts this repository already
proved.

What the manifest carries:
  * the exact source identity (source system + registry id) and the identity decision,
  * the facts Catalog may publish (name, address, categories, district),
  * one deterministic assertion per distinct evidence artifact, with its real provenance,
  * an explicit eligibility verdict per record instead of an implied success.

What it never carries: description drafts, media, contacts, coordinates read out of an
excerpt, or any field the registry has not approved for publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

try:  # imported as a package by tests and by `python -m tools.registry_catalog_manifest`
    from tools import studio_registry as registry
    from tools import wfs_address_points as wfs
except ImportError:  # direct script execution
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from tools import studio_registry as registry
    from tools import wfs_address_points as wfs


SCHEMA_REVISION = "registry-catalog-import-v1"
SOURCE_SYSTEM = "dcpf.berlin_recording_services"

# One fixed namespace keeps derived assertion ids stable across runs and machines, so a
# replay of unchanged bytes produces byte-identical ids instead of fresh ones.
ASSERTION_NAMESPACE = uuid.UUID("6f3c1f0e-7a2b-5c4d-8e9f-0a1b2c3d4e5f")

STATUS_ELIGIBLE = "eligible"
STATUS_REVIEW = "review"
STATUS_BLOCKED = "blocked"

# Field paths whose evidence must carry an approved source-policy decision before anything
# derived from them may leave this repository.
PUBLISHED_FIELD_PATHS = (
    "address.street",
    "address.postal_code",
    "address.district_key",
    "service_claims",
)

# The Berlin address dataset is an official address register, not a studio website. It is
# classified by its verifiable host and layer, never by the registry's declared source_kind,
# which labels every HTTP acquisition as `official_site`.
ADDRESS_DATASET_HOSTS = ("gdi.berlin.de",)
ADDRESS_DATASET_LAYER = "adressen_berlin"

SOURCE_KIND_PUBLIC_WEBSITE = "publicWebsite"
SOURCE_KIND_LICENSED_DATASET = "licensedDataset"
USAGE_POLICY_PUBLIC_ALLOWED = "publicAllowed"

DEFAULT_CAMPAIGN = pathlib.Path("campaigns") / "berlin_recording_services"
DEFAULT_REGISTRY = DEFAULT_CAMPAIGN / "discovery" / "studios.json"
DEFAULT_MAPPING = DEFAULT_CAMPAIGN / "catalog_mapping.yaml"


class ManifestError(Exception):
    """The inputs cannot produce a manifest; the message names the exact owner rule."""


@dataclass(frozen=True)
class CatalogMapping:
    """The pinned campaign -> Catalog classification this manifest is authored against."""

    mapping_digest_sha256: str
    site_key: str
    catalog_key: str
    configuration_revision_id: str
    coordinate_precision_meters: int
    categories: Mapping[str, str]
    name_locales: Mapping[str, str]

    def catalog_category(self, campaign_category: str) -> str | None:
        return self.categories.get(campaign_category)

    def catalog_locale(self, campaign_locale: str) -> str | None:
        return self.name_locales.get(campaign_locale)


def _nfc(value: Any) -> Any:
    """Normalize text to NFC so the same characters cannot yield two digests."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {_nfc(key): _nfc(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_nfc(item) for item in value]
    return value


def canonical_bytes(document: Any) -> bytes:
    """Deterministic bytes: sorted keys, no insignificant whitespace, UTF-8, NFC."""
    return _nfc(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ).encode("utf-8")


def digest_of(document: Any) -> str:
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def load_mapping(path: pathlib.Path) -> CatalogMapping:
    """Load the pinned category/locale mapping; an unmapped value is an error, never a guess."""
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ManifestError(f"catalog mapping is unreadable: {path}: {error}") from error
    if not isinstance(raw, Mapping):
        raise ManifestError(f"catalog mapping is not an object: {path}")

    unmapped = raw.get("unmapped") or []
    if unmapped:
        raise ManifestError(
            "catalog mapping declares unclassifiable campaign categories: "
            + ", ".join(str(item) for item in unmapped)
        )

    categories = {}
    for entry in raw.get("categories") or []:
        campaign_category = str(entry["campaign_category"])
        catalog_category = str(entry["catalog_category"])
        if campaign_category in categories:
            raise ManifestError(f"catalog mapping repeats campaign category '{campaign_category}'")
        categories[campaign_category] = catalog_category

    name_locales = {}
    for entry in raw.get("name_locales") or []:
        name_locales[str(entry["campaign_locale"])] = str(entry["catalog_locale"])

    required = (
        "site_key",
        "catalog_key",
        "configuration_revision_id",
        "coordinate_precision_meters",
    )
    for field in required:
        if raw.get(field) in (None, ""):
            raise ManifestError(f"catalog mapping is missing '{field}'")

    configuration_revision_id = str(raw["configuration_revision_id"])
    try:
        uuid.UUID(configuration_revision_id)
    except ValueError as error:
        raise ManifestError("catalog mapping configuration_revision_id is not a UUID") from error

    return CatalogMapping(
        mapping_digest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        site_key=str(raw["site_key"]),
        catalog_key=str(raw["catalog_key"]),
        configuration_revision_id=configuration_revision_id,
        coordinate_precision_meters=int(raw["coordinate_precision_meters"]),
        categories=categories,
        name_locales=name_locales,
    )


def classify_source(evidence: Mapping[str, Any]) -> tuple[str, str]:
    """Classify an evidence artifact by what it verifiably is, keeping the declared kind visible."""
    url = str(evidence.get("source_url") or "")
    host = registry.normalize_domain(url)
    if host in ADDRESS_DATASET_HOSTS and ADDRESS_DATASET_LAYER in url:
        return SOURCE_KIND_LICENSED_DATASET, "official_berlin_address_dataset"
    return SOURCE_KIND_PUBLIC_WEBSITE, "official_studio_website"


def derive_assertion_id(
    registry_id: str, source_url: str, response_digest: str, observed_at_utc: str
) -> str:
    material = f"{SOURCE_SYSTEM}|{registry_id}|{source_url}|{response_digest}|{observed_at_utc}"
    return str(uuid.uuid5(ASSERTION_NAMESPACE, material))


def _assertion_key(evidence: Mapping[str, Any]) -> tuple[str, str, str]:
    """One acquisition of one artifact: two observations of the same bytes are two records."""
    return (
        str(evidence.get("source_url") or ""),
        str(evidence.get("response_digest_sha256") or ""),
        _observed_at(evidence.get("observed_at_utc")),
    )


def _observed_at(value: Any) -> str:
    parsed = registry.parse_utc(value)
    if parsed is None:
        raise ManifestError(f"evidence observed_at_utc is not an exact UTC second: {value!r}")
    if parsed.microsecond != 0:
        raise ManifestError(
            "evidence observed_at_utc carries sub-second precision the registry never records"
        )
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _status_reasons(record: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Eligibility verdict plus exact reasons, from registry blockers and policy reviews."""
    blockers = registry.publication_blockers(record)
    review_reasons: list[str] = []

    restrictions = record.get("restrictions") or {}
    for bucket in ("conflicting", "rejected", "missing"):
        for entry in restrictions.get(bucket) or []:
            if entry.get("severity") == "review":
                review_reasons.append(f"review_required:{entry.get('code')}")

    for evidence in record.get("field_evidence") or []:
        if evidence.get("field_path") not in PUBLISHED_FIELD_PATHS:
            continue
        if not registry.evidence_allows_publication(record, evidence):
            review_reasons.append(f"source_policy_decision_missing:{evidence.get('source_url')}")

    if blockers:
        return STATUS_BLOCKED, sorted(set(blockers))
    if review_reasons:
        return STATUS_REVIEW, sorted(set(review_reasons))
    return STATUS_ELIGIBLE, []


def _identity(record: Mapping[str, Any]) -> dict[str, Any]:
    match = record.get("subject_match") or {}
    decision = str(match.get("decision") or "")
    if decision not in ("create_new_pending_review", "link_existing"):
        raise ManifestError(f"identity decision '{decision}' is not importable")

    existing = None
    if decision == "link_existing":
        listing_id = match.get("listing_id")
        # The registry names the Listing it recognized. The Place behind that Listing is a
        # Catalog-owned relation, so it is resolved there and never asserted from here.
        if not isinstance(listing_id, str) or not listing_id:
            raise ManifestError("link_existing identity requires listing_id")
        try:
            uuid.UUID(listing_id)
        except ValueError as error:
            raise ManifestError(f"link_existing listing_id is not a UUID: {listing_id}") from error
        existing = {"listingId": listing_id, "placeId": match.get("subject_id") or None}

    domain = registry.normalize_domain((record.get("official_domains") or {}).get("primary") or "")
    return {
        "decision": "link_existing" if decision == "link_existing" else "create_new",
        "linkExisting": existing,
        "officialDomain": domain or None,
        "sourceIdentity": f"{SOURCE_SYSTEM}:{record['registry_id']}",
    }


def _assertions(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One assertion per distinct evidence artifact, carrying every field it proves."""
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for evidence in record.get("field_evidence") or []:
        field_path = str(evidence.get("field_path") or "")
        if field_path.startswith("source."):
            continue  # collection bookkeeping (robots) is not a published fact
        key = _assertion_key(evidence)
        if not key[0] or not key[1]:
            raise ManifestError(
                f"evidence for '{field_path}' lacks a source url or response digest"
            )
        source_kind, basis = classify_source(evidence)
        entry = grouped.get(key)
        if entry is None:
            entry = {
                "assertionId": derive_assertion_id(record["registry_id"], key[0], key[1], key[2]),
                "sourceKind": source_kind,
                "sourceReference": key[0],
                "sourceExternalId": evidence.get("external_id") or None,
                "observedAtUtc": key[2],
                "usagePolicy": USAGE_POLICY_PUBLIC_ALLOWED,
                "evidenceDigest": key[1],
                "fieldPaths": [],
                "declaredSourceKind": evidence.get("source_kind"),
                "classificationBasis": basis,
            }
            grouped[key] = entry
        entry["fieldPaths"].append(field_path)

    for entry in grouped.values():
        entry["fieldPaths"] = sorted(set(entry["fieldPaths"]))
    return sorted(grouped.values(), key=lambda item: (item["assertionId"], item["sourceReference"]))


def _address_point(
    record: Mapping[str, Any],
    lookup: Callable[[str, str, str, str | None], Any] | None,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """Resolve the address point of this record, or report why it stays unresolved.

The point is derived from the official address register response itself, so it carries its
    own provenance assertion: the exact request url, the raw response digest, the address
    record id and the response timestamp. It never borrows an assertion of an older excerpt.
    """
    address = record.get("address") or {}
    street = _text_or_none(address.get("street"))
    street_number = _text_or_none(address.get("street_number"))
    postal_code = _text_or_none(address.get("postal_code"))
    expected_record_id = _text_or_none(address.get("address_record_id"))
    if lookup is None:
        return None, "address_point_lookup_disabled", None
    if street is None or street_number is None or postal_code is None:
        return None, wfs.REASON_ADDRESS_INCOMPLETE, None
    try:
        resolution = lookup(street, street_number, postal_code, expected_record_id)
    except wfs.WfsError:
        return None, "address_point_lookup_failed", None
    if not resolution.resolved:
        return None, resolution.reason, None

    point = resolution.point
    source_kind, basis = classify_source({"source_url": point.source_reference})
    assertion_id = derive_assertion_id(
        record["registry_id"], point.source_reference, point.source_digest, point.source_observed_at
    )
    assertion = {
        "assertionId": assertion_id,
        "sourceKind": source_kind,
        "sourceReference": point.source_reference,
        "sourceExternalId": point.address_record_id,
        "observedAtUtc": point.source_observed_at,
        "usagePolicy": USAGE_POLICY_PUBLIC_ALLOWED,
        "evidenceDigest": point.source_digest,
        "fieldPaths": ["address.coordinate"],
        "declaredSourceKind": None,
        "classificationBasis": basis,
    }
    return point.as_manifest_block(assertion_id), None, assertion


def _business(
    record: Mapping[str, Any],
    mapping: CatalogMapping,
    assertions: list[dict[str, Any]],
    address_point_lookup: Callable[[str, str, str, str | None], Any] | None = None,
) -> dict[str, Any]:
    address = record.get("address") or {}
    district_basis = str(address.get("district_basis") or "")
    if district_basis != "address_source_verified":
        raise ManifestError(f"district basis '{district_basis}' is not a verified address record")

    name_locale = mapping.catalog_locale("de")
    if name_locale is None:
        raise ManifestError("catalog mapping does not establish the language of the published name")

    claims = [
        claim for claim in record.get("service_claims") or [] if claim.get("catalog_category_key")
    ]
    if not claims:
        raise ManifestError("a publishable record requires at least one classified service claim")

    claim_assertions = {}
    for claim in claims:
        refs = [str(ref) for ref in claim.get("evidence_refs") or []]
        if not refs:
            raise ManifestError(
                f"category claim '{claim['catalog_category_key']}' carries no evidence"
            )
        matched = _assertions_backing(record, assertions, refs)
        if not matched:
            raise ManifestError(
                f"category claim '{claim['catalog_category_key']}' cites unexported evidence"
            )
        claim_assertions[str(claim["catalog_category_key"])] = matched[0]

    categories = [
        {"categoryKey": key, "assertionId": value}
        for key, value in sorted(claim_assertions.items())
    ]

    address_assertion = _first_assertion_for(
        assertions, ("address.street", "address.postal_code", "address.district_key")
    )
    if address_assertion is None:
        raise ManifestError("a publishable record requires evidence for its address facts")

    name_assertion = address_assertion

    coordinate, coordinate_reason, coordinate_assertion = _address_point(
        record, address_point_lookup
    )
    if coordinate_assertion is not None:
        assertions.append(coordinate_assertion)

    return {
        "name": str(record.get("canonical_name") or "").strip() or None,
        "nameLocale": name_locale,
        "nameAssertionId": name_assertion,
        "address": {
            "street": _text_or_none(address.get("street")),
            "streetNumber": _text_or_none(address.get("street_number")),
            "postalCode": _text_or_none(address.get("postal_code")),
            "city": _text_or_none(address.get("city")),
            "countryCode": _text_or_none(address.get("country_code")),
            "districtKey": _text_or_none(address.get("district_key")),
            "districtBasis": district_basis,
            "assertionId": address_assertion,
            "coordinate": coordinate,
        },
        "categories": categories,
        "coordinates": coordinate,
        "coordinateReason": coordinate_reason,
        "displayLocales": [name_locale],
    }


def _text_or_none(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _assertions_backing(
    record: Mapping[str, Any], assertions: Sequence[Mapping[str, Any]], evidence_refs: Sequence[str]
) -> list[str]:
    """Map registry evidence ids cited by a claim onto the exported assertion ids."""
    evidence_ids = {
        str(entry.get("evidence_id")): _assertion_key(entry)
        for entry in record.get("field_evidence") or []
    }
    keys = {evidence_ids[ref] for ref in evidence_refs if ref in evidence_ids}
    return [
        item["assertionId"]
        for item in assertions
        if (item["sourceReference"], item["evidenceDigest"], item["observedAtUtc"]) in keys
    ]


def _first_assertion_for(
    assertions: Sequence[Mapping[str, Any]], field_paths: Sequence[str]
) -> str | None:
    for assertion in assertions:
        if not field_paths:
            return assertion["assertionId"]
        if any(path in assertion["fieldPaths"] for path in field_paths):
            return assertion["assertionId"]
    return None


def build_record(
    record: Mapping[str, Any],
    mapping: CatalogMapping,
    address_point_lookup: Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    """Project one registry record into its manifest entry."""
    registry_id = str(record.get("registry_id") or "")
    if not registry_id:
        raise ManifestError("a registry record has no registry_id")

    status, reasons = _status_reasons(record)
    identity = _identity(record)

    entry: dict[str, Any] = {
        "schemaRevision": SCHEMA_REVISION,
        "sourceSystem": SOURCE_SYSTEM,
        "registryId": registry_id,
        "status": status,
        "statusReasons": reasons,
        "identity": identity,
        "business": None,
        "assertions": [],
    }

    if status is not STATUS_BLOCKED:
        assertions = _assertions(record)
        if not assertions:
            raise ManifestError(f"record {registry_id} has no exportable evidence")
        entry["business"] = _business(record, mapping, assertions, address_point_lookup)
        entry["assertions"] = sorted(
            assertions, key=lambda item: (item["assertionId"], item["sourceReference"])
        )
        # A record whose official address point stays unresolved cannot become a publishable
        # listing, so its verdict must say so here instead of surfacing later as an unpublishable
        # draft. An offline plan (lookup disabled) keeps the registry verdict and its reason.
        coordinate_reason = entry["business"]["coordinateReason"]
        if (
            entry["status"] is STATUS_ELIGIBLE
            and address_point_lookup is not None
            and coordinate_reason is not None
        ):
            entry["status"] = STATUS_REVIEW
            entry["statusReasons"] = [f"address_coordinate_unresolved:{coordinate_reason}"]

    entry["digests"] = _digests(entry, mapping)
    return entry


def _content_facts(business: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The publishable facts alone: no assertion ids, so provenance drift cannot change content."""
    if business is None:
        return None
    address = business["address"]
    return {
        "name": business["name"],
        "nameLocale": business["nameLocale"],
        "address": {
            field: address[field]
            for field in (
                "street",
                "streetNumber",
                "postalCode",
                "city",
                "countryCode",
                "districtKey",
                "districtBasis",
            )
        },
        "categories": [item["categoryKey"] for item in business["categories"]],
        "coordinate": (
            {
                key: business["coordinates"][key]
                for key in ("latitude", "longitude", "accuracyMeters")
            }
            if business["coordinates"]
            else None
        ),
        "displayLocales": business["displayLocales"],
    }


def _digests(entry: Mapping[str, Any], mapping: CatalogMapping) -> dict[str, str]:
    """Content, provenance and eligibility are cached independently so one cannot hide another."""
    business = entry.get("business")
    content_material = {
        "schemaRevision": entry["schemaRevision"],
        "mappingRevision": mapping.mapping_digest_sha256,
        "facts": _content_facts(business),
    }
    evidence_material = {
        "assertions": [
            {
                "assertionId": item["assertionId"],
                "sourceKind": item["sourceKind"],
                "sourceReference": item["sourceReference"],
                "sourceExternalId": item["sourceExternalId"],
                "observedAtUtc": item["observedAtUtc"],
                "usagePolicy": item["usagePolicy"],
                "evidenceDigest": item["evidenceDigest"],
                "fieldPaths": item["fieldPaths"],
            }
            for item in entry.get("assertions") or []
        ]
    }
    eligibility_material = {
        "status": entry["status"],
        "statusReasons": entry["statusReasons"],
        "configurationRevisionId": mapping.configuration_revision_id,
        "mappingRevision": mapping.mapping_digest_sha256,
        "districtBasis": ((business or {}).get("address") or {}).get("districtBasis"),
    }
    return {
        "content": digest_of(content_material),
        "evidence": digest_of(evidence_material),
        "eligibility": digest_of(eligibility_material),
    }


def build_manifest(
    document: Mapping[str, Any],
    registry_bytes: bytes,
    mapping: CatalogMapping,
    *,
    recorded_at_utc: str | None,
    address_point_lookup: Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    """Build the whole manifest. Records are ordered by registry id for reproducible bytes.

    ``recorded_at_utc`` is the moment the manifest is recorded, and Catalog refuses a place
    revision that precedes the evidence it carries. An address point is observed while this
    build runs, so the recording time is decided after the records exist: omitted (``None``)
    means "no earlier than the latest observation", and an explicit value that precedes an
    observation is refused here instead of being silently raised to pass the Catalog check.
    """
    records = list(document.get("records") or [])
    seen: set[str] = set()
    projected = []
    for record in sorted(records, key=lambda item: str(item.get("registry_id") or "")):
        entry = build_record(record, mapping, address_point_lookup)
        if entry["registryId"] in seen:
            raise ManifestError(f"registry repeats id {entry['registryId']}")
        seen.add(entry["registryId"])
        projected.append(entry)

    digests = []
    for entry in projected:
        digests.append({"registryId": entry["registryId"], **entry["digests"]})

    recorded_at_utc = _recording_time(projected, recorded_at_utc)
    manifest = {
        "schemaRevision": SCHEMA_REVISION,
        "sourceSystem": SOURCE_SYSTEM,
        "sourceRegistry": {
            "campaignKey": document.get("campaign_key"),
            "schemaRevision": document.get("schema_revision"),
            "digestSha256": hashlib.sha256(registry_bytes).hexdigest(),
            "recordCount": len(projected),
        },
        "catalog": {
            "siteKey": mapping.site_key,
            "catalogKey": mapping.catalog_key,
            "configurationRevisionId": mapping.configuration_revision_id,
            "mappingRevision": mapping.mapping_digest_sha256,
            "coordinatePrecisionMeters": mapping.coordinate_precision_meters,
        },
        "recordedAtUtc": recorded_at_utc,
        "records": projected,
        "digests": {
            "records": sorted(digests, key=lambda item: item["registryId"]),
        },
    }
    manifest["manifestDigest"] = manifest_digest(manifest)
    return manifest


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """The manifest's own identity: everything except the digest field and the recording time."""
    material = {
        key: value
        for key, value in manifest.items()
        if key not in ("manifestDigest", "recordedAtUtc")
    }
    return digest_of(material)


def counts(manifest: Mapping[str, Any]) -> dict[str, int]:
    records = manifest.get("records") or []
    summary = {
        "records": len(records),
        STATUS_ELIGIBLE: sum(1 for item in records if item["status"] == STATUS_ELIGIBLE),
        STATUS_REVIEW: sum(1 for item in records if item["status"] == STATUS_REVIEW),
        STATUS_BLOCKED: sum(1 for item in records if item["status"] == STATUS_BLOCKED),
        "createNew": sum(1 for item in records if item["identity"]["decision"] == "create_new"),
        "linkExisting": sum(
            1 for item in records if item["identity"]["decision"] == "link_existing"
        ),
    }
    return summary


def plan_lines(manifest: Mapping[str, Any]) -> Iterable[str]:
    summary = counts(manifest)
    yield (
        f"registry digest {manifest['sourceRegistry']['digestSha256']} "
        f"manifest digest {manifest['manifestDigest']}"
    )
    catalog = manifest["catalog"]
    yield (
        f"catalog {catalog['catalogKey']} configuration {catalog['configurationRevisionId']} "
        f"mapping {catalog['mappingRevision']} recorded at {manifest['recordedAtUtc']}"
    )
    yield (
        f"records {summary['records']}: eligible {summary[STATUS_ELIGIBLE]} "
        f"review {summary[STATUS_REVIEW]} blocked {summary[STATUS_BLOCKED]} "
        f"create_new {summary['createNew']} link_existing {summary['linkExisting']}"
    )
    for item in manifest.get("records") or []:
        if item["status"] == STATUS_ELIGIBLE:
            continue
        yield f"  {item['registryId']} {item['status']}: {', '.join(item['statusReasons'])}"


def diff_lines(previous: Mapping[str, Any], current: Mapping[str, Any]) -> Iterable[str]:
    """Report exactly which records changed, and along which digest axis."""
    before = {item["registryId"]: item for item in previous.get("records") or []}
    after = {item["registryId"]: item for item in current.get("records") or []}
    for registry_id in sorted(after):
        new = after[registry_id]
        old = before.get(registry_id)
        if old is None:
            yield f"added {registry_id} {new['status']}"
            continue
        axes = [
            axis
            for axis in ("content", "evidence", "eligibility")
            if old["digests"][axis] != new["digests"][axis]
        ]
        if old["status"] != new["status"]:
            axes.append(f"status:{old['status']}->{new['status']}")
        if axes:
            yield f"changed {registry_id}: {', '.join(axes)}"
    for registry_id in sorted(set(before) - set(after)):
        yield f"removed {registry_id}"


def _recorded_at(value: str | None) -> str | None:
    if value is None:
        return None
    if registry.parse_utc(value) is None:
        raise ManifestError("--recorded-at-utc must be an exact UTC second (YYYY-MM-DDTHH:MM:SSZ)")
    return value


def _recording_time(records: list[dict[str, Any]], requested: str | None) -> str:
    """The manifest's recording time, never earlier than the evidence it carries."""
    from datetime import UTC, datetime

    observed = [
        str(assertion["observedAtUtc"])
        for record in records
        for assertion in (record.get("assertions") or [])
    ]
    latest = max(observed) if observed else None
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if requested is None:
        return max(candidate for candidate in (latest, now) if candidate is not None)
    if latest is not None and requested < latest:
        raise ManifestError(
            f"recordedAtUtc {requested} precedes the evidence this manifest carries ({latest}); "
            "Catalog refuses a place revision older than its evidence"
        )
    return requested


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Berlin studio registry -> Catalog import manifest"
    )
    parser.add_argument("--registry", type=pathlib.Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--mapping", type=pathlib.Path, default=DEFAULT_MAPPING)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="build the manifest and report eligibility")
    plan_parser.add_argument("--out", type=pathlib.Path)
    plan_parser.add_argument("--recorded-at-utc")
    plan_parser.add_argument("--wfs-cache", type=pathlib.Path, default=None)

    diff_parser = subparsers.add_parser("diff", help="compare two manifests record by record")
    diff_parser.add_argument("--previous", type=pathlib.Path, required=True)
    diff_parser.add_argument("--current", type=pathlib.Path, required=True)

    args = parser.parse_args(argv)

    if args.command == "diff":
        previous = registry.read_json(args.previous)
        current = registry.read_json(args.current)
        for line in diff_lines(previous, current):
            print(line)
        return 0

    try:
        document, problems = registry._load_registry(args.registry)
        if problems:
            for problem in problems:
                print(problem.render(), file=sys.stderr)
            raise ManifestError(f"registry has {len(problems)} validation problem(s)")
        mapping = load_mapping(args.mapping)
        lookup = None
        if getattr(args, "wfs_cache", None) is not None:
            lookup = wfs.AddressPointLookup(args.wfs_cache)
        manifest = build_manifest(
            document,
            args.registry.read_bytes(),
            mapping,
            recorded_at_utc=_recorded_at(args.recorded_at_utc),
            address_point_lookup=lookup,
        )
        if lookup is not None:
            print(
                f"address points: {lookup.requests_made} fetched, {lookup.cache_hits} from cache",
                file=sys.stderr,
            )
    except ManifestError as error:
        print(f"manifest refused: {error}", file=sys.stderr)
        return 1

    for line in plan_lines(manifest):
        print(line)
    if args.out is not None:
        registry.write_text_atomic(args.out, registry.canonical_json(manifest))
        print(f"manifest written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
