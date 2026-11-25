#!/usr/bin/env python3
"""One command: the studio registry source file -> the updated public catalog.

The operator edits ``campaigns/berlin_recording_services/discovery/studios.json`` and runs this
tool. It builds the import manifest from that file, reads the exact Catalog state it would
change, prints the diff, and only then applies:

    validate -> plan manifest -> read Catalog -> diff -> apply -> report pending decisions
             -> approve (explicit) -> next publication (explicit) -> verify public read

Nothing here invents a human decision. Descriptions are approved in the registry, contacts are
validated there, and Catalog accepts an unchanged description only when its own earlier review
covers the same bytes and the same evidence. A description nobody reviewed stays a pending
decision that this tool reports instead of recording.

The confirmation is bound to the diff: ``--dry-run`` prints a plan digest, and ``--apply``
refuses to run unless ``--confirm-plan`` repeats that digest, so an apply cannot silently
include changes that appeared after the plan was reviewed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

try:  # imported as a package by tests
    from tools import registry_catalog_apply as apply_tool
    from tools import registry_catalog_manifest as manifest_tool
    from tools import studio_registry as registry
    from tools import wfs_address_points as wfs
except ImportError:  # direct script execution
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from tools import registry_catalog_apply as apply_tool
    from tools import registry_catalog_manifest as manifest_tool
    from tools import studio_registry as registry
    from tools import wfs_address_points as wfs


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CAMPAIGN = REPO_ROOT / "campaigns" / "berlin_recording_services"
DEFAULT_REGISTRY = DEFAULT_CAMPAIGN / "discovery" / "studios.json"
DEFAULT_MAPPING = DEFAULT_CAMPAIGN / "catalog_mapping.yaml"
# The WFS response cache is evidence, not source: it never enters the repository.
DEFAULT_WFS_CACHE = REPO_ROOT.parent / ".cache" / "berlin-wfs"

PLAN_SCHEMA = "registry-sync-plan-v1"

# Every axis a binding records; a difference means Catalog must be asked to import again.
BINDING_AXES = (
    "contentDigest",
    "evidenceDigest",
    "eligibilityDigest",
    "mappingRevision",
    "configurationRevisionId",
    "manifestDigest",
)


class SyncError(RuntimeError):
    """A refusal this tool states instead of guessing."""


def canonical_bytes(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def digest_of(document: Any) -> str:
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def read_token(*, token: str | None, token_file: pathlib.Path | None) -> str:
    if token and token_file:
        raise SyncError("pass either --token or --token-file, not both")
    if token:
        return token.strip()
    if token_file is not None:
        return token_file.read_text(encoding="utf-8").strip()
    from_env = os.environ.get("AGGREGATOR_CATALOG_TOKEN", "").strip()
    if from_env:
        return from_env
    raise SyncError("no Catalog token: pass --token-file, --token, or set AGGREGATOR_CATALOG_TOKEN")


def _retry_after_seconds(status: int, detail: str) -> float | None:
    """Catalog states its own pause; only that number is honoured, never a guess."""
    if status != 429:
        return None
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return None
    pause = (payload.get("context") or {}).get("retryAfterSeconds")
    if not isinstance(pause, int | float) or pause < 0:
        return None
    return float(pause)


class Client:
    """Minimal HTTP client for the Catalog command API and the Query read API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 60.0,
        attempts: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.attempts = attempts

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = hashlib.sha256(data).hexdigest()
        request = urllib.request.Request(  # noqa: S310
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                payload = response.read()
                return None if not payload else json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            pause = _retry_after_seconds(error.code, detail)
            if pause is not None and self.attempts > 1:
                time.sleep(pause)
                self.attempts -= 1
                return self.call(method, path, body)
            raise SyncError(f"{method} {path} failed: {error.code} {detail}") from error
        except urllib.error.URLError as error:
            raise SyncError(f"{self.base_url} is unreachable: {error.reason}") from error

    def get(self, path: str) -> Any:
        return self.call("GET", path)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.call("POST", path, body)


def build_manifest(
    registry_path: pathlib.Path,
    mapping_path: pathlib.Path,
    wfs_cache: pathlib.Path | None,
) -> dict[str, Any]:
    """Project the source file exactly as the import boundary expects it."""
    document, problems = registry._load_registry(registry_path)
    if problems:
        rendered = "\n".join(problem.render() for problem in problems)
        raise SyncError(f"registry has {len(problems)} validation problem(s):\n{rendered}")
    mapping = manifest_tool.load_mapping(mapping_path)
    lookup = wfs.AddressPointLookup(require_external_cache(wfs_cache)) if wfs_cache else None
    return manifest_tool.build_manifest(
        document,
        registry_path.read_bytes(),
        mapping,
        recorded_at_utc=None,
        address_point_lookup=lookup,
    )


def require_external_cache(cache_dir: pathlib.Path) -> pathlib.Path:
    """The WFS cache is raw evidence, so it must live outside the repository."""
    resolved = cache_dir.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise SyncError(
            f"WFS cache '{resolved}' is inside the repository; pass a path outside {REPO_ROOT}"
        )
    return resolved


def read_state(client: Client, catalog_key: str) -> dict[str, Any]:
    base = f"/api/catalog-command/catalogs/{catalog_key}"
    bindings = client.get(f"{base}/registry-imports") or []
    listings = client.get(f"{base}/listings") or []
    publication = client.get(f"{base}/publication-status")
    return {
        "bindings": {binding["registryId"].lower(): binding for binding in bindings},
        "listings": {listing["id"]: listing for listing in listings},
        "publication": publication,
    }


def expected_binding(record: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, str]:
    """The binding values this exact record and manifest would install."""
    catalog = manifest["catalog"]
    return {
        "contentDigest": record["digests"]["content"],
        "evidenceDigest": record["digests"]["evidence"],
        "eligibilityDigest": record["digests"]["eligibility"],
        "mappingRevision": catalog["mappingRevision"],
        "configurationRevisionId": catalog["configurationRevisionId"],
        "manifestDigest": record["manifestDigest"],
    }


def record_changes(
    record: Mapping[str, Any], binding: Mapping[str, Any] | None, manifest: Mapping[str, Any]
) -> list[str]:
    """Which recorded axes the source moved since the last import, in a stable order."""
    if binding is None:
        return ["new_record"]
    expected = expected_binding(record, manifest)
    return [axis for axis in BINDING_AXES if str(binding.get(axis) or "") != expected[axis]]


def compute_plan(
    manifest: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    only: Sequence[str],
    exclusions: Sequence[str],
) -> dict[str, Any]:
    catalog_key = manifest["catalog"]["catalogKey"]
    wanted = {value.lower() for value in only}
    records = [
        record
        for record in manifest["records"]
        if not wanted or record["registryId"].lower() in wanted
    ]
    missing = wanted - {record["registryId"].lower() for record in records}
    if missing:
        raise SyncError(f"manifest has no record for {sorted(missing)}")

    bindings = state["bindings"]
    listings = state["listings"]
    publication = state["publication"]
    entries = {entry["listingId"]: entry["listingRevisionId"] for entry in publication["entries"]}

    pending: list[dict[str, Any]] = []
    for record in records:
        registry_id = record["registryId"]
        binding = bindings.get(registry_id.lower())
        changes = record_changes(record, binding, manifest)
        if not changes:
            continue
        if record["status"] == manifest_tool.STATUS_BLOCKED:
            pending.append(
                {
                    "registryId": registry_id,
                    "reason": "producer_status_blocked",
                    "detail": record["statusReasons"],
                }
            )
            continue
        listing = listings.get(binding["listingId"]) if binding else None
        pending.append(
            {
                "registryId": registry_id,
                "listingId": binding["listingId"] if binding else None,
                "changedAxes": changes,
                "status": record["status"],
                "statusReasons": record["statusReasons"],
                "richReasons": record.get("richReasons") or [],
                "needsApproval": listing is None
                or listing["currentDraftRevisionId"] != listing["approvedRevisionId"],
            }
        )

    excluded_listing_ids = set()
    for value in exclusions:
        key = value.lower()
        if key in bindings:
            excluded_listing_ids.add(bindings[key]["listingId"])
        elif value in listings:
            excluded_listing_ids.add(value)
        else:
            raise SyncError(f"cannot exclude '{value}': no binding or listing matches it")

    # Publication follows the source: a record the registry no longer qualifies cannot stay visible
    # just because Catalog keeps an approved revision for it. The withdrawal is recomputed on every
    # run, so it repeats until the registry cause is gone and never silently reverts.
    records_by_listing = {
        bindings[key]["listingId"]: record
        for key, record in (
            (record["registryId"].lower(), record) for record in manifest["records"]
        )
        if key in bindings
    }
    withdrawals = sorted(
        (
            {
                "listingId": listing_id,
                "registryId": records_by_listing[listing_id]["registryId"],
                "reasons": records_by_listing[listing_id]["statusReasons"],
            }
            for listing_id in entries
            if listing_id in records_by_listing
            and records_by_listing[listing_id]["status"] != manifest_tool.STATUS_ELIGIBLE
        ),
        key=lambda item: item["listingId"],
    )
    withdrawn_listing_ids = {item["listingId"] for item in withdrawals}
    bound_and_eligible = {
        records_by_listing[listing_id]["registryId"].lower(): listing_id
        for listing_id in records_by_listing
        if records_by_listing[listing_id]["status"] == manifest_tool.STATUS_ELIGIBLE
    }
    for listing_id in sorted(excluded_listing_ids - withdrawn_listing_ids):
        if listings.get(listing_id, {}).get("state") == "archived":
            continue
        if listing_id in records_by_listing:
            raise SyncError(
                f"cannot exclude {listing_id}: its registry record is still eligible, so the next "
                "sync would publish it again; record the reason in the registry instead"
            )

    archived_members = [
        listing_id
        for listing_id in entries
        if listings.get(listing_id, {}).get("state") == "archived"
    ]
    unresolved_archived = [value for value in archived_members if value not in excluded_listing_ids]
    if unresolved_archived:
        raise SyncError(
            "the current publication still carries archived listings "
            f"{sorted(unresolved_archived)}; pass --exclude for each of them"
        )

    # An approved revision that is not public yet is the exact content a next publication would
    # add. Only an eligible record reaches this list: an approval in Catalog never overrides the
    # source's own restriction.
    additions = sorted(
        (
            {
                "listingId": listing["id"],
                "listingRevisionId": listing["approvedRevisionId"],
            }
            for listing in listings.values()
            if listing["state"] != "archived"
            and listing["approvedRevisionId"] is not None
            and entries.get(listing["id"]) != listing["approvedRevisionId"]
            and listing["id"] in bound_and_eligible.values()
        ),
        key=lambda item: item["listingId"],
    )
    plan = {
        "schemaVersion": PLAN_SCHEMA,
        "catalogKey": catalog_key,
        "manifestDigest": manifest["manifestDigest"],
        "currentPublicationId": publication["currentPublicationId"],
        "currentPublicationSequence": publication["currentPublicationSequence"],
        "publicationEntryCount": len(entries),
        "records": sorted(pending, key=lambda item: item["registryId"]),
        "publicationAdditions": additions,
        "withdrawals": withdrawals,
        "exclusions": sorted(excluded_listing_ids | withdrawn_listing_ids),
    }
    plan["planDigest"] = digest_of(
        {key: value for key, value in plan.items() if key not in ("planDigest",)}
    )
    return plan


def plan_lines(plan: Mapping[str, Any]) -> Iterable[str]:
    yield (
        f"catalog {plan['catalogKey']} publication {plan['currentPublicationId']} "
        f"sequence {plan['currentPublicationSequence']} entries {plan['publicationEntryCount']}"
    )
    yield f"manifest {plan['manifestDigest']}"
    if not plan["records"]:
        yield "records: nothing to import; the source matches the installed bindings"
    for record in plan["records"]:
        if record.get("reason"):
            yield (
                f"record {record['registryId']}: blocked by {record['reason']} "
                f"({';'.join(record['detail'])})"
            )
            continue
        axes = ",".join(record["changedAxes"])
        extra = ""
        if record.get("status") != manifest_tool.STATUS_ELIGIBLE:
            extra = f" status={record['status']} reasons={';'.join(record['statusReasons'])}"
        if record.get("richReasons"):
            extra += f" rich={';'.join(record['richReasons'])}"
        yield f"record {record['registryId']}: {axes}{extra}"
    for item in plan["publicationAdditions"]:
        yield f"publication adds {item['listingId']} at revision {item['listingRevisionId']}"
    for item in plan["withdrawals"]:
        yield (
            f"publication withdraws {item['listingId']} ({item['registryId']}): "
            f"{';'.join(item['reasons'])}"
        )
    if plan["exclusions"]:
        yield f"exclusions: {','.join(plan['exclusions'])}"
    yield f"plan digest {plan['planDigest']}"


def apply_manifest(
    manifest: Mapping[str, Any], client: Client, plan: Mapping[str, Any]
) -> dict[str, Any]:
    """Send every planned record; Catalog alone decides whether it is unchanged."""
    catalog_key = manifest["catalog"]["catalogKey"]
    transport = apply_tool.http_transport(client.base_url, client.token)
    planned = [record["registryId"].lower() for record in plan["records"]]
    records = [record for record in manifest["records"] if record["registryId"].lower() in planned]
    tally = apply_tool.run(
        dict(manifest),
        records,
        transport,
        checkpoint=None,
        checkpoint_path=None,
        require_unchanged=False,
    )
    return {"catalogKey": catalog_key, **tally}


def _failure_code(reason: str) -> str:
    """The owner's own code, so a report names a cause instead of repeating prose."""
    start = reason.find("{")
    if start < 0:
        return "unclassified"
    try:
        payload = json.loads(reason[start:])
    except json.JSONDecodeError:
        return "unclassified"
    return str(payload.get("code") or "unclassified")


def pending_summary(pending: Sequence[Mapping[str, Any]]) -> list[str]:
    """Group the drafts Catalog refused by cause: each cause is one operator decision."""
    counts: dict[str, int] = {}
    for item in pending:
        counts[str(item["code"])] = counts.get(str(item["code"]), 0) + 1
    return [f"{count} draft(s) left pending: {code}" for code, count in sorted(counts.items())]


def approve_ready(client: Client, catalog_key: str) -> dict[str, Any]:
    """Approve exactly the drafts Catalog accepts; report the ones only a human can clear."""
    base = f"/api/catalog-command/catalogs/{catalog_key}"
    listings = client.get(f"{base}/listings") or []
    approved: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for listing in listings:
        draft = listing["currentDraftRevisionId"]
        if draft is None or draft == listing["approvedRevisionId"]:
            continue
        body = {"expectedVersion": listing["version"], "revisionId": draft}
        try:
            decision = client.post(
                f"/api/catalog-command/listings/{listing['id']}/approval-decisions/approve", body
            )
        except SyncError as error:
            pending.append(
                {
                    "listingId": listing["id"],
                    "revisionId": draft,
                    "code": _failure_code(str(error)),
                    "reason": str(error),
                }
            )
            continue
        approved.append({"listingId": listing["id"], "revisionId": draft, "decision": decision})
    return {"approved": approved, "pending": pending}


def publish_next(client: Client, catalog_key: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Queue the current publication plus the planned additions, minus the planned exclusions."""
    base = f"/api/catalog-command/catalogs/{catalog_key}"
    publication = client.get(f"{base}/publication-status")
    entries = {entry["listingId"]: entry["listingRevisionId"] for entry in publication["entries"]}
    listings = client.get(f"{base}/listings") or []
    planned = {
        (item["listingId"], item["listingRevisionId"]) for item in plan["publicationAdditions"]
    }
    updates = []
    for listing in listings:
        revision = listing["approvedRevisionId"]
        if revision is None:
            continue
        if entries.get(listing["id"]) == revision:
            continue
        if (listing["id"], revision) not in planned:
            continue
        updates.append(
            {
                "listingId": listing["id"],
                "listingRevisionId": revision,
                "expectedListingVersion": listing["version"],
                "carryForward": False,
            }
        )
    addition = {(item["listingId"], item["listingRevisionId"]) for item in updates}
    if addition != planned:
        raise SyncError(
            "the approved revisions changed after the confirmed plan: "
            f"planned {len(planned)} addition(s), now {len(addition)}; run --dry-run again"
        )
    excluded = list(plan["exclusions"])
    if not updates and not excluded:
        return {"queued": False, "reason": "no approved revision is missing from the publication"}
    expected_current = (
        {"kind": "absent", "publicationId": None}
        if publication["currentPublicationId"] is None
        else {"kind": "exact", "publicationId": publication["currentPublicationId"]}
    )
    request = {
        "catalogKey": catalog_key,
        "configurationRevisionId": publication["configurationRevisionId"],
        "expectedCurrent": expected_current,
        "updates": updates,
        "exclusions": excluded,
    }
    operation = client.post(f"{base}/publication-requests/next", request)
    return {"queued": True, "request": request, "operation": operation}


def wait_operation(client: Client, operation_id: str, timeout: float) -> dict[str, Any]:
    """Wait for the durable publication operation to reach a terminal state."""
    deadline = time.monotonic() + timeout
    while True:
        operation = client.get(f"/api/catalog-command/operations/{operation_id}")
        state = str(operation.get("state") or "").lower()
        if state in ("completed", "failed"):
            return cast(dict[str, Any], operation)
        if time.monotonic() >= deadline:
            raise SyncError(
                f"publication operation {operation_id} is still "
                f"{state or 'unknown'} after {timeout}s"
            )
        time.sleep(1.0)


def verify_public_read(
    client: Client, catalog_key: str, publication_id: str, timeout: float
) -> dict[str, Any]:
    """The public read must name the publication this run activated."""
    path = f"/api/catalog-query/catalogs/{catalog_key}/projection-status"
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while True:
        last = client.get(path)
        metadata = (last.get("publicRead") or {}).get("metadata") or {}
        if metadata.get("sourcePublicationId") == publication_id:
            return last
        if time.monotonic() >= deadline:
            raise SyncError(
                "the public projection does not read publication "
                f"{publication_id} within {timeout}s: state={last.get('state')} "
                f"code={last.get('code')} "
                f"source={(metadata or {}).get('sourcePublicationId')}"
            )
        time.sleep(1.0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", type=pathlib.Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--mapping", type=pathlib.Path, default=DEFAULT_MAPPING)
    parser.add_argument("--wfs-cache", type=pathlib.Path, default=DEFAULT_WFS_CACHE)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--query-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--token-file", type=pathlib.Path, default=None)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-plan", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--plan-out", type=pathlib.Path, default=None)
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)

    manifest = build_manifest(args.registry, args.mapping, args.wfs_cache)
    if manifest.get("isolatedRecords"):
        for item in manifest["isolatedRecords"]:
            print(f"isolated record {item['registryId']}: {item['error']}", file=sys.stderr)

    needs_write = args.apply or args.approve or args.publish
    if args.dry_run and needs_write:
        raise SyncError("--dry-run cannot be combined with --apply, --approve or --publish")
    if not needs_write and not args.dry_run:
        raise SyncError("choose --dry-run, or pass --apply (and optionally --approve/--publish)")

    token = read_token(token=args.token, token_file=args.token_file)
    client = Client(args.base_url, token)
    state = read_state(client, manifest["catalog"]["catalogKey"])
    plan = compute_plan(manifest, state, only=args.only, exclusions=args.exclude)
    for line in plan_lines(plan):
        print(line)
    if args.plan_out is not None:
        args.plan_out.parent.mkdir(parents=True, exist_ok=True)
        args.plan_out.write_text(
            json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"plan written: {args.plan_out}")
    if args.dry_run:
        return 0

    appliable = [record for record in plan["records"] if not record.get("reason")]
    if args.publish and not args.apply and appliable:
        raise SyncError(
            "the source still has records Catalog has not accepted; run --apply before --publish"
        )

    if args.apply and appliable and args.confirm_plan is None:
        raise SyncError(
            f"an apply needs --confirm-plan {plan['planDigest']} from the dry run that was reviewed"
        )
    if args.confirm_plan is not None and args.confirm_plan != plan["planDigest"]:
        raise SyncError(
            "the catalog state changed after the reviewed plan: expected plan digest "
            f"{args.confirm_plan}, current {plan['planDigest']}; run --dry-run again"
        )

    if args.apply and plan["records"]:
        report = apply_manifest(manifest, client, plan)
        print(json.dumps({"apply": report}, indent=2, sort_keys=True, ensure_ascii=False))
        if report["failed"]:
            raise SyncError(
                f"{len(report['failed'])} record(s) were refused by Catalog; nothing was published"
            )

    if args.approve:
        approval = approve_ready(client, plan["catalogKey"])
        print(json.dumps({"approve": approval}, indent=2, sort_keys=True, ensure_ascii=False))
        for line in pending_summary(approval["pending"]):
            # A refused draft stays out of the next publication instead of blocking the others.
            print(line, file=sys.stderr)

    if args.publish:
        publication = publish_next(client, plan["catalogKey"], plan)
        print(json.dumps({"publish": publication}, indent=2, sort_keys=True, ensure_ascii=False))
        if not publication["queued"]:
            return 0
        operation_id = publication["operation"]["operationId"]
        operation = wait_operation(client, operation_id, args.wait_seconds)
        print(json.dumps({"operation": operation}, indent=2, sort_keys=True, ensure_ascii=False))
        if str(operation.get("state") or "").lower() != "completed":
            raise SyncError(f"publication operation {operation_id} did not complete")
        publication_id = operation["publicationId"]
        status = verify_public_read(
            Client(args.query_url or args.base_url, token),
            plan["catalogKey"],
            publication_id,
            args.wait_seconds,
        )
        print(
            json.dumps(
                {
                    "publicRead": {
                        "state": status["state"],
                        "code": status["code"],
                        "sourcePublicationId": (status.get("publicRead") or {})
                        .get("metadata", {})
                        .get("sourcePublicationId"),
                    }
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SyncError as failure:
        print(f"error: {failure}", file=sys.stderr)
        sys.exit(2)
