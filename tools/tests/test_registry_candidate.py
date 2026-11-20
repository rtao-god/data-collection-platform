"""The registry -> candidate revision translation is deterministic and refuses incomplete records."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry_candidate as rc  # noqa: E402

EVIDENCE = {
    "bezirk": "Friedrichshain-Kreuzberg",
    "source_url": "https://gdi.berlin.de/services/wfs/adressen_berlin?CQL_FILTER=...",
    "sha256": "a" * 64,
    "observed_at_utc": "2026-11-20T23:45:00Z",
    "method": "official_address_source",
    "dataset_identifier": "634ab8ba-7694-333b-b95d-474d2aed0f7b",
    "dataset_version": "Adressen Berlin WFS, revision 2026-06-30",
    "address_record_id": "203298",
    "excerpt": "Prinzessinnenstrasse 16 10969 Berlin, Bezirk Friedrichshain-Kreuzberg",
}

RECORD = {
    "registry_id": "027a1f22-6b8e-5dc7-bbaf-f73c30fddabf",
    "canonical_name": "TRIXX Studios",
    "aliases": [],
    "official_domains": {"primary": "trixx-studios.de", "alternates": []},
    "address": {
        "street": "Prinzessinnenstrasse",
        "street_number": "16",
        "postal_code": "10969",
        "city": "Berlin",
        "country_code": "DE",
        "district_key": "friedrichshain-kreuzberg",
        "district_basis": "address_source_verified",
        "coordinate": None,
    },
    "service_claims": [{"catalog_category_key": "recording-studio"}, {"catalog_category_key": "mixing-studio"}],
    "field_evidence": [
        {"evidence_id": "ev-1", "field_path": "address.street", "source_url": "https://trixx-studios.de/"},
        {"evidence_id": "ev-2", "field_path": "service_claims", "source_url": "https://trixx-studios.de/"},
    ],
    "collector": {"import_batch": "baseline-30-dossier"},
}


def build(**overrides):
    record = copy.deepcopy(RECORD)
    record.update(overrides)
    return rc.build_candidate_revision(
        record,
        EVIDENCE,
        recorded_at_utc="2026-11-21T00:30:00Z",
        correlation_id="e2e-one",
    )


def test_revision_carries_typed_facts_and_official_district_provenance() -> None:
    revision = build()
    assert revision.entity_kind == "place"
    assert revision.resolution_state == "resolved"
    payload = revision.normalized_payload
    assert payload["official_domain"] == "trixx-studios.de"
    assert payload["categories"] == ["recording-studio", "mixing-studio"]
    assert payload["address"]["district_key"] == "friedrichshain_kreuzberg"
    assert payload["district_provenance"]["address_record_id"] == "203298"
    assert payload["district_provenance"]["dataset_identifier"].startswith("634ab8ba")
    assert len(revision.evidence) == 2
    assert [item.position for item in revision.evidence] == [0, 1]


def test_candidate_identity_and_digests_are_stable_across_runs() -> None:
    first = build()
    second = build()
    assert first.candidate_id == second.candidate_id
    assert first.cluster_id == second.cluster_id
    assert first.snapshot_digest == second.snapshot_digest
    assert first.source_lineage_digest == second.source_lineage_digest


def test_a_new_category_changes_the_snapshot_but_not_the_identity() -> None:
    first = build()
    changed = build(service_claims=[{"catalog_category_key": "recording-studio"}])
    assert changed.candidate_id == first.candidate_id
    assert changed.snapshot_digest != first.snapshot_digest


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"address": {**RECORD["address"], "district_basis": "postal_code"}}, "official address record"),
        ({"service_claims": []}, "catalog category"),
        ({"official_domains": {"primary": "", "alternates": []}}, "official domain"),
        ({"field_evidence": []}, "provenance"),
    ],
)
def test_incomplete_records_are_refused(overrides, message) -> None:
    with pytest.raises(rc.RegistryCandidateError, match=message):
        build(**overrides)
