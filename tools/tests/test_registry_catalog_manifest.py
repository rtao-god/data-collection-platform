"""Tests for the registry -> Catalog import manifest producer."""

from __future__ import annotations

import hashlib
import json
import socket
import uuid
from pathlib import Path

import pytest
from tools import registry_catalog_manifest as producer
from tools import registry_enrichment_queue as queue
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


@pytest.mark.parametrize(
    ("source_url", "source_kind", "expected"),
    [
        (
            "https://www.wikidata.org/wiki/Q1",
            "official_site",
            (producer.SOURCE_KIND_LICENSED_DATASET, "wikidata"),
        ),
        (
            "https://www.openstreetmap.org/way/123",
            "openstreetmap",
            (producer.SOURCE_KIND_LICENSED_DATASET, "openstreetmap"),
        ),
        (
            "https://overpass-api.de/api/interpreter",
            "official_site",
            (producer.SOURCE_KIND_LICENSED_DATASET, "openstreetmap"),
        ),
        (
            "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/release/2026-11-18.0/theme=places/type=place",
            "official_site",
            (producer.SOURCE_KIND_LICENSED_DATASET, "overture_places"),
        ),
        (
            "s3://overturemaps-us-west-2/release/2026-11-18.0/theme=places/type=place/*.parquet",
            "official_site",
            (producer.SOURCE_KIND_LICENSED_DATASET, "overture_places"),
        ),
        (
            "https://docs.foursquare.com/data-products/docs/access-fsq-os-places",
            "official_site",
            (producer.SOURCE_KIND_PUBLIC_WEBSITE, "foursquare_os_places_access_documentation"),
        ),
        (
            "https://huggingface.co/datasets/foursquare/fsq-os-places/resolve/main/places.parquet",
            "official_site",
            (producer.SOURCE_KIND_LICENSED_DATASET, "foursquare_os_places"),
        ),
        (
            "https://musicbrainz.org/ws/2/place/?query=type%3AStudio%20AND%20area%3ABerlin",
            "official_site",
            (producer.SOURCE_KIND_LICENSED_DATASET, "musicbrainz"),
        ),
        (
            "https://commons.wikimedia.org/w/api.php?action=query&format=json",
            "official_site",
            (producer.SOURCE_KIND_LICENSED_DATASET, "wikimedia_commons"),
        ),
        (
            "https://www.dasoertliche.de/Themen/Musikstudios/Berlin-Westend.html",
            "official_site",
            (producer.SOURCE_KIND_LICENSED_DATASET, "dasoertliche_directory"),
        ),
        (
            "https://directory.example/studio",
            "licensed_directory",
            (producer.SOURCE_KIND_LICENSED_DATASET, "licensed_directory"),
        ),
    ],
)
def test_source_classification_preserves_provider_identity(
    source_url: str, source_kind: str, expected: tuple[str, str]
) -> None:
    assert (
        producer.classify_source({"source_url": source_url, "source_kind": source_kind}) == expected
    )


def test_official_site_requires_a_recorded_primary_or_alternate_domain() -> None:
    evidence = {
        "source_url": "https://www.example-studio.de/contact/",
        "source_kind": "official_site",
    }
    official_domains = {"primary": "example-studio.de", "alternates": ["example-audio.de"]}

    assert producer.classify_source(evidence, official_domains) == (
        producer.SOURCE_KIND_PUBLIC_WEBSITE,
        "official_studio_website",
    )
    assert producer.classify_source(
        {"source_url": "https://example-audio.de/about", "source_kind": "official_site"},
        official_domains,
    ) == (producer.SOURCE_KIND_PUBLIC_WEBSITE, "official_studio_website")

    with pytest.raises(producer.ManifestError, match="not declared in registry official_domains"):
        producer.classify_source(
            {"source_url": "https://www.discogs.com/studio/123", "source_kind": "official_site"},
            official_domains,
        )


def test_unknown_source_kind_is_not_mislabeled_as_an_official_website() -> None:
    with pytest.raises(producer.ManifestError, match="cannot classify source kind"):
        producer.classify_source(
            {"source_url": "https://unknown-source.example/record/1", "source_kind": "new_provider"}
        )


def test_queue_contact_candidates_flow_through_real_cli_order_into_manifest(
    tmp_path: Path,
) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    registry_document = _document(record)
    registry_path = tmp_path / "studios.json"
    incoming_path = tmp_path / "studios-incoming.json"
    conflicts_path = tmp_path / "studios-conflicts.json"
    capture_path = tmp_path / "capture.json"
    policy_path = tmp_path / "official-site-policy.yaml"
    patch_path = tmp_path / "patch.json"
    contacts_path = tmp_path / "contacts.json"
    source_url = "https://example-studio.de/contact/"
    response_digest = "d" * 64
    observed_at = "2026-11-27T10:00:00Z"
    values = [
        {
            "kind": "phone",
            "target": "+49 30 1234567",
            "label": "Studio",
            "validation_state": "unvalidated",
            "evidence_refs": [],
        },
        {
            "kind": "phone",
            "target": "+49 30 7654321",
            "label": "Bookings",
            "validation_state": "unvalidated",
            "evidence_refs": [],
        },
        {
            "kind": "phone",
            "target": "+49 30 9876543",
            "label": "Mobile room",
            "validation_state": "unvalidated",
            "evidence_refs": [],
        },
    ]
    observations = [
        {
            "observation_id": f"official-site:example-studio:phone-{index}",
            "source_run_id": "official-site-pass-1",
            "registry_id": record["registry_id"],
            "subject_match": {"state": "matched_review_required", "source_label": "Example Studio"},
            "field_path": "contacts",
            "operation": "add_collection_item",
            "normalized_value": value,
            "source": {
                "source_key": "official_site",
                "source_record_id": None,
                "dataset_release": None,
                "source_url": source_url,
                "acquired_at_utc": observed_at,
                "response_digest_sha256": response_digest,
                "verification_status": "verified",
            },
            "baseline_record_digest_sha256": producer.digest_of(record),
            "status": "pending_verification",
        }
        for index, value in enumerate(values, 1)
    ]
    incoming = {
        "schema_revision": queue.INCOMING_SCHEMA,
        "campaign_key": sr.CAMPAIGN_KEY,
        "source_runs": [
            {
                "run_id": "official-site-pass-1",
                "requested_at_utc": observed_at,
                "coverage": {
                    "requested": 1,
                    "obtained": 1,
                    "matched": 1,
                    "new": 0,
                    "ambiguous": 0,
                    "unavailable": 0,
                    "denied": 0,
                },
            }
        ],
        "observations": observations,
    }
    registry_path.write_text(sr.canonical_json(registry_document), encoding="utf-8")
    incoming_path.write_text(sr.canonical_json(incoming), encoding="utf-8")
    policy_path.write_text("policy_key: official_site_berlin_studios_policy\n", encoding="utf-8")
    assert (
        queue.main(
            [
                "refresh",
                "--registry",
                str(registry_path),
                "--incoming",
                str(incoming_path),
                "--conflicts",
                str(conflicts_path),
            ]
        )
        == 0
    )

    capture_path.write_text(
        json.dumps(
            {
                "sourceKey": "official_site",
                "entries": [
                    {
                        "state": "captured",
                        "registryId": record["registry_id"],
                        "url": source_url,
                        "responseDigestSha256": response_digest,
                        "observedAtUtc": observed_at,
                        "fieldPaths": ["contacts"],
                        "excerpt": "Studio +49 30 1234567; Bookings +49 30 7654321",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "record-evidence",
                "--capture",
                str(capture_path),
            ]
        )
        == 0
    )
    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "decide",
                "--policy-file",
                str(policy_path),
                "--reviewer",
                "operator",
                "--decided-at-utc",
                "2026-11-27T11:00:00Z",
                "--registry-id",
                record["registry_id"],
            ]
        )
        == 0
    )

    assert (
        queue.main(
            [
                "refresh",
                "--registry",
                str(registry_path),
                "--incoming",
                str(incoming_path),
                "--conflicts",
                str(conflicts_path),
            ]
        )
        == 0
    )
    stale_projection = json.loads(conflicts_path.read_text(encoding="utf-8"))
    assert [item["reason"] for item in stale_projection["conflicts"]] == [
        "baseline_record_digest_changed"
    ]
    assert (
        queue.main(
            [
                "rebase-metadata-batch",
                "--registry",
                str(registry_path),
                "--incoming",
                str(incoming_path),
                "--policy-file",
                str(policy_path),
                "--reviewer",
                "operator",
                "--decided-at-utc",
                "2026-11-27T11:00:00Z",
                "--registry-id",
                record["registry_id"],
            ]
        )
        == 0
    )
    assert (
        queue.main(
            [
                "refresh",
                "--registry",
                str(registry_path),
                "--incoming",
                str(incoming_path),
                "--conflicts",
                str(conflicts_path),
            ]
        )
        == 0
    )
    projection = json.loads(conflicts_path.read_text(encoding="utf-8"))
    assert projection["conflicts"] == []
    assert len(projection["field_gaps"][0]["candidate_values"]) == 3
    refreshed_incoming = json.loads(incoming_path.read_text(encoding="utf-8"))
    baseline = refreshed_incoming["observations"][0]["baseline_record_digest_sha256"]
    evidence_id = sr.evidence_id_for(record["registry_id"], source_url, response_digest, "contacts")
    conflict_id = sr.enrichment_conflict_id(record["registry_id"], "contacts", "contacts")
    decision_time = "2026-11-27T11:20:00Z"
    patch = {
        "schema_revision": sr.PATCH_SCHEMA_REVISION,
        "patches": [
            {
                "registry_id": record["registry_id"],
                "field_path": "contacts",
                "operation": "add_collection_item",
                "value": value,
                "evidence_refs": [evidence_id],
                "reviewer": "operator",
                "decided_at_utc": decision_time,
                "baseline_record_digest_sha256": baseline,
                "conflict_resolution": {
                    "conflict_id": conflict_id,
                    "field_path": "contacts",
                    "scope": "contacts",
                    "baseline_record_digest_sha256": baseline,
                    "accepted_value": value,
                    "evidence_refs": [evidence_id],
                },
            }
            for value in values[:2]
        ],
    }
    patch_path.write_text(json.dumps(patch), encoding="utf-8")
    assert sr.main(["--registry", str(registry_path), "enrich", "--patch", str(patch_path)]) == 0
    contact_reviews = [
        {
            "registry_id": record["registry_id"],
            "kind": value["kind"],
            "target": value["target"],
            "validation_state": "validated",
            "source_evidence_digest_sha256": response_digest,
        }
        for value in values[:2]
    ]
    contacts_path.write_text(json.dumps(contact_reviews), encoding="utf-8")
    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "review",
                "--reviewer",
                "operator",
                "--decided-at-utc",
                "2026-11-27T11:30:00Z",
                "--contacts",
                str(contacts_path),
            ]
        )
        == 0
    )

    incoming_after_decision = json.loads(incoming_path.read_text(encoding="utf-8"))
    for observation in incoming_after_decision["observations"][:2]:
        observation["status"] = "consumed"
        observation["resolution"] = {
            "conflict_id": conflict_id,
            "field_path": "contacts",
            "scope": "contacts",
            "baseline_record_digest_sha256": baseline,
            "accepted_value": observation["normalized_value"],
            "evidence_refs": [evidence_id],
        }
    incoming_path.write_text(sr.canonical_json(incoming_after_decision), encoding="utf-8")
    queue_args = [
        "refresh",
        "--registry",
        str(registry_path),
        "--incoming",
        str(incoming_path),
        "--conflicts",
        str(conflicts_path),
    ]
    assert queue.main(queue_args) == 0
    first_projection = json.loads(conflicts_path.read_text(encoding="utf-8"))
    assert len(first_projection["conflicts"]) == 1
    assert first_projection["conflicts"][0]["reason"] == "baseline_record_digest_changed"
    assert first_projection["conflicts"][0]["competing_values"][0]["observation_id"] == (
        "official-site:example-studio:phone-3"
    )
    assert first_projection["field_gaps"] == []
    persisted_queue = json.loads(incoming_path.read_text(encoding="utf-8"))
    assert [item["status"] for item in persisted_queue["observations"]] == [
        "consumed",
        "consumed",
        "pending_verification",
    ]
    assert queue.main(queue_args) == 0
    assert json.loads(conflicts_path.read_text(encoding="utf-8")) == first_projection
    assert (
        queue.main(
            [
                "validate",
                "--registry",
                str(registry_path),
                "--incoming",
                str(incoming_path),
                "--conflicts",
                str(conflicts_path),
            ]
        )
        == 0
    )

    final_record = json.loads(registry_path.read_text(encoding="utf-8"))
    entry = producer.build_record(final_record["records"][0], mapping)

    assert entry["status"] == producer.STATUS_ELIGIBLE
    assert {item["target"] for item in entry["richFields"]["contacts"]} == {
        "tel:+49301234567",
        "tel:+49307654321",
    }
    assert any(assertion["evidenceDigest"] == response_digest for assertion in entry["assertions"])

    phone_three = values[2]
    phone_three_baseline = sr.registry_record_digest(final_record["records"][0])
    phone_three_patch = {
        "schema_revision": sr.PATCH_SCHEMA_REVISION,
        "patches": [
            {
                "registry_id": record["registry_id"],
                "field_path": "contacts",
                "operation": "add_collection_item",
                "value": phone_three,
                "evidence_refs": [evidence_id],
                "reviewer": "operator",
                "decided_at_utc": "2026-11-27T12:00:00Z",
                "baseline_record_digest_sha256": phone_three_baseline,
                "conflict_resolution": {
                    "conflict_id": conflict_id,
                    "field_path": "contacts",
                    "scope": "contacts",
                    "baseline_record_digest_sha256": phone_three_baseline,
                    "accepted_value": phone_three,
                    "evidence_refs": [evidence_id],
                },
            }
        ],
    }
    patch_path.write_text(json.dumps(phone_three_patch), encoding="utf-8")
    assert sr.main(["--registry", str(registry_path), "enrich", "--patch", str(patch_path)]) == 0
    contacts_path.write_text(
        json.dumps(
            [
                {
                    "registry_id": record["registry_id"],
                    "kind": phone_three["kind"],
                    "target": phone_three["target"],
                    "validation_state": "validated",
                    "source_evidence_digest_sha256": response_digest,
                }
            ]
        ),
        encoding="utf-8",
    )
    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "review",
                "--reviewer",
                "operator",
                "--decided-at-utc",
                "2026-11-27T12:10:00Z",
                "--contacts",
                str(contacts_path),
            ]
        )
        == 0
    )

    closed_queue = json.loads(incoming_path.read_text(encoding="utf-8"))
    for observation in closed_queue["observations"]:
        if observation["observation_id"] != "official-site:example-studio:phone-3":
            continue
        observation["status"] = "consumed"
        observation["resolution"] = {
            "conflict_id": conflict_id,
            "field_path": "contacts",
            "scope": "contacts",
            "baseline_record_digest_sha256": phone_three_baseline,
            "accepted_value": phone_three,
            "evidence_refs": [evidence_id],
        }
    incoming_path.write_text(sr.canonical_json(closed_queue), encoding="utf-8")
    assert queue.main(queue_args) == 0
    closed_projection = json.loads(conflicts_path.read_text(encoding="utf-8"))
    assert closed_projection["conflicts"] == []
    assert closed_projection["field_gaps"] == []
    assert (
        queue.main(
            [
                "validate",
                "--registry",
                str(registry_path),
                "--incoming",
                str(incoming_path),
                "--conflicts",
                str(conflicts_path),
            ]
        )
        == 0
    )


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
                "field_path": "address.street",
            }
        ],
    }

    entry = _build(_document(record), mapping)["records"][0]

    assert entry["status"] == producer.STATUS_REVIEW
    assert "review_required:evidence_digest_changed" in entry["statusReasons"]
    # A reviewable record still carries its facts so a human can act on them.
    assert entry["business"] is not None


def test_unapproved_optional_evidence_does_not_review_or_leak_the_card(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    unapproved = _evidence(
        field_path="attributes.room_area_m2",
        url="https://directory.example/studio",
        digest="e" * 64,
        source_kind="licensed_directory",
        policy_decision=None,
    )
    unapproved["usage_policy"] = "research_only"
    record["field_evidence"] = [*record["field_evidence"], unapproved]
    record["restrictions"]["conflicting"].append(
        {
            "code": "evidence_digest_changed",
            "severity": "review",
            "detail": "a room-area observation changed",
            "field_path": "attributes.room_area_m2",
        }
    )
    record["photo_candidates"] = [
        {
            "candidate_id": "photo-candidate-1",
            "source_url": "https://directory.example/studio/photo",
            "rights_holder": None,
            "license_url": None,
            "license_version": None,
            "rights_state": "candidate_unreviewed",
            "observed_at_utc": "2026-11-20T00:00:00Z",
            "evidence_digest_sha256": None,
            "omission_reason": None,
        }
    ]

    entry = _build(_document(record), mapping)["records"][0]

    assert entry["status"] == producer.STATUS_ELIGIBLE
    assert entry["business"] is not None
    assert all(assertion["evidenceDigest"] != "e" * 64 for assertion in entry["assertions"])


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


def test_link_existing_without_a_listing_becomes_a_blocked_record(tmp_path: Path) -> None:
    """One unprojectable record must not stop the manifest; it is stated as a blocked verdict."""
    mapping = _mapping(tmp_path)
    record = _record()
    record["subject_match"] = {
        "decision": "link_existing",
        "reason": "x",
        "subject_id": None,
        "listing_id": None,
    }
    healthy = _record(domain="other-studio.de", name="Other Studio")

    manifest = _build(_document(record, healthy), mapping)
    entry = next(
        item for item in manifest["records"] if item["registryId"] == record["registry_id"]
    )
    other = next(
        item for item in manifest["records"] if item["registryId"] == healthy["registry_id"]
    )

    assert entry["status"] == producer.STATUS_BLOCKED
    assert entry["statusReasons"] == [
        "manifest_record_invalid:link_existing identity requires listing_id"
    ]
    assert entry["business"] is None
    assert manifest["isolatedRecords"] == [
        {
            "registryId": entry["registryId"],
            "error": "link_existing identity requires listing_id",
        }
    ]
    # The healthy record still carries its facts: isolation is per record, not per manifest.
    assert other["status"] == producer.STATUS_ELIGIBLE
    assert other["business"]["name"] == "Other Studio"


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


# --- address points -----------------------------------------------------------------------

ADDRESS_EASTING = 392181.815
ADDRESS_NORTHING = 5818151.337


def _address_payload(*features: dict) -> dict:
    return {
        "type": "FeatureCollection",
        "timeStamp": "2026-11-21T05:55:25.985Z",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::25833"}},
        "features": list(features)
        or [
            {
                "geometry": {
                    "type": "Point",
                    "coordinates": [ADDRESS_EASTING, ADDRESS_NORTHING],
                },
                "properties": {
                    "adressid": "187448",
                    "hnr": 1,
                    "hnr_zusatz": None,
                    "str_name": "Example Str.",
                    "str_nr": "99999",
                    "plz": "10963",
                    "typ": "Adresse",
                    "qualitaet": "Qualitaet A",
                    "adr_datum": "2008-01-28",
                },
            }
        ],
    }


class _Resolver:
    """A lookup that answers from one fixed raw response, like the cached snapshot does."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.raw = json.dumps(payload, sort_keys=True).encode("utf-8")

    def __call__(self, street, street_number, postal_code, expected_address_record_id=None):
        from tools import wfs_address_points as wfs

        search = wfs.create_search(street, street_number, postal_code, expected_address_record_id)
        assert search is not None
        return wfs.resolve_address_point(
            self.payload, search, wfs.request_url(search), raw_bytes=self.raw
        )


def test_address_point_carries_its_own_response_provenance(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    payload = _address_payload()
    record = _record()

    entry = producer.build_manifest(
        _document(record),
        json.dumps(_document(record), sort_keys=True).encode("utf-8"),
        mapping,
        recorded_at_utc="2026-11-21T06:00:00Z",
        address_point_lookup=_Resolver(payload),
    )["records"][0]

    coordinate = entry["business"]["coordinates"]
    assert coordinate["addressRecordId"] == "187448"
    assert coordinate["latitude"] == pytest.approx(52.5028179)
    assert coordinate["sourceCrs"] == "EPSG:25833"
    assert coordinate["sourceX"] == pytest.approx(ADDRESS_EASTING)
    assertion = [
        item for item in entry["assertions"] if item["assertionId"] == coordinate["assertionId"]
    ]
    assert len(assertion) == 1
    assert assertion[0]["sourceKind"] == producer.SOURCE_KIND_LICENSED_DATASET
    assert assertion[0]["sourceExternalId"] == "187448"
    assert assertion[0]["observedAtUtc"] == "2026-11-21T05:55:25Z"
    assert assertion[0]["evidenceDigest"] == coordinate["sourceDigest"]
    assert assertion[0]["sourceReference"] == coordinate["sourceReference"]
    assert "adressen_berlin" in assertion[0]["sourceReference"]


def test_two_streets_with_the_same_house_number_resolve_to_different_points(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    payload = _address_payload(
        _address_payload()["features"][0],
        {
            "geometry": {"type": "Point", "coordinates": [391096.583, 5818583.769]},
            "properties": {
                "adressid": "246505",
                "hnr": 1,
                "hnr_zusatz": None,
                "str_name": "Other Str.",
                "str_nr": "99998",
                "plz": "10963",
                "typ": "Adresse",
                "qualitaet": "Qualitaet A",
                "adr_datum": "2008-01-28",
            },
        },
    )
    record = _record()
    record["address"] = dict(record["address"], street="Other Str.")
    resolver = _Resolver(payload)

    entry = producer.build_manifest(
        _document(record),
        json.dumps(_document(record), sort_keys=True).encode("utf-8"),
        mapping,
        recorded_at_utc="2026-11-21T06:00:00Z",
        address_point_lookup=resolver,
    )["records"][0]

    coordinate = entry["business"]["coordinates"]
    assert coordinate["addressRecordId"] == "246505"


def test_ambiguous_or_out_of_boundary_point_stays_unresolved_with_the_reason(
    tmp_path: Path,
) -> None:
    mapping = _mapping(tmp_path)
    duplicate = _address_payload()
    duplicate["features"] = [
        duplicate["features"][0],
        dict(
            duplicate["features"][0],
            properties=dict(duplicate["features"][0]["properties"], adressid="187449"),
        ),
    ]
    outside = _address_payload()
    outside["features"][0]["geometry"]["coordinates"] = [383000.0, 5806000.0]
    record = _record()
    registry_bytes = json.dumps(_document(record), sort_keys=True).encode("utf-8")

    for payload, reason in (
        (duplicate, "address_point_ambiguous_match"),
        (outside, "address_point_outside_berlin_boundary"),
    ):
        entry = producer.build_manifest(
            _document(record),
            registry_bytes,
            mapping,
            recorded_at_utc="2026-11-21T00:00:00Z",
            address_point_lookup=_Resolver(payload),
        )["records"][0]
        assert entry["business"]["coordinates"] is None
        assert entry["business"]["coordinateReason"] == reason
        assert entry["business"]["address"]["coordinate"] is None
        # Unresolvable geography must not carry an eligible verdict into Catalog.
        assert entry["status"] == producer.STATUS_REVIEW
        assert entry["statusReasons"] == [f"address_coordinate_unresolved:{reason}"]


def test_resolved_address_point_keeps_the_eligible_verdict(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    entry = producer.build_manifest(
        _document(record),
        json.dumps(_document(record), sort_keys=True).encode("utf-8"),
        mapping,
        recorded_at_utc="2026-11-21T06:00:00Z",
        address_point_lookup=_Resolver(_address_payload()),
    )["records"][0]

    assert entry["status"] == producer.STATUS_ELIGIBLE
    assert entry["statusReasons"] == []


def test_recording_time_cannot_precede_the_evidence_the_manifest_carries(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()

    with pytest.raises(producer.ManifestError) as refusal:
        producer.build_manifest(
            _document(record),
            json.dumps(_document(record), sort_keys=True).encode("utf-8"),
            mapping,
            recorded_at_utc="2026-11-21T05:55:24Z",
            address_point_lookup=_Resolver(_address_payload()),
        )

    assert "2026-11-21T05:55:25Z" in str(refusal.value)


def test_recording_time_defaults_after_the_address_point_it_observed(tmp_path: Path) -> None:
    """An address point is observed while the manifest is built, so an unset recording time is
    taken from the evidence, and it stays outside the manifest identity."""
    mapping = _mapping(tmp_path)
    record = _record()
    registry_bytes = json.dumps(_document(record), sort_keys=True).encode("utf-8")

    default = producer.build_manifest(
        _document(record),
        registry_bytes,
        mapping,
        recorded_at_utc=None,
        address_point_lookup=_Resolver(_address_payload()),
    )
    observed = max(
        assertion["observedAtUtc"]
        for entry in default["records"]
        for assertion in entry["assertions"]
    )
    explicit = producer.build_manifest(
        _document(record),
        registry_bytes,
        mapping,
        recorded_at_utc=observed,
        address_point_lookup=_Resolver(_address_payload()),
    )

    assert observed == "2026-11-21T05:55:25Z"
    assert default["recordedAtUtc"] >= observed
    assert default["manifestDigest"] == explicit["manifestDigest"]
    assert default["records"] == explicit["records"]


def test_offline_plan_without_address_lookup_keeps_the_registry_verdict(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    entry = producer.build_manifest(
        _document(record),
        json.dumps(_document(record), sort_keys=True).encode("utf-8"),
        mapping,
        recorded_at_utc="2026-11-21T00:00:00Z",
    )["records"][0]

    assert entry["status"] == producer.STATUS_ELIGIBLE
    assert entry["business"]["coordinateReason"] == "address_point_lookup_disabled"


def test_changed_address_response_changes_evidence_not_content(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    registry_bytes = json.dumps(_document(record), sort_keys=True).encode("utf-8")
    first = producer.build_manifest(
        _document(record),
        registry_bytes,
        mapping,
        recorded_at_utc="2026-11-21T06:00:00Z",
        address_point_lookup=_Resolver(_address_payload()),
    )["records"][0]
    changed = _address_payload()
    changed["timeStamp"] = "2026-11-21T06:00:00.000Z"
    second = producer.build_manifest(
        _document(record),
        registry_bytes,
        mapping,
        recorded_at_utc="2026-11-21T06:00:00Z",
        address_point_lookup=_Resolver(changed),
    )["records"][0]

    assert first["digests"]["content"] == second["digests"]["content"]
    assert first["digests"]["evidence"] != second["digests"]["evidence"]


def test_coordinate_states_its_derivation_and_approximate_semantics(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    resolver = _Resolver(_address_payload())
    resolution = resolver("Example Str.", "1", "10963")

    point = resolution.point
    assert point is not None
    approximate = point.__class__(
        **{
            **point.__dict__,
            "accuracy_meters": "20.0",
            "derivation": "range_endpoints_midpoint",
            "locality_semantics": "approximate_range",
            "address_record_ids": ("187448", "187449"),
        }
    )

    class _Approximate:
        def __call__(self, street, street_number, postal_code, expected=None):
            from tools import wfs_address_points as wfs

            return wfs.AddressResolution.resolved_at(approximate)

    entry = producer.build_manifest(
        _document(record),
        json.dumps(_document(record), sort_keys=True).encode("utf-8"),
        mapping,
        recorded_at_utc="2026-11-21T06:00:00Z",
        address_point_lookup=_Approximate(),
    )["records"][0]

    coordinate = entry["business"]["coordinates"]
    assert coordinate["derivation"] == "range_endpoints_midpoint"
    assert coordinate["localitySemantics"] == "approximate_range"
    assert coordinate["accuracyMeters"] == 20.0
    assert coordinate["addressRecordIds"] == ["187448", "187449"]


def test_legacy_district_key_is_not_imported_as_a_new_facet(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    record["address"] = dict(record["address"], district_key="berlin-mitte")

    entry = _build(_document(record), mapping)["records"][0]

    assert entry["status"] == producer.STATUS_REVIEW
    assert entry["statusReasons"] == ["district_key_not_canonical:berlin-mitte"]


def test_district_key_must_agree_with_the_bezirk_its_evidence_names(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _record()
    record["reviews"] = [
        {
            "review_id": "review-1",
            "kind": "fact",
            "actor_id": "operator",
            "sequence": 1,
            "decided_at_utc": "2026-11-19T19:00:00Z",
            "decision": "approved",
            "text_digest_sha256": "a" * 64,
            "source_evidence_digest_sha256": "b" * 64,
            "locale": None,
            "note": (
                "district_key verified against the official address register: "
                "https://gdi.berlin.de/ method=official_address_source bezirk=Pankow"
            ),
        }
    ]

    entry = _build(_document(record), mapping)["records"][0]

    assert entry["status"] == producer.STATUS_REVIEW
    assert entry["statusReasons"] == [
        "district_key_conflicts_with_address_evidence:friedrichshain-kreuzberg"
    ]


DESCRIPTION_LOCALES = ("de-DE", "en-GB", "ru-RU")


def _with_rich_material(
    record: dict,
    *,
    approved: bool = True,
    contact_state: str = "validated",
    texts: dict[str, str] | None = None,
) -> dict:
    """An approved description set and one validated contact over the record's own evidence."""
    evidence_digest = record["field_evidence"][0]["response_digest_sha256"]
    evidence_id = record["field_evidence"][0]["evidence_id"]
    texts = texts or {
        "de-DE": "Ein Aufnahmestudio.",
        "en-GB": "A recording studio.",
        "ru-RU": "Студия звукозаписи.",
    }
    record["descriptions"] = {
        locale: {
            "state": "approved" if approved else "draft",
            "text": texts[locale],
            "text_digest_sha256": hashlib.sha256(texts[locale].encode("utf-8")).hexdigest(),
            "source_evidence_digest_sha256": evidence_digest,
            "review_ref": "review-1" if approved else None,
        }
        for locale in DESCRIPTION_LOCALES
    }
    record["reviews"] = (
        [
            {
                "review_id": "review-1",
                "kind": "translation",
                "locale": locale,
                "decision": "approved",
                "text_digest_sha256": hashlib.sha256(texts[locale].encode("utf-8")).hexdigest(),
                "source_evidence_digest_sha256": evidence_digest,
                "sequence": 1,
            }
            for locale in DESCRIPTION_LOCALES
        ]
        if approved
        else []
    )
    record["contacts"] = [
        {
            "kind": "phone",
            "target": "+49 30 1234567",
            "label": None,
            "validation_state": contact_state,
            "evidence_refs": [evidence_id],
        }
    ]
    return record


def test_approved_descriptions_and_validated_contacts_travel_with_the_facts(
    tmp_path: Path,
) -> None:
    mapping = _mapping(tmp_path)
    entry = _build(_document(_with_rich_material(_record())), mapping)["records"][0]

    descriptions = entry["richFields"]["descriptions"]
    assert [item["locale"] for item in descriptions] == list(DESCRIPTION_LOCALES)
    assert descriptions[0]["text"] == "Ein Aufnahmestudio."
    # A bare phone number becomes the absolute URI Catalog can store, not an invented fact.
    assert [item["target"] for item in entry["richFields"]["contacts"]] == ["tel:+49301234567"]
    assertion_ids = {item["assertionId"] for item in entry["assertions"]}
    assert {item["assertionId"] for item in descriptions} <= assertion_ids
    assert entry["richReasons"] == []


def test_unapproved_description_is_absent_instead_of_deleting_stored_text(
    tmp_path: Path,
) -> None:
    mapping = _mapping(tmp_path)
    record = _with_rich_material(_record(), approved=False)
    entry = _build(_document(record), mapping)["records"][0]

    # Absence is stated as absence: the record says nothing about descriptions, so Catalog keeps
    # whatever it already stores. The reasons name the exact pending decision.
    assert "descriptions" not in (entry["richFields"] or {})
    assert "description_draft:de-DE" in entry["richReasons"]
    # The validated contact still travels on its own.
    assert [item["kind"] for item in entry["richFields"]["contacts"]] == ["phone"]


def test_unvalidated_contact_is_reported_instead_of_published(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    record = _with_rich_material(_record(), contact_state="draft")
    entry = _build(_document(record), mapping)["records"][0]

    assert "contacts" not in (entry["richFields"] or {})
    assert "contact_draft:phone" in entry["richReasons"]


def test_changed_description_changes_content_but_not_evidence(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path)
    original = _build(_document(_with_rich_material(_record())), mapping)["records"][0]
    changed = _build(
        _document(
            _with_rich_material(
                _record(),
                texts={
                    "de-DE": "Ein Aufnahmestudio in Berlin.",
                    "en-GB": "A recording studio in Berlin.",
                    "ru-RU": "Студия звукозаписи в Берлине.",
                },
            )
        ),
        mapping,
    )["records"][0]

    assert changed["digests"]["content"] != original["digests"]["content"]
    assert changed["digests"]["evidence"] == original["digests"]["evidence"]
    assert changed["digests"]["eligibility"] == original["digests"]["eligibility"]
