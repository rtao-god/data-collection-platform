"""Tests for the versioned Berlin studio research registry."""

from __future__ import annotations

import json
from pathlib import Path

from tools import studio_registry as sr

CAMPAIGN = Path("campaigns") / "berlin_recording_services" / "discovery"


def _evidence(
    *,
    field_path: str = "address.street",
    url: str = "https://example-studio.de/contact/",
    digest: str = "a" * 64,
    excerpt: str = "Example Studio, Example Str. 1, 10963 Berlin",
    usage_policy: str = "facts_only",
    policy_decision: dict | None = None,
) -> dict:
    return {
        "evidence_id": sr.derive_evidence_id(url, digest, field_path),
        "field_path": field_path,
        "source_kind": "official_site",
        "source_url": url,
        "external_id": None,
        "observed_at_utc": "2026-11-19T18:00:00Z",
        "response_digest_sha256": digest,
        "excerpt": excerpt,
        "usage_policy": usage_policy,
        "policy_decision": policy_decision,
        "attribution": None,
    }


def _description_draft(locale: str = "de-DE", text: str = "Faktenbasierte Beschreibung.") -> dict:
    return {
        "state": "draft",
        "text": text,
        "text_digest_sha256": sr.sha256_hex(text),
        "origin": "editorial",
        "review_state": "pending_human",
        "review_ref": None,
        "reason": None,
    }


def _review(
    *,
    review_id: str = "review-1",
    sequence: int = 1,
    decision: str = "approved",
    text: str = "Faktenbasierte Beschreibung.",
    locale: str = "de-DE",
    note: str | None = None,
) -> dict:
    return {
        "review_id": review_id,
        "kind": "translation",
        "actor_id": "human-reviewer",
        "sequence": sequence,
        "decided_at_utc": "2026-11-19T19:00:00Z",
        "decision": decision,
        "text_digest_sha256": sr.sha256_hex(text),
        "source_evidence_digest_sha256": sr.sha256_hex("source-evidence"),
        "locale": locale,
        "note": note,
    }


def _approved_description(locale: str, text: str, review_id: str = "review-1") -> dict:
    return {
        "state": "approved",
        "text": text,
        "text_digest_sha256": sr.sha256_hex(text),
        "origin": "editorial",
        "review_state": "approved",
        "review_ref": review_id,
        "reason": None,
        "source_evidence_digest_sha256": sr.sha256_hex("source-evidence"),
    }


def _record(**overrides) -> dict:
    address_evidence = _evidence()
    service_evidence = _evidence(
        field_path="service_claims",
        url="https://example-studio.de/",
        digest="b" * 64,
        excerpt="Example Studio offers recording and mixing sessions.",
    )
    record = {
        "registry_id": sr.derive_registry_id("official_domain", "example-studio.de"),
        "entity_kind": "place",
        "canonical_name": "Example Studio",
        "aliases": [],
        "identity_keys": [
            {"kind": "official_domain", "value": "example-studio.de", "authority": "authoritative"}
        ],
        "subject_match": {
            "decision": "create_new_pending_review",
            "reason": "no existing catalog subject matched",
            "subject_id": None,
            "listing_id": None,
        },
        "possible_duplicate_of": [],
        "official_domains": {"primary": "example-studio.de", "alternates": []},
        "source_original_name": "Example Studio",
        "address": {
            "street": "Example Str.",
            "street_number": "1",
            "postal_code": "10963",
            "city": "Berlin",
            "district_key": "friedrichshain-kreuzberg",
            "district_basis": "observed_source",
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
                "source_text": "recording and mixing sessions",
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


def _registry(*records: dict) -> dict:
    ordered = sorted(records or (_record(),), key=lambda item: item["registry_id"])
    return {
        "schema_revision": sr.SCHEMA_REVISION,
        "campaign_key": sr.CAMPAIGN_KEY,
        "records": ordered,
    }


def _codes(problems) -> set[str]:
    return {problem.code for problem in problems}


def test_committed_registry_is_valid() -> None:
    document = sr.read_json(sr.REGISTRY_PATH)
    assert sr.validate_registry(document) == []
    assert len(document["records"]) >= 30


def test_qualified_studio_is_ready_but_blocked_by_missing_source_decision() -> None:
    record = _record()
    qualified, reasons = sr.qualification(record)
    assert qualified, reasons
    blockers = sr.publication_blockers(record)
    assert "source_policy_decision_missing:example-studio.de" in blockers
    assert sr.rich_blockers(record)


def test_duplicate_registry_id_is_rejected() -> None:
    first = _record()
    second = _record(
        registry_id=first["registry_id"],
        canonical_name="Different Studio",
        identity_keys=[
            {
                "kind": "official_domain",
                "value": "different-studio.de",
                "authority": "authoritative",
            }
        ],
        official_domains={"primary": "different-studio.de", "alternates": []},
    )
    document = _registry(first, second)
    assert "registry_id_duplicate" in _codes(sr.validate_registry(document))


def test_shared_authoritative_identity_is_rejected() -> None:
    first = _record()
    second = _record(registry_id=str(sr.uuid.uuid4()), canonical_name="Same Domain Studio")
    problems = sr.validate_registry(_registry(first, second))
    assert "identity_key_conflict" in _codes(problems)


def test_non_berlin_subject_is_not_qualified() -> None:
    record = _record()
    record["address"] = {**record["address"], "city": "Potsdam", "postal_code": "14467"}
    qualified, reasons = sr.qualification(record)
    assert not qualified
    assert "outside_berlin" in reasons


def test_service_claim_without_defined_evidence_is_rejected() -> None:
    record = _record()
    record["service_claims"][0]["evidence_refs"] = ["ev-" + "0" * 16]
    problems = sr.validate_registry(_registry(record))
    assert "evidence_ref_unknown" in _codes(problems)


def test_unsafe_evidence_url_is_rejected() -> None:
    record = _record()
    record["field_evidence"][0]["source_url"] = "javascript:alert(1)"
    problems = sr.validate_registry(_registry(record))
    assert "evidence_url" in _codes(problems)


def test_oversized_or_markup_excerpt_is_rejected() -> None:
    record = _record()
    record["field_evidence"][0]["excerpt"] = "<p>" + "x" * 500 + "</p>"
    codes = _codes(sr.validate_registry(_registry(record)))
    assert {"evidence_excerpt_length", "evidence_excerpt_markup"} <= codes


def test_reviewed_description_requires_matching_approval() -> None:
    record = _record()
    record["reviews"] = []
    record["descriptions"] = {"de-DE": _approved_description("de-DE", "Text.")}
    problems = sr.validate_registry(_registry(record))
    assert "description_review_not_approving" in _codes(problems)


def test_later_rejection_overrides_earlier_approval() -> None:
    text = "Faktenbasierte Beschreibung."
    record = _record()
    record["reviews"] = [
        _review(review_id="review-1", sequence=1, decision="approved", text=text),
        _review(
            review_id="review-2",
            sequence=2,
            decision="rejected",
            text=text,
            note="Falsche Terminologie",
        ),
    ]
    record["descriptions"] = {"de-DE": _approved_description("de-DE", text)}
    problems = sr.validate_registry(_registry(record))
    assert "description_review_not_approving" in _codes(problems)


def test_changed_text_digest_fails_owner_gate() -> None:
    text = "Faktenbasierte Beschreibung."
    record = _record()
    record["reviews"] = [_review(text=text)]
    description = _approved_description("de-DE", text)
    description["text_digest_sha256"] = sr.sha256_hex("other text")
    record["descriptions"] = {"de-DE": description}
    problems = sr.validate_registry(_registry(record))
    assert {"description_digest_mismatch", "description_review_not_approving"} <= _codes(problems)


def test_rich_predicate_requires_three_approved_locales(tmp_path: Path) -> None:
    record = _record()
    texts = {
        "de-DE": "Erstes Studio in Berlin.",
        "en-GB": "First studio in Berlin.",
        "ru-RU": "Первая студия в Берлине.",
    }
    record["reviews"] = [
        _review(review_id=f"review-{locale}", sequence=index + 1, text=text, locale=locale)
        for index, (locale, text) in enumerate(texts.items())
    ]
    record["descriptions"] = {
        locale: _approved_description(locale, text, review_id=f"review-{locale}")
        for locale, text in texts.items()
    }
    record["contacts"] = [
        {
            "kind": "website",
            "target": "https://example-studio.de/",
            "label": None,
            "validation_state": "validated",
            "evidence_refs": [record["field_evidence"][1]["evidence_id"]],
        }
    ]
    record["photo_candidates"] = [
        {
            "candidate_id": "photo-1",
            "source_url": None,
            "rights_holder": None,
            "license_url": None,
            "license_version": None,
            "rights_state": "omitted_rights_unclear",
            "observed_at_utc": "2026-11-19T18:00:00Z",
            "evidence_digest_sha256": None,
            "omission_reason": "site terms forbid reuse without written consent",
        }
    ]
    record["field_evidence"][1] = {
        **record["field_evidence"][1],
        "policy_decision": {
            "decision": "approved",
            "reviewer": "source-policy-owner",
            "decided_at_utc": "2026-11-19T19:00:00Z",
            "scope": "official_site:example-studio.de facts",
            "policy_digest_sha256": "c" * 64,
        },
    }
    record["address"] = {**record["address"], "district_basis": "operator_reviewed"}
    record["restrictions"] = {"rejected": [], "missing": [], "conflicting": []}

    blockers = sr.rich_blockers(record)
    assert blockers == []
    assert sr.validate_registry(_registry(record)) == []


def test_generated_projection_is_byte_identical_and_detects_manual_edit(tmp_path: Path) -> None:
    registry_path = tmp_path / "studios.json"
    verified_path = tmp_path / "studios-verified.json"
    registry_path.write_text(sr.canonical_json(_registry()), encoding="utf-8")

    assert (
        sr.main(
            ["--registry", str(registry_path), "--verified", str(verified_path), "build-verified"]
        )
        == 0
    )
    first = verified_path.read_bytes()
    assert (
        sr.main(
            ["--registry", str(registry_path), "--verified", str(verified_path), "build-verified"]
        )
        == 0
    )
    assert verified_path.read_bytes() == first
    assert (
        sr.main(
            ["--registry", str(registry_path), "--verified", str(verified_path), "check-verified"]
        )
        == 0
    )

    verified_path.write_text(first.decode("utf-8") + " ", encoding="utf-8")
    assert (
        sr.main(
            ["--registry", str(registry_path), "--verified", str(verified_path), "check-verified"]
        )
        == 1
    )


def test_split_street_address_normalizes_glued_number_and_postal_country_prefix() -> None:
    # Official sites write the house number attached to the street (tonus arcus) and put the postal
    # country prefix right after it (uniSono: "Hammerstr. 17 D- 14167"); both reached the registry
    # as an unsplit street fragment, so the address had no house number at all.
    assert sr.split_street_address("Dunckerstr.21", "10437") == ("Dunckerstr.", "21")
    assert sr.split_street_address("Hammerstr. 17 D- 14167", "14167") == ("Hammerstr.", "17")
    assert sr.split_street_address("Hammerstr. 17 D", "14167") == ("Hammerstr.", "17")
    # Without the postal code there is no evidence that the letter belongs to the prefix, so it is
    # not dropped and the fragment stays unsplit instead of inventing a house number.
    assert sr.split_street_address("Hammerstr. 17 D") == ("Hammerstr. 17 D", None)
    assert sr.split_street_address("Revaler Straße 99", "10245") == ("Revaler Straße", "99")
    assert sr.split_street_address("Warschauer Str. 33a", "10243") == ("Warschauer Str.", "33a")


def test_import_leads_splits_a_street_number_glued_to_the_street_name(tmp_path: Path) -> None:
    registry_path = tmp_path / "studios.json"
    leads = [
        {
            "name": "Glued Address Studio",
            "domain": "glued-address-studio.de",
            "street": "Dunckerstr.21",
            "plz": "10437",
            "bezirk": "pankow",
            "bezirk_basis": "address_source_verified",
            "identityAction": "CreateNewPendingReview",
        }
    ]
    (tmp_path / "leads.json").write_text(json.dumps(leads), encoding="utf-8")
    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "import-leads",
                "--input",
                str(tmp_path / "leads.json"),
                "--batch",
                "tests",
            ]
        )
        == 0
    )
    address = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]["address"]
    assert address["street"] == "Dunckerstr."
    assert address["street_number"] == "21"
    assert address["postal_code"] == "10437"


def test_import_leads_is_deterministic_and_dry_run_does_not_write(tmp_path: Path) -> None:
    registry_path = tmp_path / "studios.json"
    leads = [
        {
            "name": "Example Studio",
            "domain": "example-studio.de",
            "street": "Example Str. 1",
            "plz": "10963",
            "bezirk": "friedrichshain-kreuzberg",
            "bezirk_basis": "postal_code",
            "identityAction": "CreateNewPendingReview",
        }
    ]
    (tmp_path / "leads.json").write_text(json.dumps(leads), encoding="utf-8")
    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "import-leads",
                "--input",
                str(tmp_path / "leads.json"),
                "--batch",
                "tests",
                "--dry-run",
            ]
        )
        == 0
    )
    assert not registry_path.exists()

    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "import-leads",
                "--input",
                str(tmp_path / "leads.json"),
                "--batch",
                "tests",
            ]
        )
        == 0
    )
    first = registry_path.read_bytes()
    first_document = json.loads(first)
    expected_id = sr.derive_registry_id("official_domain", "example-studio.de")
    assert [record["registry_id"] for record in first_document["records"]] == [expected_id]

    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "import-leads",
                "--input",
                str(tmp_path / "leads.json"),
                "--batch",
                "tests",
            ]
        )
        == 0
    )
    assert registry_path.read_bytes() == first
    assert len(json.loads(registry_path.read_bytes())["records"]) == 1


def test_enrich_refuses_self_approval_and_unknown_evidence(tmp_path: Path) -> None:
    registry_path = tmp_path / "studios.json"
    registry_path.write_text(sr.canonical_json(_registry()), encoding="utf-8")
    record = _record()
    patch = {
        "schema_revision": sr.PATCH_SCHEMA_REVISION,
        "patches": [
            {
                "registry_id": record["registry_id"],
                "field_path": "descriptions.de-DE",
                "value": _approved_description("de-DE", "Selbst freigegeben."),
                "reviewer": "same-agent",
            },
            {
                "registry_id": record["registry_id"],
                "field_path": "contacts",
                "value": [],
                "evidence_refs": ["ev-" + "1" * 16],
                "reviewer": "researcher",
            },
        ],
    }
    patch_path = tmp_path / "patch.json"
    patch_path.write_text(json.dumps(patch), encoding="utf-8")

    assert (
        sr.main(
            ["--registry", str(registry_path), "enrich", "--patch", str(patch_path), "--dry-run"]
        )
        == 1
    )
    document = json.loads(registry_path.read_text(encoding="utf-8"))
    assert document == _registry()


def test_merge_keeps_human_decision_and_records_disagreement() -> None:
    existing = _record(
        descriptions={"de-DE": _description_draft()},
        reviews=[_review()],
    )
    incoming = _record(
        descriptions={"de-DE": _description_draft(text="Andere Beschreibung.")},
    )
    merged = sr.merge_lead_into_record(existing, incoming)
    assert merged["descriptions"]["de-DE"]["text"] == "Faktenbasierte Beschreibung."
    assert any(
        entry["code"] == "description_digest_changed"
        for entry in merged["restrictions"]["conflicting"]
    )


def test_catalog_category_missing_is_blocking_for_unmapped_service() -> None:
    lead = {
        "name": "Mix Only",
        "domain": "mix-only.de",
        "street": "Example Str. 2",
        "plz": "10963",
        "bezirk": "friedrichshain-kreuzberg",
        "bezirk_basis": "postal_code",
        "identityAction": "CreateNewPendingReview",
        "evidence": [
            {
                "role": "address",
                "url": "https://mix-only.de/contact/",
                "sha256": "d" * 64,
                "observed_at_utc": "2026-11-19T18:00:00Z",
                "excerpt": "Mix Only, Example Str. 2, 10963 Berlin",
            },
            {
                "role": "service",
                "url": "https://mix-only.de/",
                "sha256": "e" * 64,
                "observed_at_utc": "2026-11-19T18:00:00Z",
                "keyword": "Produktion",
                "excerpt": "Produktion und Aufnahme in unserem Studio.",
            },
        ],
    }
    record = sr.lead_to_record(lead, "tests")
    assert record is not None
    assert record["service_claims"] == []
    assert any(
        entry["code"] == "catalog_category_missing" and entry["severity"] == "blocking"
        for entry in record["restrictions"]["conflicting"]
    )
    qualified, reasons = sr.qualification(record)
    assert not qualified
    assert "restriction:catalog_category_missing" in reasons


def test_mixing_service_maps_to_the_place_mixing_studio_category() -> None:
    lead = {
        "name": "Mix Only",
        "domain": "mix-only.de",
        "street": "Example Str. 2",
        "plz": "10963",
        "bezirk": "friedrichshain-kreuzberg",
        "bezirk_basis": "postal_code",
        "identityAction": "CreateNewPendingReview",
        "evidence": [
            {
                "role": "address",
                "url": "https://mix-only.de/contact/",
                "sha256": "d" * 64,
                "observed_at_utc": "2026-11-19T18:00:00Z",
                "excerpt": "Mix Only, Example Str. 2, 10963 Berlin",
            },
            {
                "role": "service",
                "url": "https://mix-only.de/",
                "sha256": "e" * 64,
                "observed_at_utc": "2026-11-19T18:00:00Z",
                "keyword": "Mixing",
                "excerpt": "Mixing sessions in our control room.",
            },
        ],
    }
    record = sr.lead_to_record(lead, "tests")
    assert record is not None
    assert [claim["catalog_category_key"] for claim in record["service_claims"]] == [
        "mixing-studio"
    ]
    assert not any(
        entry["code"] == "catalog_category_missing"
        for entry in record["restrictions"]["conflicting"]
    )


def test_merge_drops_a_stale_taxonomy_blocker_once_the_service_is_classified() -> None:
    existing = _record(
        service_claims=[],
        restrictions={
            "rejected": [],
            "missing": [],
            "conflicting": [
                {
                    "code": "catalog_category_missing",
                    "severity": "blocking",
                    "detail": "the pinned taxonomy has no place category",
                }
            ],
        },
    )
    incoming = _record()
    assert incoming["service_claims"]
    merged = sr.merge_lead_into_record(existing, incoming)
    assert merged["service_claims"]
    assert not any(
        entry["code"] == "catalog_category_missing"
        for entry in merged["restrictions"]["conflicting"]
    )


def _shared_page_lead(domain: str) -> dict:
    return {
        "canonical_name": "Shared Page Studio",
        "domain": domain,
        "street": "Example Str. 3",
        "plz": "10963",
        "bezirk": "friedrichshain-kreuzberg",
        "bezirk_basis": "postal_code",
        "categories": ["recording-studio"],
        "service": "Aufnahme im Studio.",
        "evidence": [
            {
                "role": "address",
                "url": "https://directory.example/studios/",
                "sha256": "f" * 64,
                "observed_at_utc": "2026-11-20T20:00:00Z",
                "excerpt": "Shared Page Studio, Example Str. 3, 10963 Berlin",
            },
            {
                "role": "service",
                "url": "https://directory.example/studios/",
                "sha256": "f" * 64,
                "observed_at_utc": "2026-11-20T20:00:00Z",
                "excerpt": "Aufnahme, Mixing und Mastering im Studio.",
            },
        ],
    }


def test_two_records_citing_one_page_get_distinct_evidence_ids() -> None:
    """Registry validity requires document-wide evidence ids, so the owner is part of the id."""
    first = sr.lead_to_record(_shared_page_lead("first-studio.de"), "tests")
    second = sr.lead_to_record(_shared_page_lead("second-studio.de"), "tests")
    assert first is not None and second is not None
    first_ids = {entry["evidence_id"] for entry in first["field_evidence"]}
    second_ids = {entry["evidence_id"] for entry in second["field_evidence"]}
    assert first_ids and second_ids
    assert not first_ids & second_ids
    document = {
        "schema_revision": sr.SCHEMA_REVISION,
        "campaign_key": sr.CAMPAIGN_KEY,
        "records": [first, second],
    }
    assert sr.validate_registry(document) == []


def test_www_and_bare_host_share_one_identity_and_one_record(tmp_path: Path) -> None:
    """A www-prefixed spelling must merge into the bare-host record, not create a second one."""
    registry = tmp_path / "studios.json"
    plain = _shared_page_lead("shared-host.de")
    prefixed = _shared_page_lead("www.shared-host.de")
    registry.write_text(
        json.dumps(
            {"schema_revision": sr.SCHEMA_REVISION, "campaign_key": sr.CAMPAIGN_KEY, "records": []}
        ),
        encoding="utf-8",
    )
    sr.import_leads(registry, [plain], "tests", dry_run=False)
    sr.import_leads(registry, [prefixed], "tests", dry_run=False)
    document = json.loads(registry.read_text(encoding="utf-8"))
    assert len(document["records"]) == 1
    stored = document["records"][0]["official_domains"]["primary"]
    assert stored == plain["domain"]


def _registry_with_postal_code_district(tmp_path: Path) -> Path:
    registry = tmp_path / "studios.json"
    registry.write_text(
        json.dumps(
            {"schema_revision": sr.SCHEMA_REVISION, "campaign_key": sr.CAMPAIGN_KEY, "records": []}
        ),
        encoding="utf-8",
    )
    sr.import_leads(registry, [_shared_page_lead("shared-host.de")], "tests", dry_run=False)
    return registry


def _district_evidence_file(tmp_path: Path, registry_id: str, bezirk: str = "Friedrichshain-Kreuzberg") -> Path:
    path = tmp_path / "district-evidence.json"
    path.write_text(
        json.dumps(
            {
                "records": {
                    registry_id: {
                        "bezirk": bezirk,
                        "source_url": "https://www.berlin.de/ba-friedrichshain-kreuzberg/",
                        "sha256": "c" * 64,
                        "observed_at_utc": "2026-11-20T20:00:00Z",
                        "method": "official_boundary_source",
                        "boundary_contract": "campaign-geography-provenance-v1",
                        "boundary_digest": "sha256:" + "d" * 64,
                        "excerpt": "Example Str. 3, 10963 Berlin, Bezirk Friedrichshain-Kreuzberg",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_confirm_district_requires_evidence_and_refuses_a_bare_flag(tmp_path: Path) -> None:
    registry = _registry_with_postal_code_district(tmp_path)
    result = sr.decide(
        registry,
        policy_path=None,
        reviewer="tester",
        decided_at_utc="2026-11-20T20:10:00Z",
        confirm_district=True,
        confirm_market=False,
        identity_decision=None,
        registry_ids=None,
        dry_run=False,
    )
    assert result == 2
    record = json.loads(registry.read_text(encoding="utf-8"))["records"][0]
    assert record["address"]["district_basis"] == "postal_code"
    assert any(
        entry["code"] == "district_derivation_unconfirmed"
        for entry in record["restrictions"].get("conflicting", [])
    )
    assert not sr.publication_blockers(record) == []


def test_district_evidence_verifies_the_bezirk_and_records_its_provenance(tmp_path: Path) -> None:
    registry = _registry_with_postal_code_district(tmp_path)
    record = json.loads(registry.read_text(encoding="utf-8"))["records"][0]
    evidence = _district_evidence_file(tmp_path, record["registry_id"])
    result = sr.decide(
        registry,
        policy_path=None,
        reviewer="tester",
        decided_at_utc="2026-11-20T20:10:00Z",
        confirm_district=True,
        confirm_market=False,
        identity_decision=None,
        registry_ids=None,
        dry_run=False,
        district_evidence_path=evidence,
    )
    assert result == 0
    stored = json.loads(registry.read_text(encoding="utf-8"))["records"][0]
    assert stored["address"]["district_basis"] == "address_source_verified"
    review = [entry for entry in stored["reviews"] if entry["kind"] == "fact"][-1]
    assert "official_boundary_source" in review["note"]
    assert "boundary=campaign-geography-provenance-v1@" in review["note"]
    assert review["source_evidence_digest_sha256"] == "c" * 64
    assert any(
        entry["field_path"] == "address.district_key" and entry["source_url"].endswith("/ba-friedrichshain-kreuzberg/")
        for entry in stored["field_evidence"]
    )
    assert not any(
        entry["code"] == "district_derivation_unconfirmed"
        for entry in stored["restrictions"].get("conflicting", [])
    )
    assert "district_derivation_not_confirmed" not in sr.publication_blockers(stored)


def test_district_evidence_for_another_bezirk_is_refused(tmp_path: Path) -> None:
    registry = _registry_with_postal_code_district(tmp_path)
    record = json.loads(registry.read_text(encoding="utf-8"))["records"][0]
    evidence = _district_evidence_file(tmp_path, record["registry_id"], bezirk="Pankow")
    result = sr.decide(
        registry,
        policy_path=None,
        reviewer="tester",
        decided_at_utc="2026-11-20T20:10:00Z",
        confirm_district=True,
        confirm_market=False,
        identity_decision=None,
        registry_ids=None,
        dry_run=False,
        district_evidence_path=evidence,
    )
    assert result == 0
    stored = json.loads(registry.read_text(encoding="utf-8"))["records"][0]
    assert stored["address"]["district_basis"] == "postal_code"
    assert any(
        entry["code"] == "district_derivation_unconfirmed"
        for entry in stored["restrictions"].get("conflicting", [])
    )


def test_official_address_record_certifies_a_bezirk_without_a_boundary_digest(tmp_path: Path) -> None:
    """An official address record names the Bezirk itself, so no boundary geometry is claimed."""
    registry = _registry_with_postal_code_district(tmp_path)
    record = json.loads(registry.read_text(encoding="utf-8"))["records"][0]
    path = tmp_path / "address-evidence.json"
    path.write_text(
        json.dumps(
            {
                "records": {
                    record["registry_id"]: {
                        "bezirk": "friedrichshain-kreuzberg",
                        "source_url": "https://gdi.berlin.de/services/wfs/adressen_berlin?CQL_FILTER=...",
                        "sha256": "b" * 64,
                        "observed_at_utc": "2026-11-20T23:30:00Z",
                        "method": "official_address_source",
                        "dataset_identifier": "634ab8ba-7694-333b-b95d-474d2aed0f7b",
                        "dataset_version": "Adressen Berlin WFS revision 2026-06-30",
                        "address_record_id": "203298",
                        "excerpt": "Kantstrasse 63A, 10627, Bezirk Charlottenburg-Wilmersdorf",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    result = sr.decide(
        registry,
        policy_path=None,
        reviewer="tester",
        decided_at_utc="2026-11-20T23:35:00Z",
        confirm_district=True,
        confirm_market=False,
        identity_decision=None,
        registry_ids=None,
        dry_run=False,
        district_evidence_path=path,
    )
    assert result == 0
    stored = json.loads(registry.read_text(encoding="utf-8"))["records"][0]
    assert stored["address"]["district_basis"] == "address_source_verified"
    review = [entry for entry in stored["reviews"] if entry["kind"] == "fact"][-1]
    assert "dataset=634ab8ba" in review["note"]
    assert "address_record=203298" in review["note"]


def test_address_evidence_needs_the_official_record_identifier(tmp_path: Path) -> None:
    registry = _registry_with_postal_code_district(tmp_path)
    record = json.loads(registry.read_text(encoding="utf-8"))["records"][0]
    path = tmp_path / "address-evidence.json"
    path.write_text(
        json.dumps(
            {
                "records": {
                    record["registry_id"]: {
                        "bezirk": "friedrichshain-kreuzberg",
                        "source_url": "https://example.org/address",
                        "sha256": "b" * 64,
                        "observed_at_utc": "2026-11-20T23:30:00Z",
                        "method": "official_address_source",
                        "dataset_identifier": "634ab8ba-7694-333b-b95d-474d2aed0f7b",
                        "excerpt": "Example",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    result = sr.decide(
        registry,
        policy_path=None,
        reviewer="tester",
        decided_at_utc="2026-11-20T23:35:00Z",
        confirm_district=True,
        confirm_market=False,
        identity_decision=None,
        registry_ids=None,
        dry_run=False,
        district_evidence_path=path,
    )
    assert result == 0
    stored = json.loads(registry.read_text(encoding="utf-8"))["records"][0]
    assert stored["address"]["district_basis"] == "postal_code"


def _contact_patch(record: dict, *, target: str = "booking@example-studio.de") -> dict:
    return {
        "schema_revision": sr.PATCH_SCHEMA_REVISION,
        "patches": [
            {
                "registry_id": record["registry_id"],
                "field_path": "contacts",
                "reviewer": "researcher",
                "evidence_refs": [record["field_evidence"][0]["evidence_id"]],
                "value": [
                    {
                        "kind": "email",
                        "label": None,
                        "target": target,
                        "validation_state": "unvalidated",
                        "evidence_refs": [record["field_evidence"][0]["evidence_id"]],
                    }
                ],
            }
        ],
    }


def test_enrich_replay_writes_nothing_and_keeps_a_single_revision_entry(tmp_path: Path) -> None:
    registry_path = tmp_path / "studios.json"
    registry_path.write_text(sr.canonical_json(_registry()), encoding="utf-8")
    patch_path = tmp_path / "patch.json"
    patch_path.write_text(json.dumps(_contact_patch(_record())), encoding="utf-8")

    assert sr.main(["--registry", str(registry_path), "enrich", "--patch", str(patch_path)]) == 0
    first = registry_path.read_bytes()
    assert sr.main(["--registry", str(registry_path), "enrich", "--patch", str(patch_path)]) == 0

    assert registry_path.read_bytes() == first
    stored = json.loads(first)["records"][0]
    assert len(stored["collector"]["revisions"]) == 1
    assert stored["contacts"][0]["validation_state"] == "unvalidated"


def test_enrich_refuses_to_replace_a_contact_the_record_already_carries(tmp_path: Path) -> None:
    existing = [
        {
            "kind": "email",
            "label": "booking",
            "target": "verified@example-studio.de",
            "validation_state": "validated",
            "evidence_refs": ["ev-" + "a" * 16],
        }
    ]
    record = _record(contacts=existing)
    registry_path = tmp_path / "studios.json"
    registry_path.write_text(sr.canonical_json(_registry(record)), encoding="utf-8")
    patch_path = tmp_path / "patch.json"
    patch_path.write_text(json.dumps(_contact_patch(record)), encoding="utf-8")

    assert (
        sr.main(["--registry", str(registry_path), "enrich", "--patch", str(patch_path), "--dry-run"])
        == 1
    )
    assert json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]["contacts"] == existing


def _capture_manifest(record: dict, *, url: str = "https://example-studio.de/studios/") -> dict:
    return {
        "schemaRevision": "studio-evidence-capture-v1",
        "sourceKey": "official_site",
        "entries": [
            {
                "registryId": record["registry_id"],
                "url": url,
                "state": "captured",
                "statusCode": 200,
                "contentType": "text/html; charset=utf-8",
                "responseDigestSha256": "d" * 64,
                "observedAtUtc": "2026-11-21T09:00:00Z",
                "encodedSizeBytes": 2048,
                "decodedSizeBytes": 2048,
                "fieldPaths": ["descriptions.de-DE", "source.page"],
                "excerpt": "Studio Example: Recording, Mixing und Mastering im Studio.",
            },
            {
                "registryId": "99999999-9999-5999-8999-999999999999",
                "url": "https://unknown.example/",
                "state": "captured",
                "responseDigestSha256": "e" * 64,
                "observedAtUtc": "2026-11-21T09:00:02Z",
                "fieldPaths": ["source.page"],
                "excerpt": "irrelevant",
            },
        ],
    }


def test_record_evidence_appends_one_artifact_per_declared_field_and_refuses_unknown_records(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "studios.json"
    registry_path.write_text(sr.canonical_json(_registry()), encoding="utf-8")
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(_capture_manifest(_record())), encoding="utf-8")

    assert sr.main(["--registry", str(registry_path), "record-evidence", "--capture", str(capture_path)]) == 0

    stored = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]
    added = [item for item in stored["field_evidence"] if item["response_digest_sha256"] == "d" * 64]
    assert sorted(item["field_path"] for item in added) == ["descriptions.de-DE", "source.page"]
    assert all(item["policy_decision"] is None for item in added)
    assert not [item for item in stored["field_evidence"] if item["response_digest_sha256"] == "e" * 64]

    before = registry_path.read_bytes()
    assert sr.main(["--registry", str(registry_path), "record-evidence", "--capture", str(capture_path)]) == 0
    assert registry_path.read_bytes() == before


def test_policy_digest_reuse_needs_a_digest_this_registry_already_approved(tmp_path: Path) -> None:
    recorded = _evidence(
        url="https://example-studio.de/",
        digest="f" * 64,
        policy_decision={
            "decision": "approved",
            "reviewer": "owner",
            "decided_at_utc": "2026-11-20T00:00:00Z",
            "scope": "official_site facts_only",
            "policy_digest_sha256": "f" * 64,
        }
    )
    pending = _evidence(
        field_path="descriptions.de-DE",
        url="https://example-studio.de/studios/",
        digest="d" * 64,
    )
    base = _record()
    record = _record(field_evidence=[*base["field_evidence"], recorded, pending])
    registry_path = tmp_path / "studios.json"
    registry_path.write_text(sr.canonical_json(_registry(record)), encoding="utf-8")

    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "decide",
                "--reviewer",
                "owner",
                "--decided-at-utc",
                "2026-11-22T00:00:00Z",
                "--policy-digest",
                "a" * 64,
            ]
        )
        == 2
    )
    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "decide",
                "--reviewer",
                "owner",
                "--decided-at-utc",
                "2026-11-22T00:00:00Z",
                "--policy-digest",
                "f" * 64,
            ]
        )
        == 0
    )
    stored = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]
    decided = [item for item in stored["field_evidence"] if item["response_digest_sha256"] == "d" * 64]
    assert decided[0]["policy_decision"]["policy_digest_sha256"] == "f" * 64


def test_media_omission_is_recorded_once_and_never_overwrites_a_photo_candidate(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "studios.json"
    registry_path.write_text(sr.canonical_json(_registry()), encoding="utf-8")
    reason = "No signed image-reuse decision exists; the policy prohibits source_photo."

    arguments = [
        "--registry",
        str(registry_path),
        "decide",
        "--reviewer",
        "owner",
        "--decided-at-utc",
        "2026-11-22T00:00:00Z",
        "--omit-media",
        reason,
    ]
    assert sr.main(arguments) == 0
    assert sr.main(arguments) == 0

    stored = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]
    assert len(stored["photo_candidates"]) == 1
    assert stored["photo_candidates"][0]["rights_state"] == "omitted_rights_unclear"
    assert stored["photo_candidates"][0]["omission_reason"] == reason
    assert "media_state_not_recorded" not in sr.rich_blockers(stored)

    stored["photo_candidates"] = [
        {
            "candidate_id": "photo-1",
            "source_url": "https://example-studio.de/photo.jpg",
            "rights_holder": None,
            "license_url": None,
            "license_version": None,
            "rights_state": "candidate_unreviewed",
            "observed_at_utc": "2026-11-21T09:00:00Z",
            "evidence_digest_sha256": None,
            "omission_reason": None,
        }
    ]
    registry_path.write_text(sr.canonical_json(_registry(stored)), encoding="utf-8")
    assert sr.main(arguments) == 0
    kept = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]["photo_candidates"]
    assert [item["candidate_id"] for item in kept] == ["photo-1"]


def _drafted_record() -> dict:
    """A record whose description and contact exist but are still unapproved and unvalidated."""
    base = _record()
    text = "Faktenbasierte Beschreibung."
    record = _record(
        descriptions={
            "de-DE": {
                "state": "draft",
                "text": text,
                "text_digest_sha256": sr.sha256_hex(text),
                "origin": "editorial",
                "review_state": "pending_human",
                "review_ref": None,
                "reason": "draft",
                "source_evidence_digest_sha256": base["field_evidence"][1]["response_digest_sha256"],
            }
        },
        contacts=[
            {
                "kind": "email",
                "label": None,
                "target": "booking@example-studio.de",
                "validation_state": "unvalidated",
                "evidence_refs": [base["field_evidence"][1]["evidence_id"]],
            }
        ],
    )
    return record


def test_review_approves_the_exact_text_and_then_clears_the_draft_blocker(tmp_path: Path) -> None:
    record = _drafted_record()
    registry_path = tmp_path / "studios.json"
    registry_path.write_text(sr.canonical_json(_registry(record)), encoding="utf-8")
    digest = record["descriptions"]["de-DE"]["text_digest_sha256"]
    decisions = tmp_path / "descriptions.json"
    decisions.write_text(
        json.dumps(
            [
                {
                    "registry_id": record["registry_id"],
                    "locale": "de-DE",
                    "decision": "approved",
                    "text_digest_sha256": digest,
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
                "editor-1",
                "--decided-at-utc",
                "2026-11-22T10:00:00Z",
                "--descriptions",
                str(decisions),
            ]
        )
        == 0
    )

    stored = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]
    assert stored["descriptions"]["de-DE"]["state"] == "approved"
    assert "description_draft:de-DE" not in sr.rich_blockers(stored)


def test_review_refuses_a_research_identity_and_a_stale_text_digest(tmp_path: Path) -> None:
    record = _drafted_record()
    registry_path = tmp_path / "studios.json"
    registry_path.write_text(sr.canonical_json(_registry(record)), encoding="utf-8")
    decisions = tmp_path / "descriptions.json"
    decisions.write_text(
        json.dumps(
            [
                {
                    "registry_id": record["registry_id"],
                    "locale": "de-DE",
                    "decision": "approved",
                    "text_digest_sha256": sr.sha256_hex("another text"),
                }
            ]
        ),
        encoding="utf-8",
    )
    before = registry_path.read_bytes()

    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "review",
                "--reviewer",
                "research-capture-not-approved",
                "--decided-at-utc",
                "2026-11-22T10:00:00Z",
                "--descriptions",
                str(decisions),
            ]
        )
        == 2
    )
    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "review",
                "--reviewer",
                "editor-1",
                "--decided-at-utc",
                "2026-11-22T10:00:00Z",
                "--descriptions",
                str(decisions),
            ]
        )
        == 1
    )
    stored = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]
    assert stored["descriptions"]["de-DE"]["state"] == "draft"
    assert stored["reviews"] == []
    assert registry_path.read_bytes() == before


def test_review_validates_the_reviewed_contact_only(tmp_path: Path) -> None:
    record = _drafted_record()
    registry_path = tmp_path / "studios.json"
    registry_path.write_text(sr.canonical_json(_registry(record)), encoding="utf-8")
    decisions = tmp_path / "contacts.json"
    decisions.write_text(
        json.dumps(
            [
                {
                    "registry_id": record["registry_id"],
                    "kind": "email",
                    "target": "booking@example-studio.de",
                    "validation_state": "validated",
                },
                {
                    "registry_id": record["registry_id"],
                    "kind": "phone",
                    "target": "+49 30 000000",
                    "validation_state": "validated",
                },
            ]
        ),
        encoding="utf-8",
    )
    before = registry_path.read_bytes()

    # one unknown target refuses the whole batch: a reviewer must not get a half-applied review file
    assert (
        sr.main(
            [
                "--registry",
                str(registry_path),
                "review",
                "--reviewer",
                "editor-1",
                "--decided-at-utc",
                "2026-11-22T10:00:00Z",
                "--contacts",
                str(decisions),
            ]
        )
        == 1
    )
    assert registry_path.read_bytes() == before
    stored = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]
    assert stored["contacts"][0]["validation_state"] == "unvalidated"

    decisions.write_text(
        json.dumps(
            [
                {
                    "registry_id": record["registry_id"],
                    "kind": "email",
                    "target": "booking@example-studio.de",
                    "validation_state": "validated",
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
                "editor-1",
                "--decided-at-utc",
                "2026-11-22T10:00:00Z",
                "--contacts",
                str(decisions),
            ]
        )
        == 0
    )

    stored = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]
    assert stored["contacts"][0]["validation_state"] == "validated"
    assert [review["kind"] for review in stored["reviews"]] == ["fact"]
    assert "validated_contact_missing" not in sr.rich_blockers(stored)


def test_a_contact_decision_cannot_cite_a_digest_of_some_other_artifact(tmp_path: Path) -> None:
    record = _drafted_record()
    citations = record["field_evidence"]
    contact_evidenced = citations[1]["response_digest_sha256"]
    unrelated = next(
        item["response_digest_sha256"]
        for item in citations
        if item["response_digest_sha256"] != contact_evidenced
    )
    registry_path = tmp_path / "studios.json"
    registry_path.write_text(sr.canonical_json(_registry(record)), encoding="utf-8")
    before = registry_path.read_bytes()
    decisions = tmp_path / "contacts.json"
    decisions.write_text(
        json.dumps(
            [
                {
                    "registry_id": record["registry_id"],
                    "kind": "email",
                    "target": "booking@example-studio.de",
                    "validation_state": "validated",
                    "source_evidence_digest_sha256": unrelated,
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
                "editor-1",
                "--decided-at-utc",
                "2026-11-22T10:00:00Z",
                "--contacts",
                str(decisions),
            ]
        )
        == 1
    )

    assert json.loads(registry_path.read_text(encoding="utf-8")) == json.loads(before)
    stored = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]
    assert stored["contacts"][0]["validation_state"] == "unvalidated"
    assert stored["reviews"] == record["reviews"]


def test_a_contact_decision_uses_the_digest_of_the_artifact_it_cites(tmp_path: Path) -> None:
    record = _drafted_record()
    evidenced = record["field_evidence"][1]["response_digest_sha256"]
    registry_path = tmp_path / "studios.json"
    registry_path.write_text(sr.canonical_json(_registry(record)), encoding="utf-8")
    decisions = tmp_path / "contacts.json"
    decisions.write_text(
        json.dumps(
            [
                {
                    "registry_id": record["registry_id"],
                    "kind": "email",
                    "target": "booking@example-studio.de",
                    "validation_state": "validated",
                    "source_evidence_digest_sha256": evidenced,
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
                "editor-1",
                "--decided-at-utc",
                "2026-11-22T10:00:00Z",
                "--contacts",
                str(decisions),
            ]
        )
        == 0
    )

    stored = json.loads(registry_path.read_text(encoding="utf-8"))["records"][0]
    assert stored["contacts"][0]["validation_state"] == "validated"
    assert stored["reviews"][-1]["source_evidence_digest_sha256"] == evidenced
