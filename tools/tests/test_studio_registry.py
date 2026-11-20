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
                        "method": "official_address_source",
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
    assert "official_address_source" in review["note"]
    assert "sha256:" + "d" * 64 in review["note"]
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
