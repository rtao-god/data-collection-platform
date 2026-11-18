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


def _with_normalization_profile(
    files: dict[str, bytes],
    *,
    target_field: str = "website",
    value_kind: str = "url",
    prohibited_fields: tuple[str, ...] = (),
) -> dict[str, bytes]:
    import yaml

    bindings = yaml.safe_load(files["source_bindings.yaml"])
    bindings["items"][0]["normalization_profile"] = "website_profile"
    files["source_bindings.yaml"] = yaml.safe_dump(bindings, sort_keys=False).encode()
    files["normalization_profiles.yaml"] = yaml.safe_dump(
        {
            "schema_revision": "normalization-profiles-v1",
            "items": [
                {
                    "key": "website_profile",
                    "profile": {
                        "normalizer_revision": "website-normalizer-v1",
                        "field_rules": [
                            {
                                "source_field": "website",
                                "target_field": target_field,
                                "value_kind": value_kind,
                            }
                        ],
                        "prohibited_fields": list(prohibited_fields),
                    },
                }
            ],
        },
        sort_keys=False,
    ).encode()
    return files


def test_snapshot_is_deterministic_and_preserves_ready_state() -> None:
    service = CampaignSnapshotService(InMemorySource(_valid_files()))

    first = service.create("berlin_recording_services", "correlation-1")
    second = service.create("berlin_recording_services", "correlation-2")

    assert first.bundle_digest == second.bundle_digest
    assert first.readiness == "ready"
    assert first.blockers == ()
    assert tuple(component.path for component in first.components) == tuple(
        sorted(component.path for component in first.components)
    )


def test_seed_content_changes_bundle_digest() -> None:
    files = _valid_files()
    service = CampaignSnapshotService(InMemorySource(files))
    original = service.create("berlin_recording_services", "correlation-1")
    files["discovery/manual_seeds.csv"] += (
        b"place,Example Studio,recording_studio,https://example.test,,,"
        b"Owned test seed,operator fixture" + bytes((10,))
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
    import yaml

    files = _valid_files()
    campaign = yaml.safe_load(files["campaign.yaml"])
    campaign["target_categories"][0] = "unknown_category"
    files["campaign.yaml"] = yaml.safe_dump(campaign, sort_keys=False).encode()

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


def test_manual_seed_declared_format_must_be_policy_approved() -> None:
    files = _valid_files()
    files["source_bindings.yaml"] = files["source_bindings.yaml"].replace(
        b"      format: csv\n",
        b"      format: json\n",
    )
    files["source_policies/manual_seed_policy.yaml"] = files[
        "source_policies/manual_seed_policy.yaml"
    ].replace(b"    - json\n", b"")

    with pytest.raises(OwnerContextError) as captured:
        CampaignSnapshotService(InMemorySource(files)).create(
            "berlin_recording_services",
            "correlation-format-policy",
        )

    assert captured.value.envelope.code == "MANUAL_SEED_FORMAT_FORBIDDEN"


def test_normalization_profile_is_reference_bound_and_included_in_snapshot() -> None:
    files = _with_normalization_profile(_valid_files())

    compiled = CampaignSnapshotService(InMemorySource(files)).compile(
        "berlin_recording_services",
        "correlation-normalization-profile",
    )

    assert compiled.documents.source_bindings.items[0].normalization_profile == "website_profile"
    assert compiled.documents.normalization_profiles is not None
    assert compiled.documents.normalization_profiles.items[0].profile.normalizer_revision == (
        "website-normalizer-v1"
    )
    component_paths = {component.path for component in compiled.snapshot.components}
    assert "normalization_profiles.yaml" in component_paths
    assert compiled.canonical_documents["normalization_profiles.yaml"]["items"][0]["key"] == (
        "website_profile"
    )


def test_missing_normalization_profile_reference_blocks_snapshot() -> None:
    files = _valid_files()
    files["source_bindings.yaml"] = files["source_bindings.yaml"].replace(
        b"    extraction_profile: manual-seed-v1\n",
        b"    extraction_profile: manual-seed-v1\n    normalization_profile: missing_profile\n",
    )

    with pytest.raises(OwnerContextError) as captured:
        CampaignSnapshotService(InMemorySource(files)).create(
            "berlin_recording_services",
            "correlation-normalization-profile-missing",
        )

    assert captured.value.envelope.code == "CAMPAIGN_REFERENCE_INVALID"
    assert any(
        item["reference"] == "binding.manual_seed_import.normalization_profile"
        and item["reason"] == "missing"
        for item in captured.value.envelope.context["violations"]
    )


def test_normalization_profile_value_kind_must_match_attribute_type() -> None:
    files = _with_normalization_profile(_valid_files(), value_kind="boolean")

    with pytest.raises(OwnerContextError) as captured:
        CampaignSnapshotService(InMemorySource(files)).create(
            "berlin_recording_services",
            "correlation-normalization-profile-type-mismatch",
        )

    assert captured.value.envelope.code == "CAMPAIGN_REFERENCE_INVALID"
    assert any(
        item["reason"] == "normalization_type_mismatch"
        and item["value"] == "website"
        and item["normalizationKind"] == "boolean"
        and item["attributeValueType"] == "url"
        for item in captured.value.envelope.context["violations"]
    )


def test_normalization_profile_targets_must_be_allowed_by_source_policy() -> None:
    import yaml

    files = _with_normalization_profile(_valid_files())
    policy_path = "source_policies/manual_seed_policy.yaml"
    policy = yaml.safe_load(files[policy_path])
    policy["allowed_fields"].remove("website")
    files[policy_path] = yaml.safe_dump(policy, sort_keys=False).encode()

    with pytest.raises(OwnerContextError) as captured:
        CampaignSnapshotService(InMemorySource(files)).create(
            "berlin_recording_services",
            "correlation-normalization-profile-policy",
        )

    assert captured.value.envelope.code == "CAMPAIGN_REFERENCE_INVALID"
    assert any(
        item["reference"] == "binding.manual_seed_import.normalization_profile.allowed_fields"
        and item["value"] == "website"
        and item["reason"] == "missing"
        for item in captured.value.envelope.context["violations"]
    )


def test_normalization_profile_rejects_prohibited_target_field() -> None:
    files = _with_normalization_profile(_valid_files(), prohibited_fields=("website",))

    with pytest.raises(OwnerContextError) as captured:
        CampaignSnapshotService(InMemorySource(files)).create(
            "berlin_recording_services",
            "correlation-normalization-profile-prohibited",
        )

    assert captured.value.envelope.code == "CAMPAIGN_REFERENCE_INVALID"
    assert any(
        item["reason"] == "normalization_target_is_prohibited"
        for item in captured.value.envelope.context["violations"]
    )


def test_ready_campaign_requires_exact_geography_revision() -> None:
    files = _valid_files()
    del files["geography.yaml"]

    with pytest.raises(OwnerContextError) as captured:
        CampaignSnapshotService(InMemorySource(files)).create(
            "berlin_recording_services",
            "correlation-geography-missing",
        )

    assert captured.value.envelope.code == "CAMPAIGN_REFERENCE_INVALID"
    violations = captured.value.envelope.context["violations"]
    assert any(item["reason"] == "geography_document_missing" for item in violations)


def test_geography_artifacts_are_digest_bound_snapshot_components() -> None:
    import hashlib
    import json

    import yaml

    files = _valid_files()
    campaign = yaml.safe_load(files["campaign.yaml"])
    campaign["readiness"] = {"state": "ready"}
    files["campaign.yaml"] = yaml.safe_dump(campaign, sort_keys=False).encode()
    boundary = json.dumps(
        {
            "coordinates": [
                [
                    [13.0, 52.0],
                    [13.1, 52.0],
                    [13.1, 52.1],
                    [13.0, 52.0],
                ]
            ],
            "type": "Polygon",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    boundary_digest = f"sha256:{hashlib.sha256(boundary).hexdigest()}"
    provenance = json.dumps(
        {
            "authority": "State of Berlin",
            "boundary_artifact_path": "geography/berlin-boundary.geojson",
            "boundary_digest": boundary_digest,
            "contract": "campaign-geography-provenance",
            "contract_revision": "campaign-geography-provenance-v1",
            "dataset_identifier": "official-test-dataset",
            "dataset_title": "Official Berlin boundary test fixture",
            "derivation": "Independent owner contract fixture.",
            "distribution_feature_count": 12,
            "distribution_owner": "Official fixture owner",
            "distribution_url": "https://example.test/berlin.geojson",
            "license_identifier": "dl-de-zero-2.0",
            "license_title": "Datenlizenz Deutschland Zero 2.0",
            "source_digest": "sha256:" + "a" * 64,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    provenance_digest = f"sha256:{hashlib.sha256(provenance).hexdigest()}"
    files["geography/berlin-boundary.geojson"] = boundary
    files["geography/berlin-boundary.provenance.json"] = provenance
    files["geography.yaml"] = yaml.safe_dump(
        {
            "schema_revision": "geography-config-v1",
            "geography_revision": campaign["geography_revision"],
            "boundary_artifact_path": "geography/berlin-boundary.geojson",
            "boundary_digest": boundary_digest,
            "provenance_artifact_path": ("geography/berlin-boundary.provenance.json"),
            "provenance_digest": provenance_digest,
        },
        sort_keys=False,
    ).encode()

    snapshot = CampaignSnapshotService(InMemorySource(files)).create(
        "berlin_recording_services",
        "correlation-geography-ready",
    )

    assert snapshot.readiness == "ready"
    component_paths = {component.path for component in snapshot.components}
    assert {
        "geography.yaml",
        "geography/berlin-boundary.geojson",
        "geography/berlin-boundary.provenance.json",
    }.issubset(component_paths)
