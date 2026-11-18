"""Adapter tests over a real sealed export-package@2 document.

The export is built and sealed by the contract's own sealer, so the adapter is proven
against the exact producer document rather than a hand-written approximation. Digests are
checked against the backend's canonical form via `backend_canonical`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from collection_contracts.export_package import (
    ExportPackagePayload,
    seal_export_candidate,
    seal_export_package,
)
from collector_cli.backend_canonical import compute_digest
from collector_cli.backend_ingestion import (
    IngestionAdapterError,
    _utc_text,
    build_manifest,
    build_package_documents,
)

_NOW = datetime(2026, 11, 16, 0, 3, 0, tzinfo=UTC)
_NAME_EVIDENCE = "sha256:" + "a" * 64
_POLICY_DIGEST = "sha256:" + "b" * 64
_DECISION_DIGEST = "sha256:" + "c" * 64
_CONFIGURATION_REVISION = "00000000-0000-0000-0000-00000000d002"


def _evidence(field_path: str, *, usage_policy: str = "publishable") -> dict[str, Any]:
    return {
        "fieldPath": field_path,
        "sourceKey": "openstreetmap",
        "sourceRecordId": "osm:node:101",
        "sourceUrl": "https://www.openstreetmap.org/node/101",
        "observedAtUtc": "2026-11-16T00:01:59+00:00",
        "legalStatus": "approved",
        "usagePolicy": usage_policy,
        "evidenceDigest": _NAME_EVIDENCE,
        "sourcePolicyDigest": _POLICY_DIGEST,
        "attribution": "OpenStreetMap contributors",
    }


def _entry_document(
    *, usage_policy: str = "publishable", entity_kind: str = "organization"
) -> dict[str, Any]:
    return {
        "candidateId": "00000000-0000-0000-0000-00000000a001",
        "candidateRevision": 1,
        "snapshotDigest": "sha256:" + "d" * 64,
        "clusterId": "00000000-0000-0000-0000-00000000a002",
        "entityKind": entity_kind,
        "localizedNames": [
            {
                "locale": "de",
                "state": "observed",
                "value": "Studio One",
                "fieldPath": "names",
            }
        ],
        "contacts": [
            {
                "kind": "website",
                "state": "observed",
                "target": "https://studio-one.example/",
                "fieldPath": "website_urls",
            }
        ],
        "typedAttributes": [],
        "categoryKeys": ["recording_studio"],
        "geography": {
            "coverage": "inside",
            "marketAreaRevision": "berlin_boundary_v1",
            "marketAreaDigest": "sha256:" + "e" * 64,
            "latitude": 52.5007771,
            "longitude": 13.4168436,
            "fieldPath": "geography",
        },
        "fieldEvidence": [
            _evidence("names", usage_policy=usage_policy),
            _evidence("website_urls", usage_policy=usage_policy),
            _evidence("geography", usage_policy=usage_policy),
            _evidence("categories/recording_studio", usage_policy=usage_policy),
        ],
        "quality": {"state": "passed", "issues": []},
        "qualityEvaluationDigest": "sha256:" + "f" * 64,
        "qualityPolicyDigest": "sha256:" + "1" * 64,
        "reviewDecisions": [
            {
                "decisionId": "00000000-0000-0000-0000-00000000c001",
                "decisionKind": "accept_candidate",
                "decisionDigest": _DECISION_DIGEST,
                "decidedAtUtc": "2026-11-16T00:02:00+00:00",
            }
        ],
        "sourcePolicies": [
            {
                "sourceKey": "openstreetmap",
                "policyDigest": _POLICY_DIGEST,
                "legalStatus": "approved",
                "usagePolicy": usage_policy,
                "attribution": "OpenStreetMap contributors",
            }
        ],
        "sourceArtifactIds": ["00000000-0000-0000-0000-00000000b001"],
        "contentDigest": "sha256:" + "0" * 64,
    }


def _sealed_export(*, usage_policy: str = "publishable", entity_kind: str = "organization") -> Any:
    entry = seal_export_candidate(
        _entry_document(usage_policy=usage_policy, entity_kind=entity_kind)
    )
    payload = {
        "runId": "00000000-0000-0000-0000-00000000d001",
        "campaignBundleDigest": "sha256:" + "2" * 64,
        "campaignKey": "berlin_recording_services",
        "exportProfileKey": "berlin_recording_services",
        "exportProfileRevision": "revision_3",
        "qualityProfileDigest": "sha256:" + "3" * 64,
        "catalogBinding": {
            "siteKey": "berlin-recording",
            "catalogKey": "berlin-recording-services",
            "configurationRevisionId": _CONFIGURATION_REVISION,
            "coordinatePrecisionMeters": 25,
            "categories": [
                {"campaignCategory": "recording_studio", "catalogCategory": "recording-studio"}
            ],
            "nameLocales": [{"campaignLocale": "de", "catalogLocale": "de-DE"}],
        },
        "entries": [entry.model_dump(mode="json", by_alias=True)],
        "generatedAtUtc": _NOW.isoformat(),
        "correlationId": "corr-adapter",
    }
    return seal_export_package(ExportPackagePayload.model_validate(payload))


def test_a_sealed_export_produces_rev_three_item_documents() -> None:
    export = _sealed_export()

    documents = build_package_documents(export)

    assert len(documents.items) == 1
    item = documents.items[0]
    assert item["itemKey"] == "00000000-0000-0000-0000-00000000a001"
    assert item["entityKind"] == "organization"
    # The campaign's own category vocabulary never reaches the backend: the sealed
    # binding resolves it to the Catalog category the proposal is actually about.
    assert item["categoryProposals"] == ["recording-studio"]
    assert sorted(reference["fieldPath"] for reference in item["provenanceReferences"]) == [
        "categories/recording-studio",
        "contacts/website",
        "geography/proposedPoint",
        "localizedNames/de-DE",
        "subjectProposal",
    ]
    # The campaign's `und` name is displayed under the declared Catalog locale, never
    # guessed by an adapter.
    assert item["localizedNames"][0]["locale"] == "de-DE"
    assert item["localizedNames"][0]["fieldPath"] == "localizedNames/de-DE"
    assert item["contactProposals"][0]["fieldPath"] == "contacts/website"
    assert item["geographyProposal"]["state"] == "proposedPoint"
    assert item["provenanceReferences"], "evidence must reach the backend per field"
    assert item["collectorReviewReferences"][0]["decisionKind"] == "accept_candidate"


def test_the_produced_payload_digest_is_computed_over_the_real_item_bytes() -> None:
    export = _sealed_export()

    documents = build_package_documents(export)

    assert documents.payload_digest == compute_digest({"items": list(documents.items)})
    # The index repeats the item's own sealed digest; it does not re-hash the item document.
    assert (
        documents.item_index[0]["contentDigest"]
        == documents.items[0]["contentDigest"]
        == documents.item_digest(documents.item_index[0]["itemKey"])
    )


def test_the_manifest_declares_revision_three_and_the_export_identity() -> None:
    export = _sealed_export()
    documents = build_package_documents(export)

    manifest = build_manifest(
        export,
        documents,
        producer_identity="data-collection-platform",
        producer_build="test",
        target_site_key="berlin-recording",
        target_catalog_key="berlin-recording-services",
        target_catalog_configuration_revision_id=_CONFIGURATION_REVISION,
    )

    assert manifest["contractIdentity"] == "aggregator-candidate-ingestion"
    assert manifest["contractRevision"] == 3
    # The collector seals `sha256:`-prefixed digests; the backend document carries the
    # same checksum without the prefix.
    assert manifest["collectorExportDigest"] == export.package_digest.removeprefix("sha256:")
    assert manifest["itemCount"] == 1
    assert manifest["payloadDigest"] == documents.payload_digest


def test_link_only_evidence_stays_restricted_in_the_backend_document() -> None:
    export = _sealed_export(usage_policy="link_only")

    documents = build_package_documents(export)

    policies = {item["usagePolicy"] for item in documents.items[0]["provenanceReferences"]}
    assert policies == {"linkOnly"}
    assert all(
        reference["attribution"] == "OpenStreetMap contributors"
        for reference in documents.items[0]["provenanceReferences"]
    )


def test_an_exported_timestamp_stays_timezone_aware() -> None:
    export = _sealed_export()

    documents = build_package_documents(export)

    observed = documents.items[0]["provenanceReferences"][0]["observedAtUtc"]
    assert observed.endswith("+00:00")


def test_a_sealed_export_cannot_be_resealed_with_tampered_content() -> None:
    export = _sealed_export()
    tampered = export.model_dump(mode="json", by_alias=True)
    tampered["entries"][0]["localizedNames"][0]["value"] = "Tampered Studio"

    with pytest.raises(Exception) as captured:
        type(export).model_validate(tampered)

    assert "digest" in str(captured.value).lower()


def test_a_category_claimed_without_its_evidence_cannot_be_sealed() -> None:
    export = _sealed_export()
    tampered = export.model_dump(mode="json", by_alias=True)
    tampered["entries"][0]["categoryKeys"] = ["something_else"]

    with pytest.raises(Exception) as captured:
        type(export).model_validate(tampered)

    assert "no matching field evidence" in str(captured.value)


def test_a_submission_target_off_the_sealed_pin_is_refused() -> None:
    export = _sealed_export()
    documents = build_package_documents(export)

    with pytest.raises(IngestionAdapterError) as captured:
        build_manifest(
            export,
            documents,
            producer_identity="data-collection-platform",
            producer_build="test",
            target_site_key="berlin-recording",
            target_catalog_key="berlin-recording-services",
            target_catalog_configuration_revision_id="00000000-0000-0000-0000-00000000d999",
        )

    assert captured.value.code == "INGESTION_CATALOG_PIN_MISMATCH"


def test_an_observed_category_the_pin_does_not_classify_is_refused() -> None:
    from collector_cli.backend_ingestion import _item_document

    export = _sealed_export()
    entry = export.entries[0]
    forged = entry.model_construct(
        candidate_id=entry.candidate_id,
        candidate_revision=entry.candidate_revision,
        entity_kind=entry.entity_kind,
        localized_names=entry.localized_names,
        contacts=entry.contacts,
        typed_attributes=entry.typed_attributes,
        category_keys=("rehearsal_recording_space",),
        geography=entry.geography,
        field_evidence=entry.field_evidence,
        quality=entry.quality,
        review_decisions=entry.review_decisions,
        source_policies=entry.source_policies,
    )

    with pytest.raises(IngestionAdapterError) as captured:
        _item_document(forged, export.catalog_binding)

    assert captured.value.code == "INGESTION_CATEGORY_MAPPING_MISSING"


def test_an_unsupported_entity_kind_is_refused_not_defaulted() -> None:
    from collector_cli.backend_ingestion import _item_document

    export = _sealed_export()
    entry = export.entries[0]
    forged = entry.model_construct(
        candidate_id=entry.candidate_id,
        candidate_revision=entry.candidate_revision,
        entity_kind="space_station",
        localized_names=entry.localized_names,
        contacts=entry.contacts,
        typed_attributes=entry.typed_attributes,
        category_keys=entry.category_keys,
        geography=entry.geography,
        field_evidence=entry.field_evidence,
        quality=entry.quality,
        review_decisions=entry.review_decisions,
        source_policies=entry.source_policies,
    )

    with pytest.raises(IngestionAdapterError) as captured:
        _item_document(forged, export.catalog_binding)

    assert captured.value.code == "INGESTION_VALUE_UNSUPPORTED"


def test_a_place_candidate_carries_native_place_details_and_category_paths() -> None:
    export = _sealed_export(entity_kind="place")

    item = build_package_documents(export).items[0]

    details = item["placeDetails"]
    # A studio is a physical place: the Catalog can only turn it into a listing through this
    # shape, and every value it claims is either observed evidence or an explicit absence.
    assert item["entityKind"] == "place"
    assert details["coordinates"] == {
        "state": "observed",
        "latitude": details["coordinates"]["latitude"],
        "longitude": details["coordinates"]["longitude"],
        "precisionMeters": Decimal("25"),
        "fieldPath": "placeDetails/coordinates",
    }
    assert details["address"]["state"] == "unknown"
    assert details["address"]["missingReason"] == "notPublishedBySource"
    assert details["organizationBinding"] == "unknown"
    assert details["identityReferences"][0]["externalId"] == item["itemKey"]
    assert details["identityReferences"][0]["fieldPath"] == "externalReferences/identity"
    assert item["externalReferenceProposals"] == details["identityReferences"]
    assert item["categoryDetails"] == [
        {"categoryKey": "recording-studio", "fieldPath": "categories/recording-studio"}
    ]
    assert sorted(reference["fieldPath"] for reference in item["provenanceReferences"]) == [
        "categories/recording-studio",
        "contacts/website",
        "externalReferences/identity",
        "geography/proposedPoint",
        "localizedNames/de-DE",
        "placeDetails/coordinates",
        "subjectProposal",
    ]


def test_a_place_identity_reference_may_not_widen_its_source_restriction() -> None:
    export = _sealed_export(usage_policy="link_only", entity_kind="place")

    with pytest.raises(IngestionAdapterError) as captured:
        build_package_documents(export)

    assert captured.value.code == "INGESTION_PLACE_IDENTITY_UNPUBLISHABLE"


def test_a_timestamp_is_written_in_the_backend_canonical_form() -> None:
    # The backend writes the shortest round-tripping fraction, so a microsecond value ending
    # in zeros must not carry them: the digest covers these exact bytes.
    assert _utc_text(datetime(2026, 11, 17, 2, 31, 23, 647750, tzinfo=UTC)) == (
        "2026-11-17T02:31:23.64775+00:00"
    )
    assert _utc_text(datetime(2026, 11, 16, 12, 0, tzinfo=UTC)) == "2026-11-16T12:00:00+00:00"
    assert _utc_text(datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=UTC)) == (
        "2026-01-01T00:00:00.5+00:00"
    )
