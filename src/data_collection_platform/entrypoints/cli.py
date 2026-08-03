"""Administrative command-line entrypoint for deterministic campaign artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from data_collection_platform.configuration.compiler import (
    CampaignConfigurationViolation,
    compile_campaign_directory,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="data-collection-platform")
    commands = parser.add_subparsers(dest="command", required=True)

    compile_campaign = commands.add_parser(
        "compile-campaign",
        help="Validate a campaign source directory and write one canonical bundle.",
    )
    compile_campaign.add_argument("campaign_root", type=Path)
    compile_campaign.add_argument("output", type=Path)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(arguments)

    if namespace.command != "compile-campaign":
        parser.error(f"Unsupported command: {namespace.command}")

    campaign_root = namespace.campaign_root
    output = namespace.output
    if not isinstance(campaign_root, Path) or not isinstance(output, Path):
        parser.error("compile-campaign paths were not parsed correctly")

    try:
        bundle = compile_campaign_directory(campaign_root)
        bundle.write_atomic(output)
    except CampaignConfigurationViolation as error:
        diagnostic = {
            "status": "error",
            "code": error.code,
            "message": error.message,
            "context": dict(error.context),
        }
        print(json.dumps(diagnostic, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2

    result = {
        "status": "compiled",
        "campaign_id": bundle.campaign_id,
        "bundle_sha256": bundle.bundle_sha256,
        "output": str(output),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
