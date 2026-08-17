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


def test_campaign_geography_owner_paths_are_bounded_and_allowlisted(tmp_path: Path) -> None:
    campaign = tmp_path / "campaigns" / "example"
    geography = campaign / "geography"
    geography.mkdir(parents=True)
    (campaign / "geography.yaml").write_text("schema_revision: geography-config-v1\n")
    (geography / "primary.geojson").write_text(
        '{"coordinates":[[[0,0],[1,0],[1,1],[0,0]]],"type":"Polygon"}',
        encoding="utf-8",
    )
    (geography / "primary.provenance.json").write_text("{}", encoding="utf-8")
    source = FilesystemCampaignBundleSource(tmp_path / "campaigns")

    bundle = source.read("example", "correlation-geography")

    assert set(bundle.files) == {
        "geography.yaml",
        "geography/primary.geojson",
        "geography/primary.provenance.json",
    }
