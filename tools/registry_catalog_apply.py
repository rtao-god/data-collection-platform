#!/usr/bin/env python3
"""Send a sealed registry manifest to the Catalog import boundary, one record at a time.

This tool owns no business state. It maps manifest records onto the published
``registry-catalog-import-v1`` wire contract, sends them to Catalog's own command
endpoint, and keeps a local checkpoint purely so an interrupted run can resume
without re-sending what the owner already applied. Catalog's binding table stays
the source of truth: ``verify`` re-sends every record and requires the owner to
answer ``unchanged``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

SCHEMA_REVISION = "registry-catalog-import-v1"
CHECKPOINT_SCHEMA = "registry-catalog-apply-checkpoint-v1"

# The wire enums are camelCase contracts; a mismatch is a producer defect, not a fallback.
STATUS = {"eligible": "eligible", "review": "review", "blocked": "blocked"}
DECISION = {"create_new": "createNew", "link_existing": "linkExisting"}
ACTION_CHANGED = {"created", "linked", "updated", "reviewRequired"}

Transport = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class ApplyError(RuntimeError):
    """The Catalog boundary rejected a record; the message carries its own diagnostic."""


def load_manifest(path: pathlib.Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schemaRevision") != SCHEMA_REVISION:
        raise ApplyError(f"unsupported manifest schema {manifest.get('schemaRevision')!r}")
    if not isinstance(manifest.get("records"), list):
        raise ApplyError("manifest carries no records")
    return manifest


def build_request(manifest: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Map one manifest record onto the Catalog apply request without inventing facts."""
    catalog = manifest["catalog"]
    status = record["status"]
    if status not in STATUS:
        raise ApplyError(f"unknown record status {status!r}")
    decision = record["identity"]["decision"]
    if decision not in DECISION:
        raise ApplyError(f"unknown identity decision {decision!r}")

    business = record.get("business")
    address = None
    if business is not None:
        source = business["address"]
        address = {
            "street": source.get("street"),
            "streetNumber": source.get("streetNumber"),
            "postalCode": source.get("postalCode"),
            "city": source.get("city"),
            "countryCode": source.get("countryCode"),
            "districtKey": source["districtKey"],
            "districtBasis": source["districtBasis"],
            "assertionId": source["assertionId"],
            "coordinate": None,
        }
        point = source.get("coordinate")
        if point is not None:
            address["coordinate"] = {
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "accuracyMeters": point["accuracyMeters"],
                "source": point["source"],
                "datasetIdentifier": point["datasetIdentifier"],
                "addressRecordId": point["addressRecordId"],
                "sourceReference": point["sourceReference"],
                "assertionId": point["assertionId"],
            }

    wire_business = None
    if business is not None:
        wire_business = {
            "name": business["name"],
            "nameLocale": business["nameLocale"],
            "nameAssertionId": business["nameAssertionId"],
            "address": address,
            "categories": [
                {"categoryKey": item["categoryKey"], "assertionId": item["assertionId"]}
                for item in business["categories"]
            ],
            "displayLocales": list(business["displayLocales"]),
        }

    return {
        "record": {
            "schemaRevision": SCHEMA_REVISION,
            "sourceSystem": manifest["sourceSystem"],
            "registryId": record["registryId"],
            "status": STATUS[status],
            "statusReasons": list(record["statusReasons"]),
            "identity": {
                "decision": DECISION[decision],
                "linkExistingListingId": record["identity"].get("linkExistingListingId"),
                "officialDomain": record["identity"].get("officialDomain"),
            },
            "business": wire_business,
            "assertions": [
                {
                    "assertionId": item["assertionId"],
                    "sourceKind": item["sourceKind"],
                    "sourceReference": item["sourceReference"],
                    "sourceExternalId": item.get("sourceExternalId"),
                    "observedAtUtc": item["observedAtUtc"],
                    "usagePolicy": item["usagePolicy"],
                    "evidenceDigest": item["evidenceDigest"],
                }
                for item in record["assertions"]
            ],
            "contentDigest": record["digests"]["content"],
            "evidenceDigest": record["digests"]["evidence"],
            "eligibilityDigest": record["digests"]["eligibility"],
            "manifestDigest": manifest["manifestDigest"],
            "mappingRevision": catalog["mappingRevision"],
            "configurationRevisionId": catalog["configurationRevisionId"],
        }
    }


def http_transport(base_url: str, token: str, timeout: float = 60.0) -> Transport:
    def send(catalog_key: str, _token: str, body: dict[str, Any]) -> dict[str, Any]:
        base = base_url.rstrip("/")
        url = f"{base}/api/catalog-command/catalogs/{catalog_key}/registry-imports"
        request = urllib.request.Request(  # noqa: S310
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": hashlib.sha256(
                    json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest(),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise ApplyError(
                f"Catalog rejected {body['record']['registryId']}: {error.code} {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise ApplyError(f"Catalog is unreachable at {base_url}: {error.reason}") from error

    return send


def read_checkpoint(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schemaVersion": CHECKPOINT_SCHEMA, "records": {}}
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    if checkpoint.get("schemaVersion") != CHECKPOINT_SCHEMA:
        raise ApplyError(f"unsupported checkpoint schema {checkpoint.get('schemaVersion')!r}")
    return checkpoint


def write_checkpoint(path: pathlib.Path, checkpoint: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(checkpoint, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def select_records(
    manifest: dict[str, Any], only: list[str], limit: int | None
) -> list[dict[str, Any]]:
    records = manifest["records"]
    if only:
        wanted = {value.lower() for value in only}
        records = [record for record in records if record["registryId"].lower() in wanted]
        missing = wanted - {record["registryId"].lower() for record in records}
        if missing:
            raise ApplyError(f"manifest has no record for {sorted(missing)}")
    return records[:limit] if limit is not None else records


def run(
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    send: Transport,
    checkpoint: dict[str, Any] | None,
    checkpoint_path: pathlib.Path | None,
    require_unchanged: bool,
) -> dict[str, Any]:
    """Apply records in manifest order and return the tallies this run actually observed."""
    catalog_key = manifest["catalog"]["catalogKey"]
    tally: dict[str, Any] = {
        "total": len(records),
        "actions": {},
        "resumed": 0,
        "applied": 0,
        "failed": [],
    }
    for record in records:
        registry_id = record["registryId"]
        if record["status"] == "blocked":
            # A blocked record is a producer verdict Catalog must refuse, so it is never sent.
            tally["actions"]["blockedNotSent"] = tally["actions"].get("blockedNotSent", 0) + 1
            continue
        if checkpoint is not None and not require_unchanged:
            previous = checkpoint["records"].get(registry_id)
            if previous and previous.get("status") == "applied":
                tally["resumed"] += 1
                continue
        try:
            response = send(catalog_key, "", build_request(manifest, record))
        except ApplyError as error:
            tally["failed"].append({"registryId": registry_id, "error": str(error)})
            if checkpoint is not None and checkpoint_path is not None:
                checkpoint["records"][registry_id] = {"status": "failed", "error": str(error)}
                write_checkpoint(checkpoint_path, checkpoint)
            continue
        action = response["lastAction"]
        if require_unchanged and action in ACTION_CHANGED:
            tally["failed"].append(
                {
                    "registryId": registry_id,
                    "error": f"replay reported {action}, expected unchanged",
                }
            )
        tally["actions"][action] = tally["actions"].get(action, 0) + 1
        tally["applied"] += 1
        if checkpoint is not None and checkpoint_path is not None:
            checkpoint["records"][registry_id] = {
                "status": "applied",
                "lastAction": action,
                "placeId": response["placeId"],
                "listingId": response.get("listingId"),
                "contentDigest": response["contentDigest"],
            }
            write_checkpoint(checkpoint_path, checkpoint)
    return tally


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["map", "apply", "verify"])
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument(
        "--only", action="append", default=[], help="registry id to include, repeatable"
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--token-file", type=pathlib.Path, default=None)
    parser.add_argument("--checkpoint", type=pathlib.Path, default=None)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    records = select_records(manifest, args.only, args.limit)
    if args.command == "map":
        requests = [build_request(manifest, value) for value in records]
        print(json.dumps(requests, indent=2, ensure_ascii=False))
        return 0

    token = args.token
    if token is None and args.token_file is not None:
        token = args.token_file.read_text(encoding="utf-8").strip()
    if not args.base_url or not token:
        raise ApplyError("apply and verify need --base-url and --token or --token-file")

    checkpoint = read_checkpoint(args.checkpoint) if args.checkpoint is not None else None
    tally = run(
        manifest,
        records,
        http_transport(args.base_url, token),
        checkpoint,
        args.checkpoint,
        args.command == "verify",
    )
    print(json.dumps(tally, indent=2, sort_keys=True))
    return 1 if tally["failed"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApplyError as failure:
        print(f"error: {failure}", file=sys.stderr)
        sys.exit(2)
