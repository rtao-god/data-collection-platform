from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from collection_contracts import (
    ExportCandidateEntry,
    ExportCatalogBinding,
    ExportCatalogCategoryMapping,
    ExportCatalogNameLocale,
    ExportPackage,
    ExportPackagePayload,
    canonical_export_package_json,
    decode_export_package,
    export_package_digest,
    seal_export_candidate,
    seal_export_package,
    verify_export_package,
)

_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def _entry(candidate_id=None, *, revision: int = 0) -> ExportCandidateEntry:
    document = {
        "candidateId": str(candidate_id or uuid4()),
        "candidateRevision": revision,
        "snapshotDigest": _DIGEST_A,
        "entityKind": "organization",
        "clusterId": str(uuid4()),
        "localizedNames": [
            {"locale": "de", "state": "observed", "value": "Studio One", "fieldPath": "names"}
        ],
        "contacts": [
            {
                "kind": "website",
                "state": "observed",
                "target": "https://studio.test",
                "fieldPath": "website_urls",
            }
        ],
        "typedAttributes": [],
        "categoryKeys": ["recording_studio"],
        "geography": {
            "coverage": "inside",
            "marketAreaRevision": "berlin_v1",
            "marketAreaDigest": _DIGEST_B,
            "latitude": 52.5,
            "longitude": 13.4,
            "fieldPath": "geography",
        },
        "fieldEvidence": [
            _evidence("names"),
            _evidence("website_urls"),
            _evidence("geography"),
            _evidence("categories/recording_studio"),
        ],
        "quality": {"state": "passed", "issues": []},
        "qualityEvaluationDigest": _DIGEST_B,
        "qualityPolicyDigest": _DIGEST_A,
        "reviewDecisions": [
            {
                "decisionId": "00000000-0000-0000-0000-000000000201",
                "decisionKind": "accept_candidate",
                "decisionDigest": _DIGEST_B,
                "decidedAtUtc": "2026-01-06T00:00:00+00:00",
            }
        ],
        "sourcePolicies": [
            {
                "sourceKey": "openstreetmap",
                "policyDigest": _DIGEST_A,
                "legalStatus": "approved",
                "usagePolicy": "publishable",
                "attribution": "OpenStreetMap contributors",
            }
        ],
        "sourceArtifactIds": [str(uuid4())],
    }
    return seal_export_candidate(document)


def _evidence(field_path: str) -> dict:
    return {
        "fieldPath": field_path,
        "sourceKey": "openstreetmap",
        "sourceRecordId": "osm:node:101",
        "sourceUrl": "https://www.openstreetmap.org/node/101",
        "observedAtUtc": "2026-01-05T00:00:00+00:00",
        "legalStatus": "approved",
        "usagePolicy": "publishable",
        "evidenceDigest": _DIGEST_B,
        "sourcePolicyDigest": _DIGEST_A,
        "attribution": "OpenStreetMap contributors",
    }


def _binding() -> ExportCatalogBinding:
    return ExportCatalogBinding(
        siteKey="berlin-recording",
        catalogKey="berlin-recording-services",
        configurationRevisionId="00000000-0000-0000-0000-000000000001",
        coordinatePrecisionMeters=25,
        nameLocales=[ExportCatalogNameLocale(campaignLocale="de", catalogLocale="de-DE")],
        categories=[
            ExportCatalogCategoryMapping(
                campaignCategory="recording_studio", catalogCategory="recording-studio"
            )
        ],
    )


def _payload(entries=None) -> ExportPackagePayload:
    return ExportPackagePayload(
        runId=uuid4(),
        campaignBundleDigest=_DIGEST_A,
        campaignKey="berlin_recording_services",
        exportProfileKey="default",
        exportProfileRevision="rev_v1",
        qualityProfileDigest=_DIGEST_B,
        catalogBinding=_binding(),
        entries=entries if entries is not None else (_entry(),),
        generatedAtUtc=_NOW,
        correlationId="export-test",
    )


def test_sealed_package_carries_deterministic_digest() -> None:
    payload = _payload()

    first = seal_export_package(payload)
    second = seal_export_package(payload)

    assert first.package_digest == second.package_digest
    assert first.package_digest == export_package_digest(payload)
    verify_export_package(first)


def test_canonical_bytes_round_trip_exactly() -> None:
    package = seal_export_package(_payload())

    content = canonical_export_package_json(package).encode("utf-8")
    decoded = decode_export_package(content)

    assert decoded == package
    assert canonical_export_package_json(decoded).encode("utf-8") == content


def test_tampered_package_digest_is_rejected() -> None:
    package = seal_export_package(_payload())
    document = json.loads(canonical_export_package_json(package))
    document["packageDigest"] = _DIGEST_A

    with pytest.raises(ValueError, match="package_digest"):
        ExportPackage.model_validate(document)


def test_tampered_entry_content_is_rejected() -> None:
    package = seal_export_package(_payload())
    document = json.loads(canonical_export_package_json(package))
    document["entries"][0]["localizedNames"][0]["value"] = "Forged Studio"

    # Tampering is caught by whichever digest covers the changed field first; the package
    # digest covers every entry, so a forged field never reaches a consumer as valid.
    with pytest.raises(ValueError, match=r"package_digest|content digest"):
        decode_export_package(json.dumps(document).encode("utf-8"))


def test_entries_must_be_ordered_by_candidate_id() -> None:
    from uuid import UUID

    low = UUID("00000000-0000-0000-0000-000000000001")
    high = UUID("00000000-0000-0000-0000-0000000000ff")

    with pytest.raises(ValueError, match="ordered by candidate id"):
        _payload((_entry(high), _entry(low)))


def test_duplicate_candidate_revisions_are_rejected() -> None:
    from uuid import UUID

    same = UUID("00000000-0000-0000-0000-000000000010")

    with pytest.raises(ValueError, match="duplicate candidate revisions"):
        _payload((_entry(same), _entry(same)))


def test_entry_requires_an_observed_name() -> None:
    unobserved = _entry().model_dump(mode="json", by_alias=True)
    unobserved["localizedNames"] = [
        {"locale": "de", "state": "unknown", "value": None, "fieldPath": "names"}
    ]

    with pytest.raises(ValueError, match="at least one observed name"):
        ExportCandidateEntry.model_validate(unobserved)


def test_entry_requires_matching_field_evidence() -> None:
    unproven = _entry().model_dump(mode="json", by_alias=True)
    unproven["fieldEvidence"] = [
        item for item in unproven["fieldEvidence"] if item["fieldPath"] != "website_urls"
    ]

    with pytest.raises(ValueError, match="no matching field evidence"):
        ExportCandidateEntry.model_validate(unproven)


def test_entry_rejects_evidence_digest_that_disagrees_with_its_policy() -> None:
    wrong = _entry().model_dump(mode="json", by_alias=True)
    wrong["fieldEvidence"][0]["sourcePolicyDigest"] = "sha256:" + "7" * 64

    with pytest.raises(ValueError, match="disagrees with its source policy digest"):
        ExportCandidateEntry.model_validate(wrong)


def test_entry_rejects_widened_link_only_source() -> None:
    widened = _entry().model_dump(mode="json", by_alias=True)
    widened["sourcePolicies"][0]["usagePolicy"] = "link_only"

    with pytest.raises(ValueError, match="widens a link-only source"):
        ExportCandidateEntry.model_validate(widened)
