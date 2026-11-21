"""The apply driver maps manifest facts onto the published Catalog wire contract.

These tests use a fake transport: they prove the mapping and the resume/replay rules,
not that Catalog accepts them.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import registry_catalog_apply as apply

CATALOG = {
    "catalogKey": "berlin-recording-services",
    "configurationRevisionId": "46fb742c-f4d9-45dd-99ee-a364cf44b3c2",
    "mappingRevision": "8f7ff1e7ca2939719f5d21964d232cf506fd821af11ce91365db1baef542d5b0",
    "siteKey": "berlin-recording",
}
MANIFEST_DIGEST = "31324f7c6130a4521692d542de9b3c663ca0e1fa1a0d338b4a1164859f453629"


def digest(material: str) -> str:
    return material.ljust(64, "0")[:64]


def record(registry_id: str = "027a1f22-6b8e-5dc7-bbaf-f73c30fddabf", **overrides):
    value = {
        "registryId": registry_id,
        "status": "eligible",
        "statusReasons": [],
        "identity": {
            "decision": "create_new",
            "linkExistingListingId": None,
            "officialDomain": "trixx-studios.de",
        },
        "business": {
            "name": "TRIXX Studios",
            "nameLocale": "de-DE",
            "nameAssertionId": "53b15260-6327-57bb-a4f6-daf9bbdd2133",
            "address": {
                "street": "Prinzessinnenstrasse",
                "streetNumber": "16",
                "postalCode": "10969",
                "city": "Berlin",
                "countryCode": "DE",
                "districtKey": "friedrichshain-kreuzberg",
                "districtBasis": "address_source_verified",
                "assertionId": "53b15260-6327-57bb-a4f6-daf9bbdd2133",
            },
            "categories": [
                {
                    "categoryKey": "recording-studio",
                    "assertionId": "53b15260-6327-57bb-a4f6-daf9bbdd2133",
                }
            ],
            "displayLocales": ["de-DE"],
            "coordinates": None,
        },
        "assertions": [
            {
                "assertionId": "53b15260-6327-57bb-a4f6-daf9bbdd2133",
                "sourceKind": "publicWebsite",
                "sourceReference": "https://www.trixx-studios.de/de/",
                "sourceExternalId": None,
                "observedAtUtc": "2026-11-20T20:00:00Z",
                "usagePolicy": "publicAllowed",
                "evidenceDigest": digest("evidence"),
            }
        ],
        "digests": {
            "content": digest("content"),
            "evidence": digest("ev"),
            "eligibility": digest("el"),
        },
    }
    value.update(overrides)
    return value


def manifest(*records, catalog=CATALOG):
    return {
        "schemaRevision": apply.SCHEMA_REVISION,
        "sourceSystem": "dcpf.berlin_recording_services",
        "manifestDigest": MANIFEST_DIGEST,
        "catalog": catalog,
        "records": list(records),
    }


def test_record_maps_to_the_published_wire_contract():
    request = apply.build_request(manifest(), record())

    assert request["record"]["schemaRevision"] == apply.SCHEMA_REVISION
    assert request["record"]["sourceSystem"] == "dcpf.berlin_recording_services"
    assert request["record"]["manifestDigest"] == MANIFEST_DIGEST
    assert request["record"]["mappingRevision"] == CATALOG["mappingRevision"]
    assert request["record"]["configurationRevisionId"] == CATALOG["configurationRevisionId"]
    assert request["record"]["contentDigest"] == digest("content")
    assert request["record"]["identity"]["decision"] == "createNew"
    assert request["record"]["business"]["address"]["districtKey"] == "friedrichshain-kreuzberg"
    assert request["record"]["assertions"][0]["sourceKind"] == "publicWebsite"


def test_blocked_record_keeps_its_reasons_and_loses_its_facts():
    request = apply.build_request(
        manifest(),
        record(
            status="blocked", statusReasons=["district_derivation_not_confirmed"], business=None
        ),
    )

    assert request["record"]["status"] == "blocked"
    assert request["record"]["statusReasons"] == ["district_derivation_not_confirmed"]
    assert request["record"]["business"] is None


def test_link_existing_record_carries_the_named_listing():
    request = apply.build_request(
        manifest(),
        record(
            identity={
                "decision": "link_existing",
                "linkExistingListingId": "01a1efea-372c-7100-a7c0-e59d540707a5",
                "officialDomain": "hansastudios.de",
            }
        ),
    )

    assert request["record"]["identity"]["decision"] == "linkExisting"
    assert (
        request["record"]["identity"]["linkExistingListingId"]
        == "01a1efea-372c-7100-a7c0-e59d540707a5"
    )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"status": "probably"}, "unknown record status"),
        ({"identity": {"decision": "merge"}}, "unknown identity decision"),
    ],
)
def test_unmapped_verdicts_fail_instead_of_guessing(overrides, message):
    with pytest.raises(apply.ApplyError, match=message):
        apply.build_request(manifest(), record(**overrides))


def test_unsupported_manifest_schema_is_refused(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"schemaRevision": "registry-catalog-import-v2", "records": []}),
        encoding="utf-8",
    )

    with pytest.raises(apply.ApplyError, match="unsupported manifest schema"):
        apply.load_manifest(path)


def test_blocked_records_are_never_sent_to_catalog():
    sent = []

    tally = apply.run(
        manifest(record("a" * 8, status="blocked", statusReasons=["x"], business=None)),
        manifest(record("a" * 8, status="blocked", statusReasons=["x"], business=None))["records"],
        lambda key, token, body: sent.append(body) or {},
        None,
        None,
        require_unchanged=False,
    )

    assert sent == []
    assert tally["actions"] == {"blockedNotSent": 1}


def test_resume_skips_records_the_checkpoint_already_applied():
    records = [
        record("00000000-0000-5000-8000-000000000001"),
        record("00000000-0000-5000-8000-000000000002"),
    ]
    sent = []
    checkpoint = {
        "schemaVersion": apply.CHECKPOINT_SCHEMA,
        "records": {
            "00000000-0000-5000-8000-000000000001": {"status": "applied", "lastAction": "created"}
        },
    }

    tally = apply.run(
        manifest(*records),
        records,
        lambda key, token, body: (
            sent.append(body["record"]["registryId"]) or {"lastAction": "created", "placeId": "p"}
        ),
        checkpoint,
        None,
        require_unchanged=False,
    )

    assert sent == ["00000000-0000-5000-8000-000000000002"]
    assert tally["resumed"] == 1
    assert tally["applied"] == 1


def test_verify_requires_the_owner_to_report_unchanged():
    records = [record("00000000-0000-5000-8000-000000000001")]

    tally = apply.run(
        manifest(*records),
        records,
        lambda key, token, body: {"lastAction": "updated", "placeId": "p"},
        None,
        None,
        require_unchanged=True,
    )

    assert tally["failed"] == [
        {
            "registryId": "00000000-0000-5000-8000-000000000001",
            "error": "replay reported updated, expected unchanged",
        }
    ]


def test_rejection_is_recorded_and_the_run_continues(tmp_path):
    good = record("00000000-0000-5000-8000-000000000002")
    bad = record("00000000-0000-5000-8000-000000000001")
    path = tmp_path / "checkpoint.json"
    checkpoint = apply.read_checkpoint(path)

    def send(key: str, token: str, body: dict) -> dict:
        if body["record"]["registryId"] == bad["registryId"]:
            raise apply.ApplyError("Catalog rejected it: 409")
        return {"lastAction": "created", "placeId": "p", "contentDigest": digest("content")}

    tally = apply.run(
        manifest(bad, good), [bad, good], send, checkpoint, path, require_unchanged=False
    )

    assert [item["registryId"] for item in tally["failed"]] == [bad["registryId"]]
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["records"][bad["registryId"]]["status"] == "failed"
    assert stored["records"][good["registryId"]]["status"] == "applied"
    assert tally["applied"] == 1


def test_select_records_rejects_unknown_ids():
    with pytest.raises(apply.ApplyError, match="manifest has no record"):
        apply.select_records(manifest(record()), ["00000000-0000-5000-8000-0000000000ff"], None)
