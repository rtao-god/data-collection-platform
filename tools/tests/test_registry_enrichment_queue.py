from __future__ import annotations

import copy

import pytest
from tools import registry_catalog_manifest as manifest
from tools import registry_enrichment_queue as queue
from tools import studio_registry as registry

REGISTRY_ID = "11111111-1111-4111-8111-111111111111"


def _registry(*, contacts: list[dict] | None = None) -> dict:
    return {
        "campaign_key": "berlin_recording_services",
        "schema_revision": "studio-registry-v1",
        "records": [
            {
                "registry_id": REGISTRY_ID,
                "canonical_name": "Example Studio",
                "contacts": contacts or [],
                "photo_candidates": [],
                "reviews": [],
                "field_evidence": [],
            }
        ],
    }


def _incoming(registry_document: dict, *, value: str = "https://example.de/") -> dict:
    record = registry_document["records"][0]
    run_id = "source-run-1"
    return {
        "schema_revision": queue.INCOMING_SCHEMA,
        "campaign_key": "berlin_recording_services",
        "source_runs": [
            {
                "run_id": run_id,
                "requested_at_utc": "2026-11-26T04:56:01Z",
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
        "observations": [
            {
                "observation_id": "source:Q1:P856:rev1",
                "source_run_id": run_id,
                "registry_id": REGISTRY_ID,
                "subject_match": {
                    "state": "matched_review_required",
                    "source_label": "Example Studio",
                },
                "field_path": "contacts",
                "operation": "add_collection_item",
                "normalized_value": {
                    "kind": "website",
                    "target": value,
                    "validation_state": "unvalidated",
                    "evidence_refs": [],
                },
                "source": {
                    "source_key": "wikidata",
                    "source_record_id": "Q1",
                    "dataset_release": "wikidata-entity-revision:1",
                    "source_url": "https://www.wikidata.org/wiki/Special:EntityData/Q1.json",
                    "acquired_at_utc": "2026-11-26T04:56:01Z",
                    "source_effective_at_utc": None,
                    "response_digest_sha256": "a" * 64,
                },
                "baseline_record_digest_sha256": manifest.digest_of(record),
                "status": "pending_verification",
            }
        ],
    }


def _add_observation(
    incoming: dict,
    *,
    observation_id: str,
    value: object,
    field_path: str,
    operation: str,
    source_record_id: str,
    effective_at: str | None = None,
) -> None:
    original = incoming["observations"][0]
    item = copy.deepcopy(original)
    item.update(
        {
            "observation_id": observation_id,
            "field_path": field_path,
            "operation": operation,
            "normalized_value": value,
            "baseline_record_digest_sha256": original["baseline_record_digest_sha256"],
        }
    )
    item["source"]["source_record_id"] = source_record_id
    item["source"]["dataset_release"] = f"release:{source_record_id}"
    item["source"]["source_url"] = f"https://source.example/{source_record_id}"
    item["source"]["source_effective_at_utc"] = effective_at
    incoming["observations"].append(item)
    incoming["source_runs"][0]["coverage"].update({"requested": 2, "obtained": 2, "matched": 2})


def test_missing_contact_candidate_is_an_actionable_gap() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)

    projection = queue.build_projection(registry_document, incoming)

    assert projection["conflicts"] == []
    assert len(projection["field_gaps"]) == 1
    candidate = projection["field_gaps"][0]["candidate_values"][0]
    assert projection["field_gaps"][0]["registry_id"] == REGISTRY_ID
    assert candidate["observation_ids"] == ["source:Q1:P856:rev1"]
    assert candidate["state"] == "candidate_addition_pending"


def test_unverified_page_observation_needs_no_dataset_release_record_id_or_digest() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    source = incoming["observations"][0]["source"]
    source.update(
        {
            "source_record_id": None,
            "dataset_release": None,
            "response_digest_sha256": None,
            "verification_status": "unverified",
            "source_url": "https://example-studio.de/contact/",
        }
    )

    projection = queue.build_projection(registry_document, incoming)

    evidence = projection["field_gaps"][0]["candidate_values"][0]["evidence"][0]
    assert evidence["source_record_id"] is None
    assert evidence["dataset_release"] is None
    assert evidence["response_digest_sha256"] is None
    assert evidence["verification_status"] == "unverified"


def test_missing_digest_without_explicit_unverified_status_is_rejected() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    incoming["observations"][0]["source"]["response_digest_sha256"] = None

    with pytest.raises(queue.QueueError, match="explicit verification_status='unverified'"):
        queue.build_projection(registry_document, incoming)


def test_different_contacts_of_same_kind_are_additive_not_conflicting() -> None:
    registry_document = _registry(
        contacts=[
            {
                "kind": "website",
                "target": "https://old.example/",
                "validation_state": "validated",
                "evidence_refs": ["ev-0123456789abcdef"],
            }
        ]
    )
    incoming = _incoming(registry_document)

    projection = queue.build_projection(registry_document, incoming)

    assert projection["conflicts"] == []
    assert len(projection["field_gaps"]) == 1
    assert projection["field_gaps"][0]["candidate_values"][0]["value"]["target"] == (
        "https://example.de/"
    )


def test_multiple_incoming_values_for_one_empty_single_value_field_conflict() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    incoming["observations"][0].update(
        {
            "field_path": "address.street",
            "operation": "set_value",
            "normalized_value": "Alpha Street",
        }
    )
    _add_observation(
        incoming,
        observation_id="source:Q2:street:rev1",
        value="Beta Street",
        field_path="address.street",
        operation="set_value",
        source_record_id="Q2",
    )

    projection = queue.build_projection(registry_document, incoming)

    assert len(projection["conflicts"]) == 1
    conflict = projection["conflicts"][0]
    assert conflict["reason"] == "competing_single_value_observations"
    assert {item["value"] for item in conflict["competing_values"]} == {
        "Alpha Street",
        "Beta Street",
    }
    assert projection["field_gaps"] == []


def test_source_effective_date_can_recommend_a_winner_but_capture_time_cannot() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    incoming["observations"][0].update(
        {
            "field_path": "address.street",
            "operation": "set_value",
            "normalized_value": "Alpha Street",
        }
    )
    incoming["observations"][0]["source"]["source_effective_at_utc"] = "2026-10-01T00:00:00Z"
    _add_observation(
        incoming,
        observation_id="source:Q2:street:rev1",
        value="Beta Street",
        field_path="address.street",
        operation="set_value",
        source_record_id="Q2",
        effective_at="2026-11-01T00:00:00Z",
    )

    projection = queue.build_projection(registry_document, incoming)

    winner = projection["conflicts"][0]["suggested_winner"]
    assert winner["observation_ids"] == ["source:Q2:street:rev1"]
    assert winner["date"] == "2026-11-01T00:00:00Z"
    assert winner["basis"] == "source_effective_at_utc"


def test_stale_baseline_blocks_projection_as_conflict() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    incoming["observations"][0]["baseline_record_digest_sha256"] = "b" * 64

    projection = queue.build_projection(registry_document, incoming)

    assert len(projection["conflicts"]) == 1
    assert projection["conflicts"][0]["reason"] == "baseline_record_digest_changed"
    assert projection["conflicts"][0]["current_record_digest_sha256"] == manifest.digest_of(
        registry_document["records"][0]
    )
    assert projection["field_gaps"] == []


def test_batch_rebase_refuses_concurrent_content_edit_without_mutating_incoming() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    observation = incoming["observations"][0]
    source_url = "https://example-studio.de/contact/"
    response_digest = "b" * 64
    reviewer = "operator"
    decided_at = "2026-11-27T11:00:00Z"
    policy_digest = "c" * 64
    expected_policy = {
        "decision": "approved",
        "reviewer": reviewer,
        "decided_at_utc": decided_at,
        "scope": "official_site facts_only",
        "policy_digest_sha256": policy_digest,
    }
    observation["source"].update(
        {
            "source_key": "official_site",
            "source_url": source_url,
            "response_digest_sha256": response_digest,
            "verification_status": "verified",
        }
    )
    queue._capture_baseline_states(registry_document, incoming)
    evidence_id = registry.evidence_id_for(REGISTRY_ID, source_url, response_digest, "contacts")
    record = registry_document["records"][0]
    record["field_evidence"].append(
        {
            "evidence_id": evidence_id,
            "field_path": "contacts",
            "source_kind": "official_site",
            "source_url": source_url,
            "response_digest_sha256": response_digest,
            "policy_decision": expected_policy,
        }
    )
    record["reviews"].append(
        {
            "review_id": "review:batch-rebase",
            "kind": "source_policy",
            "decision": "approved",
            "actor_id": reviewer,
            "decided_at_utc": decided_at,
            "source_evidence_digest_sha256": policy_digest,
            "note": "operator approved official-site facts-only usage for this campaign",
        }
    )
    record["contacts"].append(
        {
            "kind": "website",
            "target": "https://example-studio.de/",
            "validation_state": "unvalidated",
            "evidence_refs": [evidence_id],
        }
    )
    incoming_before = copy.deepcopy(incoming)

    with pytest.raises(queue.QueueError, match="changed beyond record-evidence and decide"):
        queue.rebase_metadata_batch(
            registry_document,
            incoming,
            policy_digest_sha256=policy_digest,
            reviewer=reviewer,
            decided_at_utc=decided_at,
        )

    assert incoming == incoming_before


def test_resolved_or_consumed_status_without_canonical_decision_stays_visible() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    incoming["observations"][0]["status"] = "consumed"
    incoming["observations"][0]["resolution"] = {"conflict_id": "conflict-fake"}

    projection = queue.build_projection(registry_document, incoming)

    assert len(projection["conflicts"]) == 1
    assert projection["conflicts"][0]["reason"] == ("resolution_not_recorded_in_canonical_history")


def test_recorded_canonical_decision_is_required_before_consumed_item_disappears() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    item = incoming["observations"][0]
    item.update(
        {
            "field_path": "address.street",
            "operation": "set_value",
            "normalized_value": "Alpha Street",
        }
    )
    item["status"] = "resolved"
    subject_key = f"registry:{REGISTRY_ID}"
    scope = "address.street"
    conflict_id = queue._conflict_id(subject_key, "address.street", scope)
    baseline = item["baseline_record_digest_sha256"]
    evidence_id = "ev-0123456789abcdef"
    evidence_digest = "c" * 64
    record = registry_document["records"][0]
    record["address"] = {"street": "Alpha Street"}
    record["field_evidence"].append(
        {
            "evidence_id": evidence_id,
            "field_path": scope,
            "response_digest_sha256": evidence_digest,
        }
    )
    evidence_digest_set = registry.sha256_hex(registry.canonical_json([evidence_digest]))
    text_digest = queue._canonical_value_digest(scope, scope, "Alpha Street")
    review_id = "review-enrichment-1"
    record["reviews"].append(
        {
            "review_id": review_id,
            "kind": "fact",
            "decision": "approved",
            "text_digest_sha256": text_digest,
            "source_evidence_digest_sha256": evidence_digest_set,
            "evidence_refs": [evidence_id],
            "note": f"operator accepted studio-enrichment-resolution:{conflict_id}:{baseline}",
        }
    )
    item["resolution"] = {
        "conflict_id": conflict_id,
        "field_path": scope,
        "scope": scope,
        "baseline_record_digest_sha256": baseline,
        "accepted_value": "Alpha Street",
        "evidence_refs": [evidence_id],
        "canonical_review_id": review_id,
    }

    projection = queue.build_projection(registry_document, incoming)

    assert projection["conflicts"] == []
    assert projection["field_gaps"] == []


def test_confirmed_new_identity_without_registry_id_remains_visible() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    item = incoming["observations"][0]
    item["registry_id"] = None
    item["baseline_record_digest_sha256"] = None
    item["subject_match"] = {
        "state": "confirmed_new_identity",
        "stable_identity_key": "website:new-studio.example",
    }
    incoming["source_runs"][0]["coverage"].update({"matched": 0, "new": 1})

    projection = queue.build_projection(registry_document, incoming)

    assert len(projection["conflicts"]) == 1
    assert projection["conflicts"][0]["registry_id"] is None
    assert projection["conflicts"][0]["reason"] == ("new_identity_pending_registry_admission")


def test_unmatched_address_conflict_keeps_accepted_candidate_and_field_differences() -> None:
    registry_document = _registry()
    record = registry_document["records"][0]
    record["address"] = {
        "street": "Accepted Street",
        "street_number": "7",
        "postal_code": "10115",
        "city": "Berlin",
    }
    record["restrictions"] = {"missing": ["explicit_exclusion"], "rejected": []}
    incoming = _incoming(registry_document)
    incoming["source_runs"][0]["coverage"].update({"matched": 0, "ambiguous": 1})
    identity = incoming["observations"][0]
    identity.update(
        {
            "registry_id": None,
            "baseline_record_digest_sha256": None,
            "field_path": "canonical_name",
            "scope": "identity",
            "operation": "identity_candidate",
            "normalized_value": "Example Studio",
            "subject_match": {
                "stable_identity_key": "musicbrainz:place:example",
                "state": "address_disagreement_ambiguous",
                "candidate_registry_ids": [REGISTRY_ID],
                "source_address": "Different Street 9, 10117 Berlin",
                "operator_action": "verify address and identity independently",
            },
        }
    )
    for field, value in (
        ("street", "Different Street"),
        ("street_number", "9"),
        ("postal_code", "10117"),
        ("city", "Berlin"),
    ):
        observation = copy.deepcopy(identity)
        observation.update(
            {
                "observation_id": f"source:example:address:{field}",
                "field_path": f"address.{field}",
                "scope": f"address.{field}",
                "operation": "propose_value",
                "original_value": "Different Street 9, 10117 Berlin",
                "normalized_value": value,
            }
        )
        incoming["observations"].append(observation)

    projection = queue.build_projection(registry_document, incoming)

    identity_conflict = next(c for c in projection["conflicts"] if c["field_path"] == "identity")
    assert identity_conflict["reason"] == "address_mismatch_requires_identity_review"
    assert identity_conflict["required_verification_action"] == (
        "verify address and identity independently"
    )
    assert identity_conflict["accepted_old_value"] == [
        {
            "registry_id": REGISTRY_ID,
            "canonical_name": "Example Studio",
            "address": record["address"],
            "baseline_record_digest_sha256": manifest.digest_of(record),
            "restrictions": record["restrictions"],
        }
    ]
    assert len(identity_conflict["accepted_old_value"][0]["baseline_record_digest_sha256"]) == 64

    address_conflicts = [
        c
        for c in projection["conflicts"]
        if c["reason"] == "address_disagreement_ambiguous_identity"
    ]
    assert {
        (c["field_path"], c["accepted_old_value"], c["competing_values"][0]["value"])
        for c in address_conflicts
    } == {
        ("address.street", "Accepted Street", "Different Street"),
        ("address.street_number", "7", "9"),
        ("address.postal_code", "10115", "10117"),
    }
    assert all(c["baseline_record_digest_sha256"] for c in address_conflicts)


def test_identity_ambiguity_retains_all_candidate_registry_records() -> None:
    registry_document = _registry()
    second = copy.deepcopy(registry_document["records"][0])
    second["registry_id"] = "22222222-2222-4222-8222-222222222222"
    second["address"] = {"street": "Another Street", "street_number": "2"}
    registry_document["records"].append(second)
    incoming = _incoming(registry_document)
    incoming["source_runs"][0]["coverage"].update({"matched": 0, "ambiguous": 1})
    item = incoming["observations"][0]
    item.update(
        {
            "registry_id": None,
            "baseline_record_digest_sha256": None,
            "subject_match": {
                "stable_identity_key": "musicbrainz:place:ambiguous",
                "state": "ambiguous_multiple_registry_ids",
                "candidate_registry_ids": [REGISTRY_ID, second["registry_id"]],
                "operator_action": "resolve both possible identities",
            },
        }
    )

    projection = queue.build_projection(registry_document, incoming)

    assert len(projection["conflicts"]) == 1
    conflict = projection["conflicts"][0]
    assert conflict["reason"] == "identity_matches_multiple_registry_records"
    assert [candidate["registry_id"] for candidate in conflict["accepted_old_value"]] == [
        REGISTRY_ID,
        second["registry_id"],
    ]
    assert conflict["required_verification_action"] == "resolve both possible identities"
    assert all(
        len(candidate["baseline_record_digest_sha256"]) == 64
        for candidate in conflict["accepted_old_value"]
    )


def test_unknown_candidate_registry_reference_is_rejected() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    incoming["observations"][0]["subject_match"]["candidate_registry_ids"] = [
        "22222222-2222-4222-8222-222222222222"
    ]

    with pytest.raises(
        queue.QueueError, match="candidate_registry_ids must reference canonical records"
    ):
        queue.build_projection(registry_document, incoming)


def test_duplicate_observation_identity_is_rejected() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    incoming["observations"].append(copy.deepcopy(incoming["observations"][0]))

    with pytest.raises(queue.QueueError, match="observation_id is duplicated"):
        queue.build_projection(registry_document, incoming)


def test_unknown_registry_identity_is_rejected() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    incoming["observations"][0]["registry_id"] = "22222222-2222-4222-8222-222222222222"

    with pytest.raises(queue.QueueError, match="not present in the canonical registry"):
        queue.build_projection(registry_document, incoming)


def test_invalid_run_coverage_cannot_report_more_obtained_than_requested() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    incoming["source_runs"][0]["coverage"]["obtained"] = 2

    with pytest.raises(queue.QueueError, match="obtained exceeds requested"):
        queue.build_projection(registry_document, incoming)


def test_same_contact_already_in_canonical_is_not_a_conflict_but_evidence_remains_pending() -> None:
    existing = {
        "kind": "website",
        "target": "https://example.de/",
        "validation_state": "validated",
        "evidence_refs": ["ev-0123456789abcdef"],
    }
    registry_document = _registry(contacts=[existing])
    incoming = _incoming(registry_document)

    projection = queue.build_projection(registry_document, incoming)

    assert projection["conflicts"] == []
    candidate = projection["field_gaps"][0]["candidate_values"][0]
    assert candidate["state"] == "canonical_value_present_evidence_merge_pending"


def _address_conflict_fixture() -> tuple[dict, dict, dict]:
    registry_document = _registry()
    record = registry_document["records"][0]
    record["address"] = {"street": "Old Street"}
    old_evidence_id = "ev-1111111111111111"
    old_digest = "b" * 64
    new_evidence_id = "ev-2222222222222222"
    new_digest = "c" * 64
    record["field_evidence"] = [
        {
            "evidence_id": old_evidence_id,
            "field_path": "address.street",
            "response_digest_sha256": old_digest,
            "source_effective_at_utc": "2026-10-01T00:00:00Z",
        },
        {
            "evidence_id": new_evidence_id,
            "field_path": "address.street",
            "response_digest_sha256": new_digest,
            "source_effective_at_utc": "2026-11-01T00:00:00Z",
        },
    ]
    old_source_digest = registry.sha256_hex(registry.canonical_json([old_digest]))
    record["reviews"].append(
        {
            "review_id": "review-old-street",
            "kind": "fact",
            "decision": "approved",
            "sequence": 1,
            "text_digest_sha256": registry.enrichment_value_digest(
                "address.street", "address.street", "Old Street"
            ),
            "source_evidence_digest_sha256": old_source_digest,
            "evidence_refs": [old_evidence_id],
        }
    )
    incoming = _incoming(registry_document)
    observation = incoming["observations"][0]
    observation.update(
        {
            "field_path": "address.street",
            "operation": "set_value",
            "normalized_value": "New Street",
            "status": "pending_verification",
        }
    )
    observation["source"].update(
        {
            "response_digest_sha256": new_digest,
            "source_effective_at_utc": "2026-11-01T00:00:00Z",
        }
    )
    return registry_document, incoming, {"evidence_id": new_evidence_id}


def _resolution_patch(registry_document: dict, incoming: dict, evidence_id: str) -> dict:
    record = registry_document["records"][0]
    observation = incoming["observations"][0]
    scope = observation["field_path"]
    conflict_id = registry.enrichment_conflict_id(REGISTRY_ID, scope, scope)
    baseline = manifest.digest_of(record)
    accepted_value = observation["normalized_value"]
    return {
        "schema_revision": registry.PATCH_SCHEMA_REVISION,
        "patches": [
            {
                "registry_id": REGISTRY_ID,
                "field_path": scope,
                "value": accepted_value,
                "evidence_refs": [evidence_id],
                "reviewer": "operator-1",
                "decided_at_utc": "2026-11-27T10:00:00Z",
                "replace_existing": True,
                "conflict_resolution": {
                    "conflict_id": conflict_id,
                    "field_path": scope,
                    "scope": scope,
                    "baseline_record_digest_sha256": baseline,
                    "accepted_value": accepted_value,
                    "evidence_refs": [evidence_id],
                },
            }
        ],
    }


def test_address_conflict_recommends_latest_source_effective_evidence() -> None:
    registry_document, incoming, _ = _address_conflict_fixture()

    projection = queue.build_projection(registry_document, incoming)

    assert len(projection["conflicts"]) == 1
    winner = projection["conflicts"][0]["suggested_winner"]
    assert winner["value"] == "New Street"
    assert winner["observation_ids"] == ["source:Q1:P856:rev1"]
    assert winner["date"] == "2026-11-01T00:00:00Z"


def test_address_conflict_can_recommend_the_existing_value_from_its_reviewed_evidence() -> None:
    registry_document, incoming, _ = _address_conflict_fixture()
    incoming["observations"][0]["source"]["source_effective_at_utc"] = "2026-09-01T00:00:00Z"

    projection = queue.build_projection(registry_document, incoming)

    winner = projection["conflicts"][0]["suggested_winner"]
    assert winner["accepted_in_registry"] is True
    assert winner["value"] == "Old Street"
    assert winner["evidence_refs"] == ["ev-1111111111111111"]
    assert winner["date"] == "2026-10-01T00:00:00Z"


def test_acquisition_time_never_breaks_a_source_effective_date_tie() -> None:
    registry_document = _registry()
    incoming = _incoming(registry_document)
    incoming["observations"][0].update(
        {
            "field_path": "address.street",
            "operation": "set_value",
            "normalized_value": "Alpha Street",
        }
    )
    incoming["observations"][0]["source"]["source_effective_at_utc"] = None
    incoming["observations"][0]["source"]["acquired_at_utc"] = "2026-11-29T00:00:00Z"
    _add_observation(
        incoming,
        observation_id="source:Q2:street:rev1",
        value="Beta Street",
        field_path="address.street",
        operation="set_value",
        source_record_id="Q2",
    )
    incoming["observations"][1]["source"]["acquired_at_utc"] = "2026-11-28T00:00:00Z"

    projection = queue.build_projection(registry_document, incoming)

    assert projection["conflicts"][0]["suggested_winner"] is None


def test_patch_apply_records_resolution_and_projection_closes_it_idempotently() -> None:
    registry_document, incoming, evidence = _address_conflict_fixture()
    patch = _resolution_patch(registry_document, incoming, evidence["evidence_id"])

    first = registry.apply_patch(registry_document, patch)

    assert first.problems == []
    assert first.applied == 1
    incoming["observations"][0]["status"] = "resolved"
    incoming["observations"][0]["resolution"] = {
        **patch["patches"][0]["conflict_resolution"],
    }
    projection = queue.build_projection(registry_document, incoming)
    assert projection["conflicts"] == []
    assert projection["field_gaps"] == []
    decision_count = len(registry_document["records"][0]["reviews"])
    after_first = copy.deepcopy(registry_document)

    second = registry.apply_patch(registry_document, patch)

    assert second.problems == []
    assert second.applied == 0
    assert second.unchanged == 1
    assert registry_document == after_first
    assert len(registry_document["records"][0]["reviews"]) == decision_count
    assert queue.build_projection(registry_document, incoming)["conflicts"] == []


def test_conflict_patch_rejects_stale_baseline_without_mutation() -> None:
    registry_document, incoming, evidence = _address_conflict_fixture()
    patch = _resolution_patch(registry_document, incoming, evidence["evidence_id"])
    registry_document["records"][0]["canonical_name"] = "Edited after review"
    before = copy.deepcopy(registry_document)

    outcome = registry.apply_patch(registry_document, patch)

    assert any(problem.code == "patch_resolution_stale" for problem in outcome.problems)
    assert registry_document == before


def _commons_candidate(index: int, *, registry_id: str | None) -> dict:
    page_id = 1000 + index
    return {
        "candidate_id": f"commons:{page_id}",
        "source_url": f"https://upload.wikimedia.org/wikipedia/commons/Photo-{page_id}.jpg",
        "source_page_url": f"https://commons.wikimedia.org/wiki/File:Photo-{page_id}.jpg",
        "source_title": f"File:Photo-{page_id}.jpg",
        "image_description": (
            "Entrance of Hansa Studios" if registry_id else "Meistersaal in Berlin"
        ),
        "image_taken_at": "2021-09-04",
        "image_metadata_recorded_at": "2023-10-30 18:38:54",
        "rights_holder": "Creator",
        "rights_holder_profile_url": "https://commons.wikimedia.org/wiki/User:Creator",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0",
        "license_version": "CC BY-SA 4.0",
        "rights_state": "candidate_unreviewed",
        "observed_at_utc": "2026-11-26T11:26:45Z",
        "evidence_digest_sha256": "b" * 64,
        "omission_reason": None,
        "attribution_required": "true",
        "credit": "Own work",
        "mime": "image/jpeg",
        "size_bytes": 100000 + index,
        "width": 1200,
        "height": 800,
        "subject_registry_id": registry_id,
        "subject_match_status": (
            "explicit_hansa_description_unverified" if registry_id else "venue_relation_unverified"
        ),
        "image_bytes_downloaded": False,
        "image_bytes_sha256": None,
    }


def _commons_incoming() -> dict:
    candidates = [
        _commons_candidate(index, registry_id=REGISTRY_ID if index < 2 else None)
        for index in range(5)
    ]
    return {
        "schema_revision": queue.INCOMING_SCHEMA,
        "campaign_key": "berlin_recording_services",
        "source_runs": [
            {
                "run_id": "commons-search-1",
                "source_key": "wikimedia_commons",
                "requested_at_utc": "2026-11-26T11:26:45Z",
                "response_digest_sha256": "b" * 64,
                "coverage": {
                    "requested": 10,
                    "obtained": 5,
                    "matched": 1,
                    "new": 0,
                    "ambiguous": 3,
                    "unavailable": 0,
                    "denied": 0,
                },
                "outcome_details": {"photo_candidates": candidates},
            }
        ],
        "observations": [],
        "record_baselines": {},
    }


def _record_commons_photo_decision(registry_document: dict, observation: dict) -> None:
    record = registry_document["records"][0]
    value = observation["normalized_value"]
    record["photo_candidates"].append(copy.deepcopy(value))
    evidence_id = "commons-photo-evidence"
    record["field_evidence"].append(
        {
            "evidence_id": evidence_id,
            "field_path": "photo_candidates",
            "response_digest_sha256": value["evidence_digest_sha256"],
        }
    )
    conflict_id = registry.enrichment_conflict_id(
        REGISTRY_ID, "photo_candidates", observation["scope"]
    )
    baseline = "c" * 64
    record["reviews"].append(
        {
            "kind": "fact",
            "decision": "approved",
            "text_digest_sha256": registry.enrichment_value_digest(
                "photo_candidates", observation["scope"], value
            ),
            "source_evidence_digest_sha256": registry.sha256_hex(
                registry.canonical_json([value["evidence_digest_sha256"]])
            ),
            "evidence_refs": [evidence_id],
            "note": f"Accepted candidate; studio-enrichment-resolution:{conflict_id}:{baseline}",
        }
    )


def test_commons_photos_materialize_once_and_preserve_review_state(tmp_path) -> None:
    registry_document = _registry()
    incoming_path = tmp_path / "studios-incoming.json"
    registry_path = tmp_path / "studios.json"
    incoming = _commons_incoming()
    registry_path.write_text(registry.canonical_json(registry_document), encoding="utf-8")
    incoming_path.write_text(registry.canonical_json(incoming), encoding="utf-8")
    baseline_digest = registry.registry_record_digest(registry_document["records"][0])

    first = queue.materialize_photo_candidates_file(
        registry_path, incoming_path, run_id="commons-search-1"
    )

    assert first == {
        "added": 5,
        "restored": 0,
        "updated": 0,
        "unchanged": 0,
        "already_materialized": 0,
        "total": 5,
    }
    materialized = registry.read_json(incoming_path)
    photos = materialized["observations"]
    assert len(photos) == 5
    assert {item["field_path"] for item in photos} == {"photo_candidates"}
    assert {item["operation"] for item in photos} == {"add_collection_item"}
    assert sum(item["registry_id"] == REGISTRY_ID for item in photos) == 2
    assert sum(item["registry_id"] is None for item in photos) == 3
    assert all(item["status"] == "pending_verification" for item in photos)
    assert all(
        item["normalized_value"]["rights_state"] == "candidate_unreviewed" for item in photos
    )
    assert all(item["image_bytes_downloaded"] is False for item in photos)
    assert all(item["image_bytes_sha256"] is None for item in photos)
    assert all(item["normalized_value"]["evidence_digest_sha256"] == "b" * 64 for item in photos)
    assert all(item["photo_metadata"]["attribution_required"] is True for item in photos)
    assert all(item["source"]["verification_status"] == "pending_verification" for item in photos)
    assert all(item["observation_id"].startswith("wikimedia_commons:photo:") for item in photos)
    assert all(len(item["source_provenance"]) == 1 for item in photos)
    assert registry.registry_record_digest(registry_document["records"][0]) == baseline_digest
    queue._validate_incoming(materialized, {REGISTRY_ID: registry_document["records"][0]})

    photos[0]["status"] = "pending_review"
    photos[0]["operator_note"] = "preserve this review state"
    incoming_path.write_text(registry.canonical_json(materialized), encoding="utf-8")
    before_repeat = incoming_path.read_bytes()
    second = queue.materialize_photo_candidates_file(
        registry_path, incoming_path, run_id="commons-search-1"
    )

    assert second == {
        "added": 0,
        "restored": 0,
        "updated": 0,
        "unchanged": 5,
        "already_materialized": 0,
        "total": 5,
    }
    assert incoming_path.read_bytes() == before_repeat
    preserved = registry.read_json(incoming_path)["observations"][0]
    assert preserved["status"] == "pending_review"
    assert preserved["operator_note"] == "preserve this review state"


def test_commons_photo_without_bytes_cannot_claim_cleared_rights() -> None:
    registry_document = _registry()
    incoming = _commons_incoming()
    incoming["source_runs"][0]["outcome_details"]["photo_candidates"][0]["rights_state"] = (
        "rights_cleared"
    )
    before = copy.deepcopy(incoming)

    with pytest.raises(queue.QueueError, match="must remain candidate_unreviewed"):
        queue.materialize_photo_candidates(registry_document, incoming, run_id="commons-search-1")

    assert incoming == before


def test_missing_photo_observation_is_restored_without_canonical_decision() -> None:
    registry_document = _registry()
    incoming = _commons_incoming()
    queue.materialize_photo_candidates(registry_document, incoming, run_id="commons-search-1")
    removed = incoming["observations"].pop(0)
    recorded_ids = incoming["source_runs"][0]["outcome_details"]["photo_observation_ids"]
    assert removed["observation_id"] in recorded_ids

    result = queue.materialize_photo_candidates(
        registry_document, incoming, run_id="commons-search-1"
    )

    assert result == {
        "added": 1,
        "restored": 1,
        "updated": 0,
        "unchanged": 4,
        "already_materialized": 0,
        "total": 5,
    }
    assert len(incoming["observations"]) == 5
    assert removed["observation_id"] in {
        item["observation_id"] for item in incoming["observations"]
    }


def test_missing_photo_observation_is_suppressed_only_after_canonical_decision() -> None:
    registry_document = _registry()
    incoming = _commons_incoming()
    queue.materialize_photo_candidates(registry_document, incoming, run_id="commons-search-1")
    removed = next(item for item in incoming["observations"] if item["registry_id"] == REGISTRY_ID)
    incoming["observations"].remove(removed)

    record = registry_document["records"][0]
    _record_commons_photo_decision(registry_document, removed)

    result = queue.materialize_photo_candidates(
        registry_document, incoming, run_id="commons-search-1"
    )

    assert result["already_materialized"] == 1
    assert result["added"] == 0
    assert len(incoming["observations"]) == 4
    assert removed["observation_id"] not in {
        item["observation_id"] for item in incoming["observations"]
    }
    assert record["photo_candidates"][0]["rights_state"] == "candidate_unreviewed"


def test_same_commons_photo_in_new_run_upserts_provenance_without_duplicate() -> None:
    registry_document = _registry()
    incoming = _commons_incoming()
    queue.materialize_photo_candidates(registry_document, incoming, run_id="commons-search-1")
    second_run = copy.deepcopy(incoming["source_runs"][0])
    second_run["outcome_details"] = {
        "photo_candidates": copy.deepcopy(
            incoming["source_runs"][0]["outcome_details"]["photo_candidates"]
        )
    }
    second_run["run_id"] = "commons-search-2"
    second_run["requested_at_utc"] = "2026-11-27T11:26:45Z"
    second_run["response_digest_sha256"] = "c" * 64
    for candidate in second_run["outcome_details"]["photo_candidates"]:
        candidate["observed_at_utc"] = "2026-11-27T11:26:45Z"
        candidate["evidence_digest_sha256"] = "c" * 64
    incoming["source_runs"].append(second_run)

    result = queue.materialize_photo_candidates(
        registry_document, incoming, run_id="commons-search-2"
    )

    assert result == {
        "added": 0,
        "restored": 0,
        "updated": 5,
        "unchanged": 0,
        "already_materialized": 0,
        "total": 5,
    }
    assert len(incoming["observations"]) == 5
    for observation in incoming["observations"]:
        assert observation["observation_id"].startswith("wikimedia_commons:photo:")
        assert [entry["source_run_id"] for entry in observation["source_provenance"]] == [
            "commons-search-1",
            "commons-search-2",
        ]
        candidate_digests = {
            entry["candidate_evidence_digest_sha256"] for entry in observation["source_provenance"]
        }
        assert candidate_digests == {
            "b" * 64,
            "c" * 64,
        }

    repeated = queue.materialize_photo_candidates(
        registry_document, incoming, run_id="commons-search-2"
    )
    assert repeated["updated"] == 0
    assert repeated["unchanged"] == 5
    assert len(incoming["observations"]) == 5


def test_canonical_photo_decision_suppresses_new_run_and_keeps_source_provenance() -> None:
    registry_document = _registry()
    incoming = _commons_incoming()
    queue.materialize_photo_candidates(registry_document, incoming, run_id="commons-search-1")
    removed = next(item for item in incoming["observations"] if item["registry_id"] == REGISTRY_ID)
    _record_commons_photo_decision(registry_document, removed)
    incoming["observations"].remove(removed)

    first_run = incoming["source_runs"][0]
    source_candidate = next(
        candidate
        for candidate in first_run["outcome_details"]["photo_candidates"]
        if candidate["candidate_id"] == removed["normalized_value"]["candidate_id"]
    )
    second_candidate = copy.deepcopy(source_candidate)
    second_candidate["observed_at_utc"] = "2026-11-27T11:26:45Z"
    second_candidate["evidence_digest_sha256"] = "c" * 64
    second_run = copy.deepcopy(first_run)
    second_run["run_id"] = "commons-search-2"
    second_run["requested_at_utc"] = "2026-11-27T11:26:45Z"
    second_run["response_digest_sha256"] = "c" * 64
    second_run["coverage"] = {
        "requested": 1,
        "obtained": 1,
        "matched": 1,
        "new": 0,
        "ambiguous": 0,
        "unavailable": 0,
        "denied": 0,
    }
    second_run["outcome_details"] = {"photo_candidates": [second_candidate]}
    incoming["source_runs"].append(second_run)

    result = queue.materialize_photo_candidates(
        registry_document, incoming, run_id="commons-search-2"
    )

    assert result["already_materialized"] == 1
    assert result["added"] == 0
    assert result["restored"] == 0
    assert len(incoming["observations"]) == 4
    stored_first = incoming["source_runs"][0]
    stored_second = incoming["source_runs"][1]
    assert (
        stored_first["outcome_details"]["photo_candidates"][0]["evidence_digest_sha256"] == "b" * 64
    )
    assert stored_second["response_digest_sha256"] == "c" * 64
    assert (
        stored_second["outcome_details"]["photo_candidates"][0]["evidence_digest_sha256"]
        == "c" * 64
    )
    assert stored_second["outcome_details"]["photo_observation_ids"] == []
    assert stored_second["outcome_details"]["photo_observations_written"] == 0
