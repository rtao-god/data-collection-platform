from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pytest

from collection_application import CampaignSnapshotService, RawCampaignBundle
from collection_contracts import OwnerContextError

_REPOSITORY_ROOT = Path(__file__).parents[3]
_CAMPAIGN_ROOT = _REPOSITORY_ROOT / "campaigns" / "berlin_recording_services"


@dataclass(frozen=True)
class InMemorySource:
    files: dict[str, bytes]

    def read(self, campaign_key: str, correlation_id: str) -> RawCampaignBundle:
        del correlation_id
        return RawCampaignBundle(campaign_key=campaign_key, files=MappingProxyType(self.files))


def _valid_files() -> dict[str, bytes]:
    return {
        path.relative_to(_CAMPAIGN_ROOT).as_posix(): path.read_bytes()
        for path in _CAMPAIGN_ROOT.rglob("*")
        if path.is_file()
    }


def test_snapshot_is_deterministic_and_preserves_explicit_blocker() -> None:
    service = CampaignSnapshotService(InMemorySource(_valid_files()))

    first = service.create("berlin_recording_services", "correlation-1")
    second = service.create("berlin_recording_services", "correlation-2")

    assert first.bundle_digest == second.bundle_digest
    assert first.readiness == "blocked"
    assert [blocker.code for blocker in first.blockers] == ["BERLIN_BOUNDARY_ARTIFACT_MISSING"]
    assert tuple(component.path for component in first.components) == tuple(
        sorted(component.path for component in first.components)
    )


def test_seed_content_changes_bundle_digest() -> None:
    files = _valid_files()
    service = CampaignSnapshotService(InMemorySource(files))
    original = service.create("berlin_recording_services", "correlation-1")
    files["discovery/manual_seeds.csv"] += (
        b"place,Example Studio,https://example.test,,,Owned test seed,operator fixture\n"
    )

    changed = service.create("berlin_recording_services", "correlation-2")

    assert changed.bundle_digest != original.bundle_digest


def test_duplicate_yaml_key_is_rejected_with_owner_context() -> None:
    files = _valid_files()
    files["campaign.yaml"] += b"campaign_key: duplicate_key\n"

    with pytest.raises(OwnerContextError) as captured:
        CampaignSnapshotService(InMemorySource(files)).create(
            "berlin_recording_services",
            "correlation-duplicate",
        )

    assert captured.value.envelope.code == "CAMPAIGN_YAML_INVALID"
    assert captured.value.envelope.context["document"] == "campaign.yaml"
    assert captured.value.envelope.correlation_id == "correlation-duplicate"


def test_unresolved_taxonomy_reference_is_rejected() -> None:
    files = _valid_files()
    files["campaign.yaml"] = files["campaign.yaml"].replace(
        b"  - recording_studio\n",
        b"  - unknown_category\n",
        1,
    )

    with pytest.raises(OwnerContextError) as captured:
        CampaignSnapshotService(InMemorySource(files)).create(
            "berlin_recording_services",
            "correlation-reference",
        )

    assert captured.value.envelope.code == "CAMPAIGN_REFERENCE_INVALID"
    violations = captured.value.envelope.context["violations"]
    assert any(item["value"] == "unknown_category" for item in violations)


def test_manual_seed_header_must_be_exact() -> None:
    files = _valid_files()
    files["discovery/manual_seeds.csv"] = b"display_name,expected_entity_kind\n"

    with pytest.raises(OwnerContextError) as captured:
        CampaignSnapshotService(InMemorySource(files)).create(
            "berlin_recording_services",
            "correlation-header",
        )

    assert captured.value.envelope.code == "MANUAL_SEED_HEADER_INVALID"
