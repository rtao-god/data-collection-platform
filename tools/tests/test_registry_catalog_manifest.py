"""Tests for the registry -> Catalog import manifest producer."""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path

import pytest
from tools import registry_catalog_manifest as producer
from tools import studio_registry as sr

MAPPING_TEXT = """
schema_revision: catalog-category-mapping-v1
site_key: berlin-recording
catalog_key: berlin-recording-services
configuration_revision_id: 46fb742c-f4d9-45dd-99ee-a364cf44b3c2
coordinate_precision_meters: 25
name_locales:
  - campaign_locale: de
    catalog_locale: de-DE
categories:
  - campaign_category: recording_studio
    catalog_category: recording-studio
  - campaign_category: mixing_studio
    catalog_category: mixing-studio
unmapped: []
"""


def _mapping(tmp_path: Path, text: str = MAPPING_TEXT) -> producer.CatalogMapping:
    path = tmp_path / "catalog_mapping.yaml"
    path.write_text(text, encoding="utf-8")
    return producer.load_mapping(path)


def _evidence(
    *,
    field_path: str = "address.street",
    url: str = "https://example-studio.de/contact/",
    digest: str = "a" * 64,
    observed_at_utc: str = "2026-11-19T18:00:00Z",
    source_kind: str = "official_site",
    usage_policy: str = "facts_only",
    policy_decision: dict | None = None,
) -> dict:
    return {
        "evidence_id": sr.derive_evidence_id(url, digest, field_path),
        "field_path": field_path,
        "source_kind": source_kind,
        "source_url": url,
        "external_id": None,
        "observed_at_utc": observed_at_utc,
        "response_digest_sha256": digest,
        "excerpt": "Example Studio, Example Str. 1, 10963 Berlin",
        "usage_policy": usage_policy,
        "policy_decision": policy_decision
        if policy_decision is not None
        else {
            "decision": "approved",
            "scope": "official_site facts_only",
            "policy_digest_sha256": "c" * 64,
            "reviewer": "rtao-god",
            "decided_at_utc": "2026-11-20T00:00:00Z",
        },
        "attribution": None,
    }


def _record(domain: str = "example-studio.de", name: str = "Example Studio", **overrides) -> dict:
    address_evidence = _evidence(url=f"https://{domain}/contact/")
    service_evidence = _evidence(
        field_path="service_claims",
        url=f"https://{domain}/",
        digest="b" * 64,
    )
    record = {
        "registry_id": sr.derive_registry_id("official_domain", domain),
        "entity_kind": "place",
        "canonical_name": name,
        "aliases": [],
        "identity_keys": [
            {"kind": "official_domain", "value": domain, "authority": "authoritative"}
        ],
        "subject_match": {
            "decision": "create_new_pending_review",
            "reason": "no existing catalog subject matched",
            "subject_id": None,
            "listing_id": None,
        },
        "possible_duplicate_of": [],
        "official_domains": {"primary": domain, "alternates": []},
        "source_original_name": name,
        "address": {
            "street": "Example Str.",
            "street_number": "1",
            "postal_code": "10963",
            "city": "Berlin",
            "district_key": "friedrichshain-kreuzberg",
            "district_basis": "address_source_verified",
            "country_code": "DE",
            "coordinate": None,
        },
        "market": {
            "classification": "primary_market",
            "basis": "campaign_geography_revision:berlin_official_boundary_v1",
            "source_kind": "manual_operator",
        },
        "service_claims": [
            {
                "catalog_category_key": "recording-studio",
                "evidence_refs": [service_evidence["evidence_id"]],
                "source_text": "recording sessions",
            }
        ],
        "descriptions": {},
        "attributes": [],
        "contacts": [],
        "photo_candidates": [],
        "field_evidence": [address_evidence, service_evidence],
        "reviews": [],
        "collector": {"candidate_ids": [], "revisions": [], "import_batch": "tests"},
        "last_refresh": {"status": "observed", "at_utc": "2026-11-19T18:00:00Z"},
        "restrictions": {"rejected": [], "missing": [], "conflicting": []},
    }
    record.update(overrides)
    return record


def _document(*records: dict) -> dict:
    return {
        "schema_revision": sr.SCHEMA_REVISION,
        "campaign_key": sr.CAMPAIGN_KEY,
        "records": list(records or (_record(),)),
    }


def _build(document: dict, mapping: producer.CatalogMapping) -> dict:
    registry_bytes = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return producer.build_manifest(
        document, registry_bytes, mapping, recorded_at_utc="2026-11-21T00:00:00Z"
    )


def test_manifest_bytes_do_not_depend_on_record_or_evidence_order(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    first = _record()
    second = _record(domain="other-studio.de", name="Other Studio")

    document = _document(first, second)
    registry_bytes = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
    forward = producer.build_manifest(
        document, registry_bytes, mapping, recorded_at_utc="2026-11-21T00:00:00Z"
    )
    # Same stored bytes, different in-memory order of records and of one record's evidence.
    permuted = dict(document, records=[dict(second), dict(first)])
    permuted["records"][1] = dict(first, field_evidence=list(reversed(first["field_evidence"])))
    reversed_manifest = producer.build_manifest(
        permuted, registry_bytes, mapping, recorded_at_utc="2026-11-21T00:00:00Z"
    )

    assert producer.canonical_bytes(forward) == producer.canonical_bytes(reversed_manifest)
    assert forward["manifestDigest"] == reversed_manifest["manifestDigest"]


def test_same_address_different_operators_stay_separate_identities(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    left = _record(domain="studio-left.de", name="Studio Left")
    right = _record(domain="studio-right.de", name="Studio Right")
    right["address"] = dict(left["address"])

    manifest = _build(_document(left, right), mapping)

    assert len(manifest["records"]) == 2
    identities = {item["registryId"] for item in manifest["records"]}
    assert identities == {left["registry_id"], right["registry_id"]}
    assert len(identities) == 2


def test_official_domain_is_not_the_identity_key(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    # One operator publishing two locations under one domain must not collapse into one card.
    first = _record(domain="shared-operator.de", name="Shared Operator Nord")
    second = _record(domain="shared-operator.de", name="Shared Operator Süd")
    second["registry_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "shared-operator-sued"))
    second["address"] = dict(first["address"], street_number="99")

    manifest = _build(_document(first, second), mapping)

    assert [item["registryId"] for item in manifest["records"]] == sorted(
        [first["registry_id"], second["registry_id"]]
    )


def test_address_dataset_is_classified_as_licensed_not_as_the_studio_site(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    wfs_url = (
        "https://gdi.berlin.de/services/wfs/adressen_berlin?service=WFS&version=1.1.0"
        "&request=GetFeature&typeName=adressen_berlin%3Aadressen_berlin"
    )
    wfs = _evidence(field_path="address.district_key", url=wfs_url, digest="d" * 64)
    record["field_evidence"] = [*record["field_evidence"], wfs]

    manifest = _build(_document(record), mapping)
    assertions = manifest["records"][0]["assertions"]

    dataset = [item for item in assertions if item["sourceReference"] == wfs_url]
    assert len(dataset) == 1
    assert dataset[0]["sourceKind"] == producer.SOURCE_KIND_LICENSED_DATASET
    assert dataset[0]["classificationBasis"] == "official_berlin_address_dataset"
    # The registry's own (wrong) declaration is preserved, never rewritten.
    assert dataset[0]["declaredSourceKind"] == "official_site"
    website = [
        item for item in assertions if item["sourceReference"] == "https://example-studio.de/"
    ]
    assert [item["sourceKind"] for item in website] == [producer.SOURCE_KIND_PUBLIC_WEBSITE]


def test_postal_code_district_is_blocked_and_carries_no_facts(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    record["address"] = dict(record["address"], district_basis="postal_code")

    manifest = _build(_document(record), mapping)
    entry = manifest["records"][0]

    assert entry["status"] == producer.STATUS_BLOCKED
    assert entry["statusReasons"] == ["district_derivation_not_confirmed"]
    assert entry["business"] is None
    assert entry["assertions"] == []


def test_changed_evidence_digest_requires_review_and_never_auto_clears(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    record["restrictions"] = {
        "rejected": [],
        "missing": [],
        "conflicting": [
            {
                "code": "evidence_digest_changed",
                "severity": "review",
                "detail": "new digest for https://example-studio.de/ on address.street",
            }
        ],
    }

    entry = _build(_document(record), mapping)["records"][0]

    assert entry["status"] == producer.STATUS_REVIEW
    assert "review_required:evidence_digest_changed" in entry["statusReasons"]
    # A reviewable record still carries its facts so a human can act on them.
    assert entry["business"] is not None


def test_evidence_without_approved_policy_is_review_not_eligible(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    unapproved = _evidence(url="https://example-studio.de/impressum", digest="e" * 64)
    unapproved["policy_decision"] = None
    record["field_evidence"] = [*record["field_evidence"], unapproved]

    entry = _build(_document(record), mapping)["records"][0]

    assert entry["status"] == producer.STATUS_REVIEW
    assert (
        "source_policy_decision_missing:https://example-studio.de/impressum"
        in entry["statusReasons"]
    )


def test_content_digest_ignores_provenance_but_evidence_digest_tracks_it(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    original = _build(_document(_record()), mapping)["records"][0]

    record = _record()
    # Same observable facts, later observation of the same page.
    record["field_evidence"] = [
        dict(entry, observed_at_utc="2026-11-20T09:30:00Z") for entry in record["field_evidence"]
    ]
    refreshed = _build(_document(record), mapping)["records"][0]

    assert original["digests"]["content"] == refreshed["digests"]["content"]
    assert original["digests"]["evidence"] != refreshed["digests"]["evidence"]
    assert original["digests"]["eligibility"] == refreshed["digests"]["eligibility"]


def test_category_change_invalidates_content_and_eligibility_digests(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    original = _build(_document(_record()), mapping)["records"][0]

    record = _record()
    record["service_claims"] = [
        dict(record["service_claims"][0], catalog_category_key="mixing-studio")
    ]
    changed = _build(_document(record), mapping)["records"][0]

    assert original["digests"]["content"] != changed["digests"]["content"]
    # Eligibility does not encode the selected category, so a category change leaves it untouched.
    assert original["digests"]["eligibility"] == changed["digests"]["eligibility"]


def test_policy_change_invalidates_only_eligibility(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    original = _build(_document(_record()), mapping)["records"][0]

    record = _record()
    record["restrictions"] = {
        "rejected": [
            {"code": "rights_revoked", "severity": "blocking", "detail": "operator withdrew"}
        ],
        "missing": [],
        "conflicting": [],
    }
    changed = _build(_document(record), mapping)["records"][0]

    assert changed["status"] == producer.STATUS_BLOCKED
    assert changed["digests"]["eligibility"] != original["digests"]["eligibility"]


def test_unicode_normalization_keeps_one_identity_for_one_name(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    composed = "Tonstudio Schloß"
    decomposed = "Tonstudio Schlo\u00df"

    left = _build(_document(_record(name=composed)), mapping)["records"][0]
    right = _build(_document(_record(name=decomposed)), mapping)["records"][0]

    assert left["digests"]["content"] == right["digests"]["content"]


def test_link_existing_names_the_listing_and_leaves_the_place_to_catalog(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    listing_id = "01a1efea-372c-7100-a7c0-e59d540707a5"
    record = _record()
    record["subject_match"] = {
        "decision": "link_existing",
        "reason": "operator linked the existing card",
        "subject_id": None,
        "listing_id": listing_id,
    }

    entry = _build(_document(record), mapping)["records"][0]

    assert entry["identity"]["decision"] == "link_existing"
    assert entry["identity"]["linkExisting"] == {"listingId": listing_id, "placeId": None}


def test_link_existing_without_a_listing_is_refused(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    record["subject_match"] = {
        "decision": "link_existing",
        "reason": "x",
        "subject_id": None,
        "listing_id": None,
    }

    with pytest.raises(producer.ManifestError, match="link_existing identity requires listing_id"):
        _build(_document(record), mapping)


def test_unmapped_campaign_category_refuses_to_guess(tmp_path: Path) -> None:
    with pytest.raises(producer.ManifestError, match="unclassifiable campaign categories"):
        _mapping(tmp_path, MAPPING_TEXT.replace("unmapped: []", "unmapped:\n  - vocal_recording"))

    mapping = _mapping(tmp_path)
    assert mapping.catalog_category("recording_studio") == "recording-studio"
    assert mapping.catalog_category("vocal_recording") is None


def test_claim_citing_unexported_evidence_blocks_the_record(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    record["service_claims"] = [
        {
            "catalog_category_key": "recording-studio",
            "evidence_refs": ["ev-unknown"],
            "source_text": "x",
        }
    ]

    entry = _build(_document(record), mapping)["records"][0]

    assert entry["status"] == producer.STATUS_BLOCKED
    assert "source_policy_decision_missing:" in "".join(entry["statusReasons"])
    assert entry["business"] is None


def test_building_a_manifest_never_opens_a_network_connection(tmp_path: Path, monkeypatch) -> None:
    mapping = _mapping(tmp_path)

    def _refuse(*args, **kwargs):
        raise AssertionError("the import manifest must be built from stored evidence only")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)

    manifest = _build(_document(_record()), mapping)

    assert manifest["records"][0]["status"] == producer.STATUS_ELIGIBLE


def test_plan_and_diff_report_only_real_changes(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    before = _build(_document(_record()), mapping)

    record = _record()
    record["service_claims"] = [
        dict(record["service_claims"][0], catalog_category_key="mixing-studio")
    ]
    after = _build(_document(record, _record(domain="new-studio.de", name="New Studio")), mapping)

    lines = list(producer.diff_lines(before, after))

    assert f"changed {record['registry_id']}: content" in lines
    assert len([line for line in lines if line.startswith("changed ")]) == 1
    assert len([line for line in lines if line.startswith("added ")]) == 1
    summary = producer.counts(after)
    assert summary["records"] == 2
