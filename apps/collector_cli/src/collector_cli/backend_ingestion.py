"""Produce backend `aggregator-candidate-ingestion` revision 3 documents from a sealed export.

This module is the only place that crosses into the backend contract. It reads a verified
`export-package@2` document and emits the exact rev-3 wire documents the backend expects:
one canonical item per candidate revision, the payload `{items: [...]}`, the item index, and
the manifest. Digests are computed with `backend_canonical`, whose rules are proven against
the backend's own canonicalizer by golden vectors.

Nothing is invented here. A value that the export did not seal is reported as missing for
its own reason instead of being defaulted, and evidence that the export marks as
`link_only` keeps that restriction per field.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from collection_contracts.export_package import (
    ExportCandidateEntry,
    ExportCatalogBinding,
    ExportFieldEvidence,
    ExportPackage,
)
from collector_cli.backend_canonical import (
    CONTRACT_IDENTITY,
    CONTRACT_REVISION,
    canonical_json_bytes,
    compute_digest,
)

_ENTITY_KINDS: Mapping[str, str] = {
    "organization": "organization",
    "place": "place",
    "provider": "provider",
}

_STATE_TOKENS: Mapping[str, str] = {
    "observed": "observed",
    "unknown": "unknown",
    "not_disclosed": "notDisclosed",
    "not_applicable": "notApplicable",
    "disputed": "disputed",
    "not_published": "notPublished",
}

_UNAVAILABLE_REASONS: Mapping[str, str] = {
    "not_published_by_source": "notPublishedBySource",
    "not_collected": "notCollected",
    "conflicting_evidence": "conflictingEvidence",
    "rights_restricted": "rightsRestricted",
    "owner_withheld": "ownerWithheld",
}

_AFFILIATION_DISPOSITIONS: Mapping[str, str] = {
    "unknown": "unknown",
    "independent": "independent",
    "affiliated": "affiliated",
}

_USAGE_POLICIES: Mapping[str, str] = {
    "publishable": "publishable",
    "display_with_attribution": "displayWithAttribution",
    "link_only": "linkOnly",
    "owner_authorized": "ownerAuthorized",
}

_CONTACT_KINDS: Mapping[str, str] = {
    "website": "website",
    "email": "email",
    "phone": "phone",
}

_QUALITY_STATES: Mapping[str, str] = {
    "passed": "passed",
    "review_required": "reviewRequired",
    "blocked": "blocked",
}

_SEVERITIES: Mapping[str, str] = {
    "error": "error",
    "warning": "warning",
    "info": "info",
}

# The backend owns its quarantine namespace and refuses an object key outside it, so the
# payload artifact is named inside that namespace rather than at an arbitrary path.
QUARANTINE_PREFIX = "ingestion/quarantine/"
PAYLOAD_ARTIFACT_ROLE = "candidatePayload"
PAYLOAD_CONTENT_TYPE = "application/json"

_GEOGRAPHY_STATES: Mapping[str, str] = {
    "inside": "proposedPoint",
    "boundary": "proposedPoint",
}


class IngestionAdapterError(ValueError):
    """Raised when a sealed export cannot be expressed in the backend contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class IngestionPackageDocuments:
    """The exact bytes and digests the backend registration consumes."""

    items: tuple[Mapping[str, Any], ...]
    payload_document: Mapping[str, Any]
    payload_bytes: bytes
    payload_digest: str
    item_index: tuple[Mapping[str, str], ...]
    item_index_bytes: bytes
    item_index_digest: str

    def item_digest(self, item_key: str) -> str:
        for item in self.items:
            if item["itemKey"] == item_key:
                return _bare_digest(str(item["contentDigest"]), field="contentDigest")
        raise IngestionAdapterError(
            "INGESTION_ITEM_MISSING", f"No ingested item carries key {item_key!r}."
        )


def _token(table: Mapping[str, str], value: str, *, field: str) -> str:
    try:
        return table[value]
    except KeyError:
        raise IngestionAdapterError(
            "INGESTION_VALUE_UNSUPPORTED",
            f"{field} value {value!r} has no backend contract token.",
        ) from None


def _bare_digest(value: str, *, field: str) -> str:
    """Translate a collector digest into the backend's bare lowercase hex form.

    The collector contract prefixes digests with `sha256:`; the backend document carries the
    same checksum without a prefix. The hexadecimal body must already be lowercase, so a
    malformed digest is refused instead of being silently normalized into a different value.
    """

    body = value.removeprefix("sha256:")
    if (
        len(body) != 64
        or body != body.lower()
        or any(character not in "0123456789abcdef" for character in body)
    ):
        raise IngestionAdapterError(
            "INGESTION_DIGEST_INVALID", f"{field} value is not a lowercase SHA-256 digest."
        )
    return body


def _utc_text(value: datetime) -> str:
    """The backend's canonical round-trip text for one instant.

    The backend writes fractional seconds as the shortest round-tripping form, so a
    microsecond value ending in zeros (`...647750`) is written `...64775`. Reproducing its
    trimming here is not cosmetic: the digest covers these exact bytes.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        raise IngestionAdapterError(
            "INGESTION_TIMESTAMP_NAIVE",
            "A backend document timestamp must be timezone-aware.",
        )
    text = value.astimezone(UTC).isoformat()
    if "." not in text:
        return text
    head, fraction_and_offset = text.split(".", 1)
    digits = len(fraction_and_offset) - len(fraction_and_offset.lstrip("0123456789"))
    fraction, offset = fraction_and_offset[:digits].rstrip("0"), fraction_and_offset[digits:]
    return f"{head}.{fraction}{offset}" if fraction else f"{head}{offset}"


def _decimal_text(value: float) -> Decimal:
    # str() of a float is its shortest round-tripping form, so a sealed 52.5 stays 52.5
    # rather than a binary-expansion artifact.
    return Decimal(str(value))


def _usage_policy(entry: ExportCandidateEntry, source_key: str, observed: str) -> str:
    """Never widen a field restriction, and never narrow one either."""

    for policy in entry.source_policies:
        if policy.source_key == source_key and policy.usage_policy != observed:
            raise IngestionAdapterError(
                "INGESTION_USAGE_POLICY_CONFLICT",
                f"Candidate {entry.candidate_id} evidence for {source_key!r} claims {observed!r} "
                f"but the sealed source policy is {policy.usage_policy!r}.",
            )
    return _token(_USAGE_POLICIES, observed, field="usagePolicy")


def _evidence_by_field(entry: ExportCandidateEntry) -> Mapping[str, tuple[Any, ...]]:
    grouped: dict[str, list[Any]] = {}
    for evidence in entry.field_evidence:
        grouped.setdefault(evidence.field_path, []).append(evidence)
    return {path: tuple(items) for path, items in grouped.items()}


def _provenance_reference(
    entry: ExportCandidateEntry, *, field_path: str, evidence: ExportFieldEvidence
) -> Mapping[str, Any]:
    return {
        # The backend requires a non-empty, unique UUID per provenance reference. Derived
        # deterministically from the sealed evidence so a replay of the same export
        # produces the same reference identity instead of an unstable random one.
        "referenceId": _reference_id(evidence, field_path=field_path),
        "fieldPath": field_path,
        "sourceKey": evidence.source_key,
        "sourceExternalId": evidence.source_record_id,
        "sourceUrl": evidence.source_url,
        "observedAtUtc": _utc_text(evidence.observed_at_utc),
        "usagePolicy": _usage_policy(entry, evidence.source_key, evidence.usage_policy),
        "evidenceDigest": _bare_digest(evidence.evidence_digest, field="evidenceDigest"),
        "attribution": evidence.attribution,
    }


def _reference_id(evidence: ExportFieldEvidence, *, field_path: str) -> str:
    """A stable provenance-reference identifier derived from the sealed evidence itself."""

    material = "|".join(
        (
            field_path,
            str(evidence.source_key),
            str(evidence.source_record_id),
            str(evidence.evidence_digest),
        )
    )
    return str(UUID(hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]))


def _geography_proposal(entry: ExportCandidateEntry) -> Mapping[str, Any] | None:
    geography = entry.geography
    state = _token(_GEOGRAPHY_STATES, geography.coverage, field="geography.coverage")
    latitude = _decimal_text(geography.latitude) if geography.latitude is not None else None
    longitude = _decimal_text(geography.longitude) if geography.longitude is not None else None
    # Null properties are omitted: the backend's canonicalizer writes the same document
    # either way, so carrying explicit nulls would only make the digest harder to predict.
    return {
        "state": state,
        "latitude": latitude,
        "longitude": longitude,
        "fieldPath": "geography/proposedPoint",
    }


_SUBJECT_PROPOSAL_PATH = "subjectProposal"
_GEOGRAPHY_PROPOSAL_PATH = "geography/proposedPoint"
_PLACE_ADDRESS_PATH = "placeDetails/address"
_PLACE_COORDINATES_PATH = "placeDetails/coordinates"
_IDENTITY_REFERENCE_PATH = "externalReferences/identity"
# The campaign mapping declares the accuracy it claims for source-observed points; the
# export carries no per-point accuracy, and a Place coordinates proposal may not be
# `observed` without one.
_PLACE_ADDRESS_MISSING_REASON = "not_published_by_source"


def _catalog_locale(entry: ExportCandidateEntry, binding: ExportCatalogBinding, locale: str) -> str:
    """Resolve a campaign name locale into the pinned Catalog display locale."""

    for mapping in binding.name_locales:
        if mapping.campaign_locale == locale:
            return mapping.catalog_locale
    raise IngestionAdapterError(
        "INGESTION_NAME_LOCALE_MAPPING_MISSING",
        f"Candidate {entry.candidate_id} carries a name in locale {locale!r} that the sealed "
        f"catalog binding does not map to a Catalog display locale.",
    )


def _place_details(
    entry: ExportCandidateEntry,
    binding: ExportCatalogBinding,
    observed_name: str,
) -> Mapping[str, Any]:
    """The native Place form of a studio candidate, with its own unresolved values kept.

    A studio is a physical place, so the Catalog can only turn it into a Listing through
    this shape. The exported candidate carries no address claim at all, so the address is
    reported as `unknown` for its own reason instead of being left out or guessed, and the
    identity reference is the source record the candidate was read from.
    """

    evidence = _single_evidence(entry, observed_name)
    identity = {
        "sourceSystem": evidence.source_key,
        "externalId": str(entry.candidate_id),
        "outboundUrl": evidence.source_url,
        "purpose": "identity",
        "usagePolicy": _usage_policy(entry, evidence.source_key, evidence.usage_policy),
        "fieldPath": _IDENTITY_REFERENCE_PATH,
    }
    if identity["usagePolicy"] != _token(_USAGE_POLICIES, "publishable", field="usagePolicy"):
        raise IngestionAdapterError(
            "INGESTION_PLACE_IDENTITY_UNPUBLISHABLE",
            f"Candidate {entry.candidate_id} identity evidence from {evidence.source_key!r} is "
            "not publishable, and a Place identity reference may not narrow or widen that.",
        )
    geography = entry.geography
    if geography.latitude is None or geography.longitude is None:
        raise IngestionAdapterError(
            "INGESTION_PLACE_POINT_MISSING",
            f"Candidate {entry.candidate_id} is a place without an observed point.",
        )
    return {
        "address": {
            "state": _token(_STATE_TOKENS, "unknown", field="placeDetails.address.state"),
            "fieldPath": _PLACE_ADDRESS_PATH,
            "missingReason": _token(
                _UNAVAILABLE_REASONS, _PLACE_ADDRESS_MISSING_REASON, field="missingReason"
            ),
        },
        "coordinates": {
            "state": _token(_STATE_TOKENS, "observed", field="placeDetails.coordinates.state"),
            "latitude": _decimal_text(geography.latitude),
            "longitude": _decimal_text(geography.longitude),
            "precisionMeters": binding.coordinate_precision_meters,
            "fieldPath": _PLACE_COORDINATES_PATH,
        },
        "organizationBinding": _token(
            _AFFILIATION_DISPOSITIONS, "unknown", field="placeDetails.organizationBinding"
        ),
        "identityReferences": [identity],
    }


def _catalog_categories(
    entry: ExportCandidateEntry, binding: ExportCatalogBinding
) -> tuple[tuple[str, str], ...]:
    """Resolve the campaign's observed categories into the pinned Catalog categories.

    The correspondence is declared and sealed by the export, so an observed category that
    the pin does not classify is refused here instead of being dropped from the proposal.
    Returns the resolved `(campaignCategory, catalogCategory)` pairs.
    """

    targets = binding.targets
    resolved: list[tuple[str, str]] = []
    missing: list[str] = []
    for category in entry.category_keys:
        target = targets.get(str(category))
        if target is None:
            missing.append(str(category))
        else:
            resolved.append((str(category), target))
    if missing:
        raise IngestionAdapterError(
            "INGESTION_CATEGORY_MAPPING_MISSING",
            f"Candidate {entry.candidate_id} observes categories the sealed catalog "
            f"binding does not classify: {', '.join(sorted(missing))}.",
        )
    return tuple(sorted(resolved))


def _single_evidence(entry: ExportCandidateEntry, field_path: str) -> ExportFieldEvidence:
    evidence_items = _evidence_by_field(entry).get(field_path, ())
    if not evidence_items:
        raise IngestionAdapterError(
            "INGESTION_FIELD_EVIDENCE_MISSING",
            f"Candidate {entry.candidate_id} has no evidence for field {field_path!r}.",
        )
    if len(evidence_items) > 1:
        raise IngestionAdapterError(
            "INGESTION_FIELD_EVIDENCE_AMBIGUOUS",
            f"Candidate {entry.candidate_id} carries more than one evidence record for "
            f"field {field_path!r}.",
        )
    evidence: ExportFieldEvidence = evidence_items[0]
    return evidence


def _provenance_references(
    entry: ExportCandidateEntry,
    claim_paths: tuple[tuple[str, str], ...],
) -> list[Mapping[str, Any]]:
    """One provenance reference per claim the item makes, addressed in backend terms.

    The backend proves each proposal against the exact path that proposal carries, so the
    collector's own field paths (`names`, `website_urls`, `categories/<campaign key>`) are
    transcribed into the backend's grouped form (`localizedNames/<locale>`,
    `contacts/<kind>`, `categories/<catalog key>`). The evidence record itself is never
    rewritten: only the path it is filed under changes.
    """

    references: list[Mapping[str, Any]] = []
    seen: dict[str, str] = {}
    for backend_path, evidence_path in claim_paths:
        if backend_path in seen:
            raise IngestionAdapterError(
                "INGESTION_PROVENANCE_PATH_DUPLICATE",
                f"Candidate {entry.candidate_id} would claim {backend_path!r} twice "
                f"({seen[backend_path]!r} and {evidence_path!r}).",
            )
        seen[backend_path] = evidence_path
        references.append(
            _provenance_reference(
                entry,
                field_path=backend_path,
                evidence=_single_evidence(entry, evidence_path),
            )
        )
    return references


def _item_document(entry: ExportCandidateEntry, binding: ExportCatalogBinding) -> Mapping[str, Any]:
    entity_kind = _token(_ENTITY_KINDS, entry.entity_kind, field="entityKind")
    if not entry.localized_names:
        raise IngestionAdapterError(
            "INGESTION_NAMES_MISSING", f"Candidate {entry.candidate_id} carries no localized name."
        )
    if not entry.review_decisions:
        raise IngestionAdapterError(
            "INGESTION_REVIEW_DECISION_MISSING",
            f"Candidate {entry.candidate_id} has no review decision to reference.",
        )
    if not entry.category_keys:
        raise IngestionAdapterError(
            "INGESTION_CATEGORY_MISSING", f"Candidate {entry.candidate_id} carries no category."
        )

    categories = _catalog_categories(entry, binding)
    observed_names = tuple(name for name in entry.localized_names if name.state == "observed")
    if not observed_names:
        raise IngestionAdapterError(
            "INGESTION_OBSERVED_NAME_MISSING",
            f"Candidate {entry.candidate_id} carries no observed name.",
        )

    claims: list[tuple[str, str]] = [(_SUBJECT_PROPOSAL_PATH, observed_names[0].field_path)]
    catalog_names = tuple(
        (_catalog_locale(entry, binding, name.locale), name) for name in entry.localized_names
    )
    if len({locale for locale, _ in catalog_names}) != len(catalog_names):
        raise IngestionAdapterError(
            "INGESTION_NAME_LOCALE_COLLISION",
            f"Candidate {entry.candidate_id} carries two names that map to the same Catalog "
            "display locale.",
        )
    claims.extend((f"localizedNames/{locale}", name.field_path) for locale, name in catalog_names)
    claims.extend(
        (f"categories/{catalog_category}", f"categories/{campaign_category}")
        for campaign_category, catalog_category in categories
    )
    claims.extend((f"contacts/{contact.kind}", contact.field_path) for contact in entry.contacts)
    claims.extend(
        (f"attributes/{attribute.attribute_key}", attribute.field_path)
        for attribute in entry.typed_attributes
    )
    geography = _geography_proposal(entry)
    if geography is not None:
        claims.append((_GEOGRAPHY_PROPOSAL_PATH, entry.geography.field_path))
    place_details = None
    if entity_kind == "place":
        place_details = _place_details(entry, binding, observed_names[0].field_path)
        claims.append((_PLACE_COORDINATES_PATH, entry.geography.field_path))
        claims.append((_IDENTITY_REFERENCE_PATH, observed_names[0].field_path))

    document: dict[str, Any] = {
        "itemKey": str(entry.candidate_id),
        "collectorCandidateId": str(entry.candidate_id),
        "collectorCandidateRevision": entry.candidate_revision,
        "entityKind": entity_kind,
        "subjectProposal": {
            "sourceSubjectKey": str(entry.candidate_id),
            "externalIdentityKeys": [],
        },
        "localizedNames": [
            {
                "locale": locale,
                "state": _token(_STATE_TOKENS, name.state, field="localizedNames.state"),
                "value": name.value,
                "fieldPath": f"localizedNames/{locale}",
            }
            for locale, name in catalog_names
        ],
        "categoryProposals": [catalog_category for _, catalog_category in categories],
        "typedAttributeProposals": [
            {
                "attributeKey": attribute.attribute_key,
                "state": _token(_STATE_TOKENS, attribute.state, field="typedAttributes.state"),
                "value": (
                    None
                    if attribute.text_value is None
                    else {"kind": "text", "textValue": attribute.text_value}
                ),
                "fieldPath": f"attributes/{attribute.attribute_key}",
            }
            for attribute in entry.typed_attributes
        ],
        "contactProposals": [
            {
                "kind": _token(_CONTACT_KINDS, contact.kind, field="contacts.kind"),
                "state": _token(_STATE_TOKENS, contact.state, field="contacts.state"),
                "target": contact.target,
                "fieldPath": f"contacts/{contact.kind}",
            }
            for contact in entry.contacts
        ],
        "externalReferenceProposals": (
            list(place_details["identityReferences"]) if place_details is not None else []
        ),
        "geographyProposal": geography,
        "relationshipProposals": [],
        "provenanceReferences": _provenance_references(entry, tuple(claims)),
        "qualitySummary": {
            "state": _token(_QUALITY_STATES, entry.quality.state, field="quality.state"),
            "issues": [
                {
                    "code": issue.code,
                    "severity": _token(
                        _SEVERITIES, issue.severity, field="quality.issues.severity"
                    ),
                    "requiredAction": issue.required_action,
                }
                for issue in entry.quality.issues
            ],
        },
        "collectorReviewReferences": [
            {
                "decisionId": str(decision.decision_id),
                "decisionKind": decision.decision_kind,
                "decisionDigest": _bare_digest(decision.decision_digest, field="decisionDigest"),
                "decidedAtUtc": _utc_text(decision.decided_at_utc),
            }
            for decision in entry.review_decisions
        ],
        "contentDigest": _UNSEALED_ITEM_DIGEST,
    }
    if place_details is not None:
        document["placeDetails"] = place_details
        # A native Place or Provider proposal must also address each category it claims,
        # because the backend proves a listing category against its own field path rather
        # than against a flat list of keys.
        document["categoryDetails"] = [
            {"categoryKey": catalog_category, "fieldPath": f"categories/{catalog_category}"}
            for _, catalog_category in categories
        ]
    # The backend defines an item's content digest over the item document without that
    # field, so it must be computed here rather than copied from the export, where the
    # digest covers the collector's own sealed entry shape.
    document["contentDigest"] = _item_content_digest(document)
    return document


_UNSEALED_ITEM_DIGEST = "0" * 64


def _item_content_digest(document: Mapping[str, Any]) -> str:
    without_digest = {key: value for key, value in document.items() if key != "contentDigest"}
    return compute_digest(without_digest)


def build_item_documents(export: ExportPackage) -> tuple[Mapping[str, Any], ...]:
    """One canonical backend item document per exported candidate revision."""

    if not export.entries:
        raise IngestionAdapterError(
            "INGESTION_EXPORT_EMPTY", "An empty export cannot be registered as an ingestion batch."
        )
    items: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for entry in export.entries:
        item = _item_document(entry, export.catalog_binding)
        key = str(item["itemKey"])
        if key in seen:
            raise IngestionAdapterError(
                "INGESTION_ITEM_DUPLICATE", f"Candidate {key} appears twice in one export."
            )
        seen.add(key)
        items.append(item)
    return tuple(items)


def build_package_documents(export: ExportPackage) -> IngestionPackageDocuments:
    """The payload, index and digests for one sealed export."""

    items = build_item_documents(export)
    payload_document: Mapping[str, Any] = {"items": list(items)}
    payload_bytes = _canonical_bytes(payload_document)
    # The index repeats each item's own sealed content digest; hashing the whole item again
    # would produce a value the backend never computes for an index entry.
    item_index = tuple(
        {
            "itemKey": str(item["itemKey"]),
            "contentDigest": _bare_digest(str(item["contentDigest"]), field="contentDigest"),
        }
        for item in items
    )
    index_bytes = _canonical_bytes(list(item_index))
    return IngestionPackageDocuments(
        items=items,
        payload_document=payload_document,
        payload_bytes=payload_bytes,
        payload_digest=compute_digest(payload_document),
        item_index=item_index,
        item_index_bytes=index_bytes,
        item_index_digest=compute_digest(list(item_index)),
    )


def build_manifest(
    export: ExportPackage,
    documents: IngestionPackageDocuments,
    *,
    producer_identity: str,
    producer_build: str,
    target_site_key: str,
    target_catalog_key: str,
    target_catalog_configuration_revision_id: str,
) -> Mapping[str, Any]:
    """The rev-3 manifest the backend registration expects.

    The target site, catalog and configuration revision belong to the backend's registered
    producer configuration. They are supplied by the operator from that registration rather
    than derived from the export, because a collector cannot know which catalog it feeds.
    """

    _require_pinned_targets(
        export,
        target_site_key=target_site_key,
        target_catalog_key=target_catalog_key,
        target_catalog_configuration_revision_id=target_catalog_configuration_revision_id,
    )
    policies = _manifest_policies(export)
    # The registered package carries exactly one payload artifact: the candidate payload the
    # upload authorization is issued for. Its key, digest and size are the ones the backend
    # re-reads at completion, so they are part of the manifest that gets registered.
    artifacts = [
        {
            "role": PAYLOAD_ARTIFACT_ROLE,
            "objectKey": payload_object_key(export),
            "contentDigest": documents.payload_digest,
            "size": len(documents.payload_bytes),
            "contentType": PAYLOAD_CONTENT_TYPE,
        }
    ]
    return {
        "contractIdentity": CONTRACT_IDENTITY,
        "contractRevision": CONTRACT_REVISION,
        "producerIdentity": producer_identity,
        "producerBuild": producer_build,
        "collectorExportId": str(export.run_id),
        "collectorExportDigest": _bare_digest(export.package_digest, field="collectorExportDigest"),
        "targetSiteKey": _product_key(target_site_key, field="targetSiteKey"),
        "targetCatalogKey": _product_key(target_catalog_key, field="targetCatalogKey"),
        "targetCatalogConfigurationRevisionId": _uuid(target_catalog_configuration_revision_id),
        "createdAtUtc": _utc_text(export.generated_at_utc),
        "itemCount": len(documents.items),
        "itemIndexDigest": documents.item_index_digest,
        "payloadDigest": documents.payload_digest,
        "sourcePolicies": [
            {
                "sourceKey": policy["sourceKey"],
                "policyDigest": _bare_digest(policy["policyDigest"], field="policyDigest"),
                "usagePolicy": policy["usagePolicy"],
            }
            for policy in policies
        ],
        "artifacts": artifacts,
    }


def _require_pinned_targets(
    export: ExportPackage,
    *,
    target_site_key: str,
    target_catalog_key: str,
    target_catalog_configuration_revision_id: str,
) -> None:
    """Refuse a submission aimed at a different Catalog than the export was classified for.

    The category mapping was authored against one exact configuration revision, so feeding
    a different revision would propose categories that were never classified for it.
    """

    binding = export.catalog_binding
    mismatches: list[str] = []
    if binding.site_key != _product_key(target_site_key, field="targetSiteKey"):
        mismatches.append(f"siteKey(expected={binding.site_key})")
    if binding.catalog_key != _product_key(target_catalog_key, field="targetCatalogKey"):
        mismatches.append(f"catalogKey(expected={binding.catalog_key})")
    pinned = _uuid(target_catalog_configuration_revision_id)
    if str(binding.configuration_revision_id) != pinned:
        mismatches.append(f"configurationRevisionId(expected={binding.configuration_revision_id})")
    if mismatches:
        raise IngestionAdapterError(
            "INGESTION_CATALOG_PIN_MISMATCH",
            "The submission target differs from the Catalog binding the export was "
            "classified against: " + ", ".join(mismatches) + ".",
        )


def _product_key(value: str, *, field: str) -> str:
    """Translate a collector key into the backend's canonical product key form.

    The backend accepts lowercase product keys; a collector campaign key such as
    `berlin_recording_services` carries underscores, so it is transcribed into the hyphenated
    product form the Catalog owner publishes instead of being sent verbatim.
    """

    candidate = value.strip().lower().replace("_", "-")
    if not candidate or not candidate[0].isascii() or not candidate[0].isalpha():
        raise IngestionAdapterError(
            "INGESTION_PRODUCT_KEY_INVALID",
            f"{field} value {value!r} cannot be expressed as a canonical product key.",
        )
    return candidate


def _uuid(value: str) -> str:
    try:
        return str(UUID(str(value).strip()))
    except ValueError:
        raise IngestionAdapterError(
            "INGESTION_IDENTIFIER_INVALID",
            f"{value!r} is not a UUID the backend can accept as a configuration revision.",
        ) from None


def _manifest_policies(export: ExportPackage) -> list[Mapping[str, Any]]:
    merged: dict[str, Mapping[str, Any]] = {}
    for entry in export.entries:
        for policy in _entry_policies(entry):
            merged.setdefault(policy["sourceKey"], policy)
    return list(merged.values())


def _entry_policies(entry: ExportCandidateEntry) -> list[Mapping[str, Any]]:
    return [
        {
            "sourceKey": policy.source_key,
            "policyDigest": policy.policy_digest,
            "usagePolicy": _token(_USAGE_POLICIES, policy.usage_policy, field="sourcePolicies"),
        }
        for policy in entry.source_policies
    ]


def _canonical_bytes(document: object) -> bytes:
    return canonical_json_bytes(document)


def verify_round_trip(documents: IngestionPackageDocuments) -> None:
    """Fail when a produced item will not pass the backend's own digest check."""

    for item in documents.items:
        expected = documents.item_digest(str(item["itemKey"]))
        if not expected:
            raise IngestionAdapterError("INGESTION_ITEM_DIGEST_MISSING", "An item has no digest.")
        is_hex = all(character in "0123456789abcdef" for character in expected)
        if len(expected) != 64 or not is_hex:
            raise IngestionAdapterError(
                "INGESTION_ITEM_DIGEST_INVALID", "An item digest is not lowercase hex SHA-256."
            )


def payload_object_key(export: ExportPackage) -> str:
    """The object key the backend accepts for this export's payload artifact."""

    return f"{QUARANTINE_PREFIX}{export.run_id}/payload.json"


__all__ = [
    "PAYLOAD_ARTIFACT_ROLE",
    "PAYLOAD_CONTENT_TYPE",
    "QUARANTINE_PREFIX",
    "IngestionAdapterError",
    "IngestionPackageDocuments",
    "build_item_documents",
    "build_manifest",
    "build_package_documents",
    "payload_object_key",
    "verify_round_trip",
]
