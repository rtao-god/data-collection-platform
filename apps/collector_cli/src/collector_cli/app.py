from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from collection_application import CampaignSnapshotService
from collection_contracts import OwnerContextError
from collection_infrastructure import FilesystemCampaignBundleSource


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collector")
    parser.add_argument(
        "--campaigns-root",
        type=Path,
        default=Path("campaigns"),
        help="Allowlisted campaign authoring root.",
    )
    command = parser.add_subparsers(dest="command", required=True)
    config = command.add_parser("config", help="Validate and identify campaign bundles.")
    config_command = config.add_subparsers(dest="config_command", required=True)
    for name in ("validate", "digest"):
        operation = config_command.add_parser(name)
        operation.add_argument("campaign_key")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    correlation_id = str(uuid4())
    try:
        source = FilesystemCampaignBundleSource(args.campaigns_root)
        snapshot = CampaignSnapshotService(source).create(args.campaign_key, correlation_id)
        if args.config_command == "digest":
            payload: dict[str, object] = {
                "campaignKey": snapshot.campaign_key,
                "bundleDigest": snapshot.bundle_digest,
                "readiness": snapshot.readiness,
                "blockers": [
                    blocker.model_dump(mode="json", by_alias=True) for blocker in snapshot.blockers
                ],
            }
        else:
            payload = snapshot.canonical_output()
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except OwnerContextError as exc:
        print(
            json.dumps(
                exc.envelope.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError) as exc:
        payload = {
            "type": "collection/cli-bootstrap-failed",
            "owner": "CollectorCli",
            "code": "CLI_BOOTSTRAP_FAILED",
            "message": "Collector CLI could not initialize its local dependencies.",
            "context": {"detail": str(exc)},
            "requiredAction": "Correct the local path or environment and run the command again.",
            "correlationId": correlation_id,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
        return 3


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
