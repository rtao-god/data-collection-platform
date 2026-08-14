from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel

from review_contracts import (
    CandidateRevision,
    ManualObservation,
    ReviewCase,
    ReviewDecision,
    ReviewDecisionCommand,
    SuppressionCommand,
    SuppressionRevision,
)

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "contracts/review"
MODELS: dict[str, type[BaseModel]] = {
    "candidate-revision.schema.json": CandidateRevision,
    "manual-observation.schema.json": ManualObservation,
    "review-case.schema.json": ReviewCase,
    "review-decision-command.schema.json": ReviewDecisionCommand,
    "review-decision.schema.json": ReviewDecision,
    "suppression-command.schema.json": SuppressionCommand,
    "suppression-revision.schema.json": SuppressionRevision,
}


def render() -> dict[str, str]:
    rendered = {
        name: json.dumps(
            model.model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
        for name, model in MODELS.items()
    }
    manifest = {
        "contract": "collector-review-contract-manifest",
        "contractRevision": "review-contract-manifest-v1",
        "files": {
            name: f"sha256:{sha256(content.encode('utf-8')).hexdigest()}"
            for name, content in sorted(rendered.items())
        },
    }
    rendered["manifest.json"] = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.check:
        drift = [
            name
            for name, content in expected.items()
            if not (OUTPUT / name).exists()
            or (OUTPUT / name).read_text(encoding="utf-8") != content
        ]
        if drift:
            raise SystemExit("review contract drift: " + ", ".join(drift))
        return 0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, content in expected.items():
        (OUTPUT / name).write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
