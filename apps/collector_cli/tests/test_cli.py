from __future__ import annotations

import json
from pathlib import Path

import pytest

from collector_cli import run

_REPOSITORY_ROOT = Path(__file__).parents[3]


def test_digest_command_returns_stable_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        [
            "--campaigns-root",
            str(_REPOSITORY_ROOT / "campaigns"),
            "config",
            "digest",
            "berlin_recording_services",
        ]
    )
    output = capsys.readouterr()
    payload = json.loads(output.out)

    assert exit_code == 0
    assert payload["campaignKey"] == "berlin_recording_services"
    assert payload["bundleDigest"].startswith("sha256:")
    assert payload["readiness"] == "blocked"


def test_missing_campaign_returns_typed_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        [
            "--campaigns-root",
            str(_REPOSITORY_ROOT / "campaigns"),
            "config",
            "validate",
            "missing_campaign",
        ]
    )
    output = capsys.readouterr()
    payload = json.loads(output.err)

    assert exit_code == 2
    assert payload["code"] == "CAMPAIGN_NOT_FOUND"
    assert payload["owner"] == "CampaignConfiguration"
