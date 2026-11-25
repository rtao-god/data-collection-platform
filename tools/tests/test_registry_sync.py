"""The single sync command: one source file, one exact diff, one confirmed apply.

These tests use fabricated Catalog state: they prove the plan/diff/confirmation rules, not
that the live Catalog accepts the requests.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import registry_sync as sync

CATALOG_KEY = "berlin-recording-services"
REGISTRY_ID = "027a1f22-6b8e-5dc7-bbaf-f73c30fddabf"
LISTING_ID = "0a3a7c1e-0d6e-5b0f-9a0f-5d2f9b0a1c11"
MAPPING_REVISION = "8f7ff1e7ca2939719f5d21964d232cf506fd821af11ce91365db1baef542d5b0"
CONFIGURATION_REVISION_ID = "46fb742c-f4d9-45dd-99ee-a364cf44b3c2"


def manifest(*records: dict, digest: str = "m" * 64) -> dict:
    return {
        "schemaRevision": "registry-catalog-import-v1",
        "manifestDigest": digest,
        "catalog": {
            "catalogKey": CATALOG_KEY,
            "mappingRevision": MAPPING_REVISION,
            "configurationRevisionId": CONFIGURATION_REVISION_ID,
        },
        "records": list(records),
    }


def record(**overrides) -> dict:
    value = {
        "registryId": REGISTRY_ID,
        "status": "eligible",
        "statusReasons": [],
        "richReasons": [],
        "digests": {
            "content": "c" * 64,
            "evidence": "e" * 64,
            "eligibility": "l" * 64,
        },
        "manifestDigest": "m" * 64,
    }
    value.update(overrides)
    return value


def binding(**overrides) -> dict:
    value = {
        "registryId": REGISTRY_ID,
        "listingId": LISTING_ID,
        "contentDigest": "c" * 64,
        "evidenceDigest": "e" * 64,
        "eligibilityDigest": "l" * 64,
        "mappingRevision": MAPPING_REVISION,
        "configurationRevisionId": CONFIGURATION_REVISION_ID,
        "manifestDigest": "m" * 64,
    }
    value.update(overrides)
    return value


def listing(*, approved: str | None = None, draft: str | None = None, state: str = "draft") -> dict:
    return {
        "id": LISTING_ID,
        "state": state,
        "version": 3,
        "currentDraftRevisionId": draft,
        "approvedRevisionId": approved,
        "publishedRevisionId": None,
    }


def state(
    *,
    bindings: list[dict] | None = None,
    listings: list[dict] | None = None,
    entries: list[tuple[str, str]] | None = None,
    publication_id: str | None = "1" * 8,
) -> dict:
    return {
        "bindings": {item["registryId"].lower(): item for item in bindings or []},
        "listings": {item["id"]: item for item in listings or []},
        "publication": {
            "catalogKey": CATALOG_KEY,
            "currentPublicationId": publication_id,
            "currentPublicationSequence": 30,
            "configurationRevisionId": "46fb742c-f4d9-45dd-99ee-a364cf44b3c2",
            "entries": [
                {"listingId": item[0], "listingRevisionId": item[1]} for item in entries or []
            ],
        },
    }


def _plan(manifest_value: dict, state_value: dict, **kwargs):
    return sync.compute_plan(manifest_value, state_value, only=[], exclusions=[], **kwargs)


def test_unchanged_source_produces_an_empty_plan() -> None:
    source = manifest(record())
    installed = state(
        bindings=[binding()],
        listings=[listing(approved="a" * 8, draft="a" * 8, state="published")],
        entries=[(LISTING_ID, "a" * 8)],
    )

    first = _plan(source, installed)
    second = _plan(source, installed)

    assert first["records"] == []
    assert first["publicationAdditions"] == []
    # A rerun of unchanged inputs is byte-identical, so nothing can be queued twice.
    assert first["planDigest"] == second["planDigest"]
    assert list(sync.plan_lines(first))[2] == (
        "records: nothing to import; the source matches the installed bindings"
    )


def test_one_changed_phone_is_one_record_in_the_diff() -> None:
    """A changed contact moves only the content axis of the record that owns it."""
    source = manifest(
        record(digests={"content": "f" * 64, "evidence": "e" * 64, "eligibility": "l" * 64})
    )
    installed = state(bindings=[binding()], listings=[listing(draft="a" * 8)])

    plan = _plan(source, installed)

    assert [item["registryId"] for item in plan["records"]] == [REGISTRY_ID]
    assert plan["records"][0]["changedAxes"] == ["contentDigest"]
    assert plan["records"][0]["listingId"] == LISTING_ID


def test_postponed_description_is_reported_as_a_pending_decision() -> None:
    source = manifest(
        record(
            digests={"content": "f" * 64, "evidence": "e" * 64, "eligibility": "l" * 64},
            richReasons=["description_draft:de-DE"],
        )
    )
    installed = state(bindings=[binding()], listings=[listing(draft="a" * 8)])

    plan = _plan(source, installed)

    assert plan["records"][0]["richReasons"] == ["description_draft:de-DE"]
    assert any("rich=description_draft:de-DE" in line for line in sync.plan_lines(plan))


def test_a_different_diff_has_a_different_plan_digest() -> None:
    installed = state(bindings=[binding()], listings=[listing(draft="a" * 8)])
    left = _plan(
        manifest(
            record(digests={"content": "f" * 64, "evidence": "e" * 64, "eligibility": "l" * 64})
        ),
        installed,
    )
    right = _plan(
        manifest(
            record(digests={"content": "a" * 64, "evidence": "e" * 64, "eligibility": "s" * 64})
        ),
        installed,
    )

    assert left["planDigest"] != right["planDigest"]


def test_archived_member_needs_an_explicit_exclusion() -> None:
    source = manifest(record())
    installed = state(
        bindings=[binding()],
        listings=[
            listing(approved="a" * 8, draft="a" * 8, state="archived"),
        ],
        entries=[(LISTING_ID, "a" * 8)],
    )

    with pytest.raises(sync.SyncError, match="archived listings"):
        _plan(source, installed)

    plan = sync.compute_plan(source, installed, only=[], exclusions=[LISTING_ID])
    assert plan["exclusions"] == [LISTING_ID]


def test_approved_revision_missing_from_the_publication_is_listed() -> None:
    installed = state(
        bindings=[binding(contentDigest="c" * 64)],
        listings=[listing(approved="b" * 8, draft="b" * 8, state="approved")],
        entries=[],
    )

    plan = _plan(manifest(record()), installed)

    assert plan["publicationAdditions"] == [{"listingId": LISTING_ID, "listingRevisionId": "b" * 8}]


def test_an_approved_but_ineligible_record_is_never_published() -> None:
    """Catalog approval is not a publication right: the source verdict decides."""
    source = manifest(
        record(status="blocked", statusReasons=["restriction:studio_use_not_confirmed"])
    )
    installed = state(
        bindings=[binding()],
        listings=[listing(approved="b" * 8, draft="b" * 8, state="approved")],
        entries=[],
    )

    plan = _plan(source, installed)

    assert plan["publicationAdditions"] == []


def test_a_published_card_whose_record_lost_eligibility_is_withdrawn() -> None:
    source = manifest(
        record(status="blocked", statusReasons=["restriction:duplicate_of_published_record"])
    )
    installed = state(
        bindings=[binding()],
        listings=[listing(approved="a" * 8, draft="a" * 8, state="published")],
        entries=[(LISTING_ID, "a" * 8)],
    )

    plan = _plan(source, installed)

    assert plan["withdrawals"] == [
        {
            "listingId": LISTING_ID,
            "registryId": REGISTRY_ID,
            "reasons": ["restriction:duplicate_of_published_record"],
        }
    ]
    assert plan["exclusions"] == [LISTING_ID]
    assert any("publication withdraws" in line for line in sync.plan_lines(plan))
    # The withdrawal is part of the reviewed diff, so confirming a plan authorizes exactly it.
    reinstated = manifest(record())
    assert _plan(reinstated, installed)["planDigest"] != plan["planDigest"]


def test_an_exclusion_without_a_registry_cause_is_refused() -> None:
    """A card cannot be hidden by a one-off flag the next sync would silently undo."""
    source = manifest(record())
    installed = state(
        bindings=[binding()],
        listings=[listing(approved="a" * 8, draft="a" * 8, state="published")],
        entries=[(LISTING_ID, "a" * 8)],
    )

    with pytest.raises(sync.SyncError, match="still eligible"):
        sync.compute_plan(source, installed, only=[], exclusions=[LISTING_ID])


def test_wfs_cache_inside_the_repository_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(sync.SyncError, match="inside the repository"):
        sync.require_external_cache(sync.REPO_ROOT / "campaigns" / "wfs")


def test_wfs_cache_outside_the_repository_is_accepted(tmp_path: pathlib.Path) -> None:
    assert sync.require_external_cache(tmp_path) == tmp_path.resolve()


def _drive(monkeypatch, argv: list[str], *, installed: dict, source: dict) -> dict:
    """Run main() with the network boundary replaced by recorded decisions."""
    calls: dict[str, list] = {"apply": [], "approve": []}
    monkeypatch.setattr(sync, "build_manifest", lambda *args, **kwargs: source)
    monkeypatch.setattr(sync, "read_token", lambda **kwargs: "token")
    monkeypatch.setattr(sync, "Client", lambda *args, **kwargs: object())
    monkeypatch.setattr(sync, "read_state", lambda client, catalog_key: installed)

    def apply_records(manifest_value, client, plan):
        calls["apply"].append(plan["planDigest"])
        return {"failed": [], "changed": 1, "sent": 1, "unchanged": 0, "total": 1}

    monkeypatch.setattr(sync, "apply_manifest", apply_records)
    monkeypatch.setattr(
        sync,
        "approve_ready",
        lambda client, catalog_key: (
            calls["approve"].append(catalog_key) or {"approved": [], "pending": []}
        ),
    )
    sync.main(argv)
    return calls


def test_approve_alone_never_sends_records(monkeypatch) -> None:
    installed = state(
        bindings=[binding(contentDigest="f" * 64)],
        listings=[listing(approved="a" * 8, draft="b" * 8, state="published")],
    )

    calls = _drive(monkeypatch, ["--approve"], installed=installed, source=manifest(record()))

    assert calls["apply"] == []
    assert calls["approve"] == [CATALOG_KEY]


def test_publish_is_refused_while_the_source_still_has_unapplied_records(monkeypatch) -> None:
    installed = state(
        bindings=[binding(contentDigest="f" * 64)],
        listings=[listing(approved="a" * 8, draft="b" * 8, state="published")],
    )

    with pytest.raises(sync.SyncError, match="run --apply before --publish"):
        _drive(monkeypatch, ["--publish"], installed=installed, source=manifest(record()))


def test_rate_limit_states_its_own_pause() -> None:
    detail = json.dumps({"context": {"retryAfterSeconds": 7}})

    assert sync._retry_after_seconds(429, detail) == 7.0


def test_other_refusals_are_not_retried() -> None:
    assert sync._retry_after_seconds(409, "{}") is None
    assert sync._retry_after_seconds(429, "not json") is None


def test_a_refused_draft_is_grouped_by_the_owner_code() -> None:
    pending = [
        {"listingId": "a", "code": "CATALOG_INVARIANT_VIOLATION", "reason": ""},
        {"listingId": "b", "code": "CATALOG_INVARIANT_VIOLATION", "reason": ""},
        {"listingId": "c", "code": "CATALOG_CONFLICT", "reason": ""},
    ]

    assert sync.pending_summary(pending) == [
        "1 draft(s) left pending: CATALOG_CONFLICT",
        "2 draft(s) left pending: CATALOG_INVARIANT_VIOLATION",
    ]


def test_the_failure_code_comes_from_the_owner_envelope() -> None:
    reason = 'POST /x failed: 422 {"code":"CATALOG_INVARIANT_VIOLATION","detail":"x"}'

    assert sync._failure_code(reason) == "CATALOG_INVARIANT_VIOLATION"
    assert sync._failure_code("plain text") == "unclassified"
