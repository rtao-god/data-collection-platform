from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest
import yaml

from collection_application import CampaignSnapshotService, RawCampaignBundle
from collection_application.campaign_snapshot_service import CompiledCampaignBundle
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineTransitionDisposition,
    PipelineTransitionRegistry,
    SucceededWorkOutput,
)
from collection_domain import WorkStage

_RUN_ID = UUID("00000000-0000-0000-0000-000000000801")
_WORK_ID = UUID("00000000-0000-0000-0000-000000000802")
_STAGE_RUN_ID = UUID("00000000-0000-0000-0000-000000000803")
_RAW_ID = UUID("00000000-0000-0000-0000-000000000804")
_OBS_ID = UUID("00000000-0000-0000-0000-000000000805")
_QUERY_ID = UUID("00000000-0000-0000-0000-000000000806")


def _observation(element_type: str, osm_id: int, *, lat: float = 52.5, lon: float = 13.4) -> dict:
    return {
        "address": {"city": "Berlin", "country": "DE", "postcode": "10115"},
        "elementType": element_type,
        "latitude": lat,
        "longitude": lon,
        "osmId": osm_id,
        "sourceUrl": f"https://www.openstreetmap.org/{element_type}/{osm_id}",
        "tags": {"amenity": "studio"},
    }


def _osm_envelope(*elements: tuple[str, int]) -> bytes:
    return json.dumps(
        {
            "attribution": "© OpenStreetMap contributors",
            "generator": "osm-overpass-worker",
            "observations": [_observation(kind, osm_id) for kind, osm_id in elements],
            "osmBaseTimestampUtc": "2026-01-05T00:00:00.000000Z",
            "queryDigest": "sha256:" + "2" * 64,
            "responseDigest": "sha256:" + "3" * 64,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


_OBSERVATIONS = _osm_envelope(("node", 101), ("way", 202))
_OBSERVATIONS_DIGEST = "sha256:" + hashlib.sha256(_OBSERVATIONS).hexdigest()
_RAW_DIGEST = "sha256:" + "1" * 64


def _osm_campaign_snapshot() -> CompiledCampaignBundle:
    campaign_root = Path(__file__).resolve().parents[3] / "campaigns" / "berlin_recording_services"
    files = {
        path.relative_to(campaign_root).as_posix(): path.read_bytes()
        for path in campaign_root.rglob("*")
        if path.is_file()
    }
    campaign = yaml.safe_load(files["campaign.yaml"])
    if "osm_overpass_berlin" not in campaign["enabled_source_bindings"]:
        campaign["enabled_source_bindings"].append("osm_overpass_berlin")
    files["campaign.yaml"] = yaml.safe_dump(campaign, sort_keys=False).encode()
    # The real policy is legal_status research_only, which an enabled binding cannot
    # use. The test enables the binding against an explicitly approved test policy.
    policies = yaml.safe_load(files["source_bindings.yaml"])
    for item in policies["items"]:
        if item["key"] == "osm_overpass_berlin":
            item["source_policy_key"] = "osm_test_policy"
    files["source_bindings.yaml"] = yaml.safe_dump(policies, sort_keys=False).encode()
    files["source_policies/osm_test_policy.yaml"] = yaml.safe_dump(
        {
            "schema_revision": "source-policy-v1",
            "policy_key": "osm_test_policy",
            "policy_revision": "osm-test-policy-v1",
            "source_key": "openstreetmap",
            "source_type": "openstreetmap",
            "legal_status": "approved",
            "terms_review": {
                "state": "reviewed",
                "terms_url": "https://www.openstreetmap.org/copyright",
                "reviewed_at": "2026-01-01",
                "expires_at": "2099-01-01",
                "reviewer": "test-reviewer",
            },
            "access": {
                "kind": "network",
                "allowed_methods": ["GET", "POST"],
                "allowed_host_patterns": ["overpass-api.de"],
                "max_requests_per_second": 1,
                "max_concurrency": 1,
                "connect_timeout_seconds": 10,
                "read_timeout_seconds": 60,
                "max_encoded_bytes": 1_000_000,
                "max_decoded_bytes": 5_000_000,
                "content_type_allowlist": ["application/json"],
                "redirect_limit": 0,
            },
            "storage_class": "collector-structured-evidence-v1",
            "retention_days": 30,
            "allowed_fields": ["district", "service_mode"],
            "prohibited_fields": ["review_text"],
            "attribution_required": True,
            "browser": "forbidden",
            "screenshots": "forbidden",
            "retry_budget": 2,
            "stop_on_status_codes": [429],
            "reviewer": "test-reviewer",
            "approver": "test-approver",
        },
        sort_keys=False,
    ).encode()

    class Source:
        def read(self, campaign_key: str, correlation_id: str) -> RawCampaignBundle:
            assert campaign_key == "berlin_recording_services"
            assert correlation_id == "osm-mapping-test"
            return RawCampaignBundle(campaign_key, MappingProxyType(files))

    return CampaignSnapshotService(Source()).compile(
        "berlin_recording_services", "osm-mapping-test"
    )


def _output_artifact(role: str) -> ArtifactIdentity:
    if role == "osm_raw_response":
        return ArtifactIdentity(
            artifact_id=_RAW_ID,
            role="osm_raw_response",
            content_digest=_RAW_DIGEST,
            size_bytes=64,
            content_type="application/json",
        )
    return ArtifactIdentity(
        artifact_id=_OBS_ID,
        role="osm_observations",
        content_digest=_OBSERVATIONS_DIGEST,
        size_bytes=len(_OBSERVATIONS),
        content_type="application/vnd.collection.osm-observations+json",
    )


def _source_fields(compiled: CompiledCampaignBundle) -> dict[str, object]:
    related = (_output_artifact("osm_raw_response"), _output_artifact("osm_observations"))
    return {
        "source_work_unit_id": _WORK_ID,
        "run_id": _RUN_ID,
        "stage_run_id": _STAGE_RUN_ID,
        "stage": WorkStage.DISCOVERY,
        "capability": "osm_query",
        "output_contract": "osm-overpass-result@1",
        "output_digest": _OBSERVATIONS_DIGEST,
        "output_artifact": _output_artifact("osm_observations"),
        "input_artifacts": (
            ArtifactIdentity(
                artifact_id=_QUERY_ID,
                role="overpass_query_spec",
                content_digest="sha256:" + "3" * 64,
                size_bytes=128,
                content_type="application/vnd.collection.overpass-query-spec+json",
            ),
        ),
        "source_binding_key": "osm_overpass_berlin",
        "config_bundle_digest": compiled.snapshot.bundle_digest,
        "campaign_bundle_content": compiled.canonical_content,
        "output_content": _OBSERVATIONS,
        "related_output_artifacts": related,
    }


def _osm_source(compiled: CompiledCampaignBundle, **overrides: object) -> SucceededWorkOutput:
    fields = _source_fields(compiled)
    fields.update(overrides)
    return SucceededWorkOutput(**fields)  # type: ignore[arg-type]


def test_osm_result_plans_deterministic_mapping_without_manual_lineage() -> None:
    compiled = _osm_campaign_snapshot()

    plan = PipelineTransitionRegistry().plan(_osm_source(compiled))

    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert plan.blocker is None
    assert plan.transition_key == "osm-overpass-result-source-record-mapping-v1"
    action = plan.source_record_mapping_actions[0]
    assert action.source_binding_key == "osm_overpass_berlin"
    assert action.source_key == "openstreetmap"
    assert action.source_record_id == "osm:node:101"
    assert (
        action.source_record_artifact.content_digest
        == "sha256:" + hashlib.sha256(action.source_record_payload).hexdigest()
    )
    assert action.source_record_artifact.role == "source_record"
    assert action.raw_response_artifact.role == "osm_observations"
    assert action.mapping_profile_digest == (
        "sha256:" + hashlib.sha256(action.mapping_profile_payload).hexdigest()
    )
    assert action.source_policy_digest.startswith("sha256:")
    assert not hasattr(action, "manual_record_lineage_artifact")


def test_osm_mapping_profile_pins_exact_source_identity() -> None:
    compiled = _osm_campaign_snapshot()

    action = (
        PipelineTransitionRegistry().plan(_osm_source(compiled)).source_record_mapping_actions[0]
    )

    profile = action.mapping_profile_payload.decode("utf-8")
    assert "openstreetmap" in profile
    assert "osm_overpass_berlin" in profile
    assert "berlin_recording_services" in profile
    assert action.source_policy_digest in profile


def test_osm_source_record_mapping_identity_is_stable_across_replans() -> None:
    compiled = _osm_campaign_snapshot()
    registry = PipelineTransitionRegistry()

    first = registry.plan(_osm_source(compiled))
    second = registry.plan(_osm_source(compiled))

    assert first.plan_digest == second.plan_digest
    assert len(first.source_record_mapping_actions) == 2
    assert [item.work_id for item in first.source_record_mapping_actions] == [
        item.work_id for item in second.source_record_mapping_actions
    ]
    assert [item.source_record_id for item in first.source_record_mapping_actions] == [
        "osm:node:101",
        "osm:way:202",
    ]


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing_observation_output", "PIPELINE_SOURCE_RECORD_RAW_AMBIGUOUS"),
        ("missing_query", "PIPELINE_SOURCE_RECORD_QUERY_MISSING"),
        ("missing_binding", "PIPELINE_SOURCE_RECORD_BINDING_MISSING"),
        ("no_snapshot", "PIPELINE_SOURCE_RECORD_SNAPSHOT_INVALID"),
    ],
)
def test_osm_transition_blocks_with_owner_context(case: str, code: str) -> None:
    compiled = _osm_campaign_snapshot()
    overrides: dict[str, object] = {}
    if case == "missing_observation_output":
        overrides["related_output_artifacts"] = (_output_artifact("osm_raw_response"),)
    elif case == "missing_query":
        overrides["input_artifacts"] = ()
    elif case == "missing_binding":
        overrides["source_binding_key"] = None
    else:
        overrides["campaign_bundle_content"] = None

    plan = PipelineTransitionRegistry().plan(_osm_source(compiled, **overrides))

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == code
    assert plan.blocker.required_action
    assert plan.source_record_mapping_actions == ()


def test_osm_binding_must_declare_osm_query_capability() -> None:
    compiled = _osm_campaign_snapshot()
    source = _osm_source(compiled, source_binding_key="manual_seed_import")

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_SOURCE_RECORD_BINDING_INVALID"
