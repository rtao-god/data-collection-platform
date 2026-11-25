"""The apply driver maps manifest facts onto the published Catalog wire contract.

These tests use a fake transport: they prove the mapping and the resume/replay rules,
not that Catalog accepts them.
"""

from __future__ import annotations

import email.message
import io
import json
import pathlib
import sys
import urllib.error

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
            "linkExisting": None,
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
        "manifestDigest": MANIFEST_DIGEST,
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
                "linkExisting": {
                    "listingId": "01a1efea-372c-7100-a7c0-e59d540707a5",
                    "placeId": None,
                },
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


def test_resume_resends_every_record_and_lets_catalog_report_unchanged():
    records = [
        record("00000000-0000-5000-8000-000000000001"),
        record("00000000-0000-5000-8000-000000000002"),
    ]
    sent = []
    manifest_value = manifest(*records)
    checkpoint = {
        "schemaVersion": apply.CHECKPOINT_SCHEMA,
        "records": {
            item["registryId"]: {
                "status": "applied",
                "lastAction": "created",
                "fingerprint": apply.record_fingerprint(manifest_value, item),
            }
            for item in records
        },
    }

    tally = apply.run(
        manifest_value,
        records,
        lambda key, token, body: (
            sent.append(body["record"]["registryId"])
            or {"lastAction": "unchanged", "placeId": "p", "contentDigest": digest("content")}
        ),
        checkpoint,
        None,
        require_unchanged=False,
    )

    # The local checkpoint is diagnostic only: Catalog decides whether a record is unchanged.
    assert sent == [item["registryId"] for item in records]
    assert tally["resumed"] == 2
    assert tally["unchanged"] == 2
    assert tally["changed"] == 0
    assert tally["drift"] == []


def test_changed_content_after_a_checkpoint_still_reaches_catalog():
    registry_id = "00000000-0000-5000-8000-000000000001"
    old = record(registry_id)
    changed = record(
        registry_id,
        digests={
            "content": digest("new-content"),
            "evidence": digest("ev"),
            "eligibility": digest("el"),
        },
    )
    manifest_value = manifest(changed)
    checkpoint = {
        "schemaVersion": apply.CHECKPOINT_SCHEMA,
        "records": {
            registry_id: {
                "status": "applied",
                "lastAction": "created",
                "fingerprint": apply.record_fingerprint(manifest_value, old),
            }
        },
    }
    sent = []

    tally = apply.run(
        manifest_value,
        [changed],
        lambda key, token, body: (
            sent.append(body["record"])
            or {"lastAction": "updated", "placeId": "p", "contentDigest": digest("new-content")}
        ),
        checkpoint,
        None,
        require_unchanged=False,
    )

    assert sent[0]["contentDigest"] == digest("new-content")
    assert tally["drift"] == [{"registryId": registry_id, "changed": ["content"]}]
    assert tally["changed"] == 1


def test_changed_mapping_configuration_or_manifest_reaches_catalog():
    registry_id = "00000000-0000-5000-8000-000000000001"
    item = record(registry_id)
    old_manifest = manifest(item)
    new_catalog = dict(
        CATALOG,
        configurationRevisionId="00000000-0000-4000-8000-000000000009",
        mappingRevision=digest("new-mapping"),
    )
    new_item = dict(item, manifestDigest=digest("new-manifest"))
    new_manifest = manifest(new_item, catalog=new_catalog)
    checkpoint = {
        "schemaVersion": apply.CHECKPOINT_SCHEMA,
        "records": {
            registry_id: {
                "status": "applied",
                "lastAction": "created",
                "fingerprint": apply.record_fingerprint(old_manifest, item),
            }
        },
    }
    sent = []

    tally = apply.run(
        new_manifest,
        [new_item],
        lambda key, token, body: (
            sent.append(body["record"])
            or {"lastAction": "updated", "placeId": "p", "contentDigest": digest("content")}
        ),
        checkpoint,
        None,
        require_unchanged=False,
    )

    assert sent[0]["configurationRevisionId"] == new_catalog["configurationRevisionId"]
    assert tally["drift"] == [
        {
            "registryId": registry_id,
            "changed": ["configurationRevisionId", "manifestDigest", "mappingRevision"],
        }
    ]


def test_interrupted_apply_is_retried_on_the_next_run():
    registry_id = "00000000-0000-5000-8000-000000000001"
    item = record(registry_id)
    manifest_value = manifest(item)
    checkpoint = {
        "schemaVersion": apply.CHECKPOINT_SCHEMA,
        "records": {registry_id: {"status": "failed", "error": "connection reset"}},
    }
    sent = []

    tally = apply.run(
        manifest_value,
        [item],
        lambda key, token, body: (
            sent.append(body["record"]["registryId"])
            or {"lastAction": "created", "placeId": "p", "contentDigest": digest("content")}
        ),
        checkpoint,
        None,
        require_unchanged=False,
    )

    assert sent == [registry_id]
    assert tally["failed"] == []
    assert tally["changed"] == 1


def test_repeated_identical_apply_writes_nothing_new_at_the_owner():
    registry_id = "00000000-0000-5000-8000-000000000001"
    item = record(registry_id)
    manifest_value = manifest(item)

    tally = apply.run(
        manifest_value,
        [item],
        lambda key, token, body: {
            "lastAction": "unchanged",
            "placeId": "p",
            "contentDigest": digest("content"),
        },
        None,
        None,
        require_unchanged=True,
    )

    assert tally["failed"] == []
    assert tally["unchanged"] == 1
    assert tally["changed"] == 0


def test_manual_catalog_edit_is_reported_as_review_not_as_a_write():
    registry_id = "00000000-0000-5000-8000-000000000001"
    item = record(registry_id)
    path = None

    tally = apply.run(
        manifest(item),
        [item],
        lambda key, token, body: {
            "lastAction": "reviewRequired",
            "placeId": "p",
            "contentDigest": digest("content"),
        },
        None,
        path,
        require_unchanged=False,
    )

    assert tally["failed"] == []
    assert tally["actions"] == {"reviewRequired": 1}
    assert tally["changed"] == 1


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
    assert tally["changed"] == 1


def test_select_records_rejects_unknown_ids():
    with pytest.raises(apply.ApplyError, match="manifest has no record"):
        apply.select_records(manifest(record()), ["00000000-0000-5000-8000-0000000000ff"], None)


def test_coordinate_maps_onto_the_catalog_coordinate_wire_contract():
    coordinate = {
        "latitude": 52.5028179,
        "longitude": 13.4115532,
        "accuracyMeters": 1.0,
        "source": "official_berlin_address_register",
        "datasetIdentifier": "adressen_berlin",
        "datasetVersion": "adressen_berlin@2026-11-21T06:51:28Z",
        "addressRecordId": "187448",
        "addressRecordDate": "2008-01-28",
        "sourceReference": "https://gdi.berlin.de/services/wfs/adressen_berlin?request=GetFeature",
        "sourceCrs": "EPSG:25833",
        "sourceX": "392181.815",
        "sourceY": "5818151.337",
        "sourceDigest": digest("raw"),
        "sourceObservedAtUtc": "2026-11-21T06:51:28Z",
        "assertionId": "77d3234d-3335-5741-8f47-ed75468d9f02",
    }
    value = record()
    value["business"]["address"]["coordinate"] = coordinate

    wire = apply.build_request(manifest(), value)["record"]["business"]["address"]["coordinate"]

    # Exactly the Catalog-owned JSON members, so a producer-side rename cannot slip through.
    assert set(wire) == {
        "latitude",
        "longitude",
        "accuracyMeters",
        "source",
        "datasetIdentifier",
        "datasetVersion",
        "addressRecordId",
        "addressRecordDate",
        "sourceReference",
        "sourceCrs",
        "sourceX",
        "sourceY",
        "sourceDigest",
        "sourceObservedAtUtc",
        "assertionId",
    }
    assert wire["sourceObservedAtUtc"] == "2026-11-21T06:51:28Z"
    assert wire["sourceDigest"] == coordinate["sourceDigest"]


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _rate_limited(detail: str, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "http://catalog/api", 429, "Too Many Requests", headers, io.BytesIO(detail.encode())
    )


def test_rate_limited_record_is_retried_with_the_stated_pause(monkeypatch):
    body = apply.build_request(manifest(record()), record())
    answers = [
        _rate_limited("budget exhausted", retry_after="7"),
        _FakeResponse({"lastAction": "unchanged"}),
    ]
    calls = []

    def urlopen(_request, timeout=None):
        calls.append(1)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(apply.urllib.request, "urlopen", urlopen)
    pauses = []
    send = apply.http_transport("http://catalog", "token", sleep=pauses.append)

    assert send("berlin-recording-services", "", body) == {"lastAction": "unchanged"}
    assert len(calls) == 2
    assert pauses == [7.0]


def test_rate_limit_retries_are_bounded_and_report_the_status(monkeypatch):
    body = apply.build_request(manifest(record()), record())

    def urlopen(_request, timeout=None):
        raise _rate_limited("budget exhausted")

    monkeypatch.setattr(apply.urllib.request, "urlopen", urlopen)
    pauses = []
    send = apply.http_transport(
        "http://catalog", "token", max_rate_limit_attempts=3, sleep=pauses.append
    )

    with pytest.raises(apply.ApplyError) as refusal:
        send("berlin-recording-services", "", body)

    assert "429 after 3 attempts" in str(refusal.value)
    assert "budget exhausted" in str(refusal.value)
    assert len(pauses) == 2
    assert pauses == [2.0, 4.0]


def test_rich_material_reaches_the_wire_only_when_the_producer_states_it():
    request = apply.build_request(
        manifest(),
        record(
            richFields={
                "descriptions": [
                    {
                        "locale": "de-DE",
                        "text": "Ein Aufnahmestudio.",
                        "assertionId": "53b15260-6327-57bb-a4f6-daf9bbdd2133",
                    }
                ],
                "contacts": [
                    {
                        "kind": "phone",
                        "target": "tel:+49301234567",
                        "label": None,
                        "assertionId": "53b15260-6327-57bb-a4f6-daf9bbdd2133",
                    }
                ],
            }
        ),
    )

    fields = request["record"]["richFields"]
    assert fields["descriptions"][0]["locale"] == "de-DE"
    assert fields["descriptions"][0]["text"] == "Ein Aufnahmestudio."
    assert fields["contacts"][0]["target"] == "tel:+49301234567"
    # Nothing the producer did not state is sent: an empty section never says "delete this".
    assert apply.build_request(manifest(), record())["record"]["richFields"] is None


def test_a_rich_section_states_both_halves_because_catalog_binds_the_whole_section():
    request = apply.build_request(
        manifest(),
        record(
            richFields={
                "descriptions": [
                    {
                        "locale": "de-DE",
                        "text": "Ein Aufnahmestudio.",
                        "assertionId": "53b15260-6327-57bb-a4f6-daf9bbdd2133",
                    }
                ]
            }
        ),
    )

    fields = request["record"]["richFields"]
    assert fields["contacts"] == []
    assert fields["descriptions"][0]["locale"] == "de-DE"
