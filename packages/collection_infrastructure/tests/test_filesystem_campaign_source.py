from __future__ import annotations

from pathlib import Path

import pytest

from collection_contracts import OwnerContextError
from collection_infrastructure import FilesystemCampaignBundleSource

_REPOSITORY_ROOT = Path(__file__).parents[3]


def test_real_campaign_bundle_contains_only_allowlisted_files() -> None:
    source = FilesystemCampaignBundleSource(_REPOSITORY_ROOT / "campaigns")

    bundle = source.read("berlin_recording_services", "correlation-real")

    assert set(bundle.files) == {
        "attributes.yaml",
        "campaign.yaml",
        "discovery/manual_seeds.csv",
        "entity_kinds.yaml",
        "source_bindings.yaml",
        "source_policies/manual_seed_policy.yaml",
        "taxonomy.yaml",
    }


def test_campaign_key_cannot_escape_root(tmp_path: Path) -> None:
    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    source = FilesystemCampaignBundleSource(campaigns)

    with pytest.raises(OwnerContextError) as captured:
        source.read("../outside", "correlation-path")

    assert captured.value.envelope.code == "CAMPAIGN_KEY_INVALID"


def test_unexpected_file_is_rejected(tmp_path: Path) -> None:
    campaign = tmp_path / "campaigns" / "example"
    campaign.mkdir(parents=True)
    (campaign / "script.py").write_text("print('not config')\n", encoding="utf-8")
    source = FilesystemCampaignBundleSource(tmp_path / "campaigns")

    with pytest.raises(OwnerContextError) as captured:
        source.read("example", "correlation-unexpected")

    assert captured.value.envelope.code == "CAMPAIGN_FILE_UNEXPECTED"
    assert captured.value.envelope.context["path"] == "script.py"


def test_symlink_is_rejected(tmp_path: Path) -> None:
    campaign = tmp_path / "campaigns" / "example"
    campaign.mkdir(parents=True)
    outside = tmp_path / "outside.yaml"
    outside.write_text("value: outside\n", encoding="utf-8")
    link = campaign / "campaign.yaml"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("test process cannot create symlinks")
    source = FilesystemCampaignBundleSource(tmp_path / "campaigns")

    with pytest.raises(OwnerContextError) as captured:
        source.read("example", "correlation-symlink")

    assert captured.value.envelope.code == "CAMPAIGN_PATH_BOUNDARY_VIOLATION"
    assert captured.value.envelope.context["reason"] == "symlink_not_allowed"
