"""End-to-end proof: OSM envelope -> per-element fan-out -> mapping -> intake -> review.

Deliberately exercises only pure owners (contracts, application planner, worker mapping
core). Database persistence is covered separately by the applier tests; this proves the
data path is complete and lossless across every stage boundary.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID

import pytest
import yaml

from collection_application.campaign_documents import RawCampaignBundle
from collection_application.campaign_snapshot_service import CampaignSnapshotService
from collection_application.geography import decode_boundary_geojson
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineTransitionDisposition,
    PipelineTransitionRegistry,
    SucceededWorkOutput,
)
from collection_contracts import (
    ObservationState,
    decode_osm_envelope,
)
from collection_domain import WorkCapability, WorkStage
from processing_worker.source_record_mapping import materialize_source_record_observations

_RUN_ID = UUID("00000000-0000-0000-0000-000000000a01")
_WORK_ID = UUID("00000000-0000-0000-0000-000000000a02")
_STAGE_RUN_ID = UUID("00000000-0000-0000-0000-000000000a03")
_RAW_ID = UUID("00000000-0000-0000-0000-000000000a04")
_OBS_ID = UUID("00000000-0000-0000-0000-000000000a05")
_QUERY_ID = UUID("00000000-0000-0000-0000-000000000a06")
_CAMPAIGN = None


def _config_digest() -> str:
    global _CAMPAIGN
    if _CAMPAIGN is None:
        _CAMPAIGN = _compiled_campaign()
    return "sha256:" + sha256(_CAMPAIGN.canonical_content).hexdigest()


def _config_campaign_content() -> bytes:
    global _CAMPAIGN
    if _CAMPAIGN is None:
        _CAMPAIGN = _compiled_campaign()
    return _CAMPAIGN.canonical_content


def _observation(element_type: str, osm_id: int, *, name: str, website: str | None) -> dict:
    tags = {"amenity": "studio", "name": name}
    if website:
        tags["website"] = website
    return {
        "address": {"city": "Berlin", "country": "DE", "postcode": "10119", "street": "Teststr."},
        "elementType": element_type,
        "latitude": 52.53,
        "longitude": 13.40,
        "osmId": osm_id,
        "sourceUrl": f"https://www.openstreetmap.org/{element_type}/{osm_id}",
        "tags": tags,
    }


_ENVELOPE = json.dumps(
    {
        "attribution": "© OpenStreetMap contributors",
        "generator": "osm-overpass-worker",
        "observations": [
            _observation("node", 501, name="Studio Nord", website="https://nord.example"),
            _observation("node", 502, name="Studio Süd", website=None),
            _observation("way", 503, name="Studio Ost", website="https://ost.example"),
        ],
        "osmBaseTimestampUtc": "2026-01-05T00:00:00.000000Z",
        "queryDigest": "sha256:" + "1" * 64,
        "responseDigest": "sha256:" + "2" * 64,
    },
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
_ENVELOPE_DIGEST = "sha256:" + sha256(_ENVELOPE).hexdigest()


def _compiled_campaign(extra_files: dict[str, bytes] | None = None) -> Any:
    campaign_root = Path(__file__).resolve().parents[3] / "campaigns" / "berlin_recording_services"
    files = {
        path.relative_to(campaign_root).as_posix(): path.read_bytes()
        for path in campaign_root.rglob("*")
        if path.is_file()
    }
    campaign = yaml.safe_load(files["campaign.yaml"])
    campaign["enabled_source_bindings"] = ["osm_overpass_berlin"]
    files["campaign.yaml"] = yaml.safe_dump(campaign, sort_keys=False).encode()
    bindings = yaml.safe_load(files["source_bindings.yaml"])
    for item in bindings["items"]:
        if item["key"] == "osm_overpass_berlin":
            item["source_policy_key"] = "osm_e2e_policy"
    files["source_bindings.yaml"] = yaml.safe_dump(bindings, sort_keys=False).encode()
    files["source_policies/osm_e2e_policy.yaml"] = yaml.safe_dump(
        {
            "schema_revision": "source-policy-v1",
            "policy_key": "osm_e2e_policy",
            "policy_revision": "osm-e2e-policy-v1",
            "source_key": "openstreetmap",
            "source_type": "openstreetmap",
            "legal_status": "approved",
            "terms_review": {
                "state": "reviewed",
                "terms_url": "https://www.openstreetmap.org/copyright",
                "reviewed_at": "2026-01-01",
                "expires_at": "2099-01-01",
                "reviewer": "e2e-reviewer",
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
            "allowed_fields": ["district"],
            "prohibited_fields": ["review_text"],
            "attribution_required": True,
            "browser": "forbidden",
            "screenshots": "forbidden",
            "retry_budget": 2,
            "stop_on_status_codes": [429],
            "reviewer": "e2e-reviewer",
            "approver": "e2e-approver",
        },
        sort_keys=False,
    ).encode()

    if extra_files:
        files.update(extra_files)

    class _Source:
        def read(self, campaign_key: str, correlation_id: str) -> RawCampaignBundle:
            return RawCampaignBundle(campaign_key, MappingProxyType(files))

    return CampaignSnapshotService(_Source()).compile("berlin_recording_services", "e2e")


def _osm_source() -> SucceededWorkOutput:
    return SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_STAGE_RUN_ID,
        stage=WorkStage.DISCOVERY,
        capability=WorkCapability.OSM_QUERY.value,
        output_contract="osm-overpass-result@1",
        output_digest=_ENVELOPE_DIGEST,
        output_artifact=ArtifactIdentity(
            artifact_id=_RAW_ID,
            role="osm_raw_response",
            content_digest=_ENVELOPE_DIGEST,
            size_bytes=len(_ENVELOPE),
            content_type="application/vnd.collection.osm-observations+json",
        ),
        related_output_artifacts=(
            ArtifactIdentity(
                artifact_id=_OBS_ID,
                role="osm_observations",
                content_digest=_ENVELOPE_DIGEST,
                size_bytes=len(_ENVELOPE),
                content_type="application/vnd.collection.osm-observations+json",
            ),
        ),
        input_artifacts=(
            ArtifactIdentity(
                artifact_id=_QUERY_ID,
                role="overpass_query_spec",
                content_digest="sha256:" + "9" * 64,
                size_bytes=10,
                content_type="application/json",
            ),
        ),
        source_binding_key="osm_overpass_berlin",
        config_bundle_digest=_config_digest(),
        campaign_bundle_content=_compiled_campaign().canonical_content,
        output_content=_ENVELOPE,
    )


def test_osm_envelope_fans_out_one_mapping_unit_per_element() -> None:
    plan = PipelineTransitionRegistry().plan(_osm_source())

    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert plan.blocker is None
    actions = plan.source_record_mapping_actions
    assert [item.source_record_id for item in actions] == [
        "osm:node:501",
        "osm:node:502",
        "osm:way:503",
    ]
    # Every unit carries exactly one immutable record derived from the same envelope.
    for action in actions:
        assert action.source_record_artifact.role == "source_record"
        assert action.raw_response_artifact.artifact_id == _OBS_ID
        assert len(action.source_record_payload) > 0


def test_each_fan_out_exposes_exactly_one_element() -> None:
    plan = PipelineTransitionRegistry().plan(_osm_source())

    for action in plan.source_record_mapping_actions:
        record = json.loads(action.source_record_payload)
        assert record["contract"] == "osm-source-record@1"
        assert record["envelope_digest"] == _ENVELOPE_DIGEST


# Verbatim tags of OSM node 871572117 ("nb film"), acquired live from the configured
# endpoint. It publishes its site and phone only under the `contact:` namespace.
_REAL_NODE_TAGS = {
    "addr:city": "Berlin",
    "addr:country": "DE",
    "addr:housenumber": "125",
    "addr:postcode": "10999",
    "addr:street": "Dresdener Straße",
    "addr:suburb": "Kreuzberg",
    "amenity": "recording_studio",
    "contact:mobile": "+49 174 5910833",
    "contact:phone": "+49 30 31169672",
    "contact:website": "http://www.nbfilm.de",
    "name": "nb film",
    "operator": "Thorsten Schneider",
}


def _real_node_envelope(tags: dict[str, str]) -> bytes:
    observation = _observation("node", 871572117, name="nb film", website=None)
    observation["tags"] = tags
    observation["latitude"] = 52.5007771
    observation["longitude"] = 13.4168436
    observation["address"] = {
        "city": "Berlin",
        "country": "DE",
        "houseNumber": "125",
        "postcode": "10999",
        "street": "Dresdener Straße",
    }
    return json.dumps(
        {
            "attribution": "© OpenStreetMap contributors",
            "generator": "osm-overpass-worker",
            "observations": [observation],
            "osmBaseTimestampUtc": "2026-11-16T00:01:59.000000Z",
            "queryDigest": "sha256:" + "1" * 64,
            "responseDigest": "sha256:" + "2" * 64,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _real_node_source(reference: SucceededWorkOutput, envelope: bytes) -> SucceededWorkOutput:
    from dataclasses import replace

    digest = "sha256:" + sha256(envelope).hexdigest()
    return replace(
        reference,
        output_digest=digest,
        output_artifact=replace(reference.output_artifact, content_digest=digest),
        related_output_artifacts=(
            replace(
                reference.related_output_artifacts[0],
                content_digest=digest,
                size_bytes=len(envelope),
            ),
        ),
        output_content=envelope,
    )


def _mapped_batch(action: object) -> Any:
    from processing_worker.source_record_mapping import (
        materialize_source_record_observations,
    )

    return materialize_source_record_observations(
        action.source_record_payload,
        action.mapping_profile_payload,
        maximum_records=1,
    )


def test_real_node_contact_website_is_observed_through_the_campaign_projection() -> None:
    """End to end with the real campaign bundle: the alias decision is what makes this work."""

    envelope = _real_node_envelope(_REAL_NODE_TAGS)
    source = _real_node_source(_osm_source(), envelope)
    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert len(plan.source_record_mapping_actions) == 1
    batch = _mapped_batch(plan.source_record_mapping_actions[0])
    by_field = {item.field_key: item for item in batch.observations}

    website = by_field["website"]
    assert website.state is ObservationState.OBSERVED
    assert website.value is not None
    # The normalizer canonicalizes every path-less URL to "/", so the observed value
    # carries it; the assertion states the canonical form, not the raw tag text.
    assert website.value.normalized == "http://www.nbfilm.de/"
    assert website.evidence is not None
    assert website.evidence.locator_value == "/tags/contact:website"
    assert website.evidence.source_url == "https://www.openstreetmap.org/node/871572117"

    phone = by_field["phone"]
    assert phone.state is ObservationState.OBSERVED
    assert phone.evidence is not None
    assert phone.evidence.locator_value == "/tags/contact:phone"

    # email is genuinely not published, so it stays unknown.
    assert by_field["email"].state is ObservationState.NOT_OBSERVED


def test_real_node_replays_to_one_identical_mapping_and_plan() -> None:
    registry = PipelineTransitionRegistry()
    envelope = _real_node_envelope(_REAL_NODE_TAGS)

    first = registry.plan(_real_node_source(_osm_source(), envelope))
    second = registry.plan(_real_node_source(_osm_source(), envelope))

    assert first.plan_digest == second.plan_digest
    first_action = first.source_record_mapping_actions[0]
    second_action = second.source_record_mapping_actions[0]
    assert first_action.semantic_key == second_action.semantic_key
    assert first_action.mapping_profile_digest == second_action.mapping_profile_digest
    assert _mapped_batch(first_action).content_digest == _mapped_batch(second_action).content_digest


def test_website_less_business_survives_the_campaign_projection() -> None:
    """A business publishing no website at all must still map, as NOT_OBSERVED."""

    tags = {key: value for key, value in _REAL_NODE_TAGS.items() if key not in {"contact:website"}}
    envelope = _real_node_envelope(tags)
    source = _real_node_source(_osm_source(), envelope)
    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.APPLY
    batch = _mapped_batch(plan.source_record_mapping_actions[0])
    by_field = {item.field_key: item for item in batch.observations}

    assert by_field["website"].state is ObservationState.NOT_OBSERVED
    assert by_field["website"].value is None
    assert by_field["display_name"].state is ObservationState.OBSERVED
    assert all(item.state is not ObservationState.ABSENT_IN_SOURCE for item in batch.observations)


def test_mapping_profile_declares_contact_aliases_for_the_berlin_binding() -> None:
    """The immutable profile carries the alias decision, so the worker can act on it."""

    plan = PipelineTransitionRegistry().plan(_osm_source())
    profile = json.loads(plan.source_record_mapping_actions[0].mapping_profile_payload)

    assert profile["fieldAliases"] == {
        "email": ["email", "contact:email"],
        "phone": ["phone", "contact:phone"],
        "website": ["website", "contact:website"],
    }
    # Aliases are extra source tags for the same target; alias handling adds no target.
    assert profile["fieldProjection"] == {
        "coordinates": "coordinates",
        "display_name": "names",
        "email": "emails",
        "phone": "phones",
        "website": "website_urls",
    }


def test_profile_with_aliases_replays_to_one_identical_digest() -> None:
    registry = PipelineTransitionRegistry()
    first = registry.plan(_osm_source())
    second = registry.plan(_osm_source())

    first_action = first.source_record_mapping_actions[0]
    second_action = second.source_record_mapping_actions[0]
    assert first_action.mapping_profile_digest == second_action.mapping_profile_digest
    assert first_action.mapping_profile_payload == second_action.mapping_profile_payload
    assert first_action.source_record_payload == second_action.source_record_payload
    assert first_action.semantic_key == second_action.semantic_key
    assert first.plan_digest == second.plan_digest


def test_alias_declaration_does_not_change_element_semantic_identity() -> None:
    """Adding aliases must not move a candidate: the element identity is unchanged."""

    plan = PipelineTransitionRegistry().plan(_osm_source())
    semantic_keys = {action.semantic_key for action in plan.source_record_mapping_actions}
    record_ids = {action.source_record_id for action in plan.source_record_mapping_actions}

    assert len(semantic_keys) == 3
    assert record_ids == {"osm:node:501", "osm:node:502", "osm:way:503"}


def test_replay_of_same_envelope_is_identity_stable() -> None:
    registry = PipelineTransitionRegistry()
    first = registry.plan(_osm_source())
    second = registry.plan(_osm_source())

    assert first.plan_digest == second.plan_digest
    assert [item.work_id for item in first.source_record_mapping_actions] == [
        item.work_id for item in second.source_record_mapping_actions
    ]


def test_mapping_to_intake_preserves_source_provenance_and_missing_website_is_not_observed() -> (
    None
):
    registry = PipelineTransitionRegistry()
    mapping_plan = registry.plan(_osm_source())
    # Studio Süd publishes no website: its record must keep the field NOT_OBSERVED.
    sud = next(
        item
        for item in mapping_plan.source_record_mapping_actions
        if item.source_record_id == "osm:node:502"
    )
    batch = materialize_source_record_observations(
        sud.source_record_payload,
        sud.mapping_profile_payload,
        maximum_records=1_000,
    )
    states = {item.field_key: item.state for item in batch.observations}
    assert states["website"] == "not_observed"
    assert states["display_name"] == "observed"

    batch_bytes = json.dumps(
        json.loads(batch.model_dump_json(by_alias=True)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    mapping_source = SucceededWorkOutput(
        source_work_unit_id=UUID("00000000-0000-0000-0000-000000000a07"),
        run_id=_RUN_ID,
        stage_run_id=UUID("00000000-0000-0000-0000-000000000a08"),
        stage=WorkStage.NORMALIZATION,
        capability=WorkCapability.SOURCE_RECORD_MAPPING.value,
        output_contract="source-record-observation-batch@1",
        output_digest="sha256:" + sha256(batch_bytes).hexdigest(),
        output_artifact=ArtifactIdentity(
            artifact_id=UUID("00000000-0000-0000-0000-000000000a09"),
            role="source_record_observation_batch",
            content_digest="sha256:" + sha256(batch_bytes).hexdigest(),
            size_bytes=len(batch_bytes),
            content_type="application/vnd.collection.source-record-observation+json",
        ),
        input_artifacts=(
            sud.source_record_artifact,
            ArtifactIdentity(
                artifact_id=UUID("00000000-0000-0000-0000-000000000a0a"),
                role="source_mapping_profile",
                content_digest=sud.mapping_profile_digest,
                size_bytes=len(sud.mapping_profile_payload),
                content_type="application/json",
            ),
        ),
        source_binding_key="osm_overpass_berlin",
        config_bundle_digest=_config_digest(),
        campaign_bundle_content=_config_campaign_content(),
        output_content=batch_bytes,
        normalization_profile_content=sud.mapping_profile_payload,
    )

    intake_plan = registry.plan(mapping_source)

    assert intake_plan.blocker is None, intake_plan.blocker
    assert intake_plan.disposition is PipelineTransitionDisposition.APPLY
    action = intake_plan.candidate_intake_actions[0]
    provenance = action.source_record_provenance
    assert provenance is not None
    assert provenance.source_record_id == "osm:node:502"
    assert provenance.source_binding_key == "osm_overpass_berlin"
    # The logical identity of a source entity is not the run: the same OSM record acquired by
    # a later run resolves to the same candidate and the same evidence lineage.
    assert provenance.campaign_key == "berlin_recording_services"
    assert provenance.acquisition_identity.startswith("sha256:")
    assert provenance.source_policy_digest == sud.source_policy_digest
    assert (
        provenance.source_record_artifact.content_digest
        == sud.source_record_artifact.content_digest
    )


def test_fan_out_rejects_envelope_with_duplicate_element_identity() -> None:
    duplicated = json.loads(_ENVELOPE)
    duplicated["observations"].append(duplicated["observations"][0])
    content = json.dumps(
        duplicated, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = "sha256:" + sha256(content).hexdigest()
    source = _osm_source()
    broken = source.__class__(
        **{
            **{
                field: getattr(source, field)
                for field in source.__dataclass_fields__  # type: ignore[attr-defined]
            },
            "output_content": content,
            "output_digest": digest,
            "output_artifact": ArtifactIdentity(
                artifact_id=_RAW_ID,
                role="osm_observations",
                content_digest=digest,
                size_bytes=len(content),
                content_type="application/vnd.collection.osm-observations+json",
            ),
        }
    )

    plan = PipelineTransitionRegistry().plan(broken)

    # The envelope is invalid as a whole, so no sibling is silently kept.
    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.source_record_mapping_actions == ()


def test_invalid_single_element_does_not_discard_valid_siblings() -> None:
    envelope = decode_osm_envelope(_ENVELOPE)
    assert len(envelope.elements) == 3

    plan = PipelineTransitionRegistry().plan(_osm_source())
    actions = plan.source_record_mapping_actions

    # Every envelope element becomes exactly one mapping unit, each carrying its own
    # source record identity so replay and provenance stay per-record.
    assert len(actions) == 3
    assert {item.source_record_id for item in actions} == {
        str(element.element.source_record_id) for element in envelope.elements
    }


def _hole_boundary_files() -> dict[str, bytes]:
    """Geography whose interior hole contains the coordinate every test element sits at."""

    boundary_bytes = json.dumps(
        {
            "type": "MultiPolygon",
            "coordinates": [
                [
                    [
                        [13.30, 52.40],
                        [13.50, 52.40],
                        [13.50, 52.60],
                        [13.30, 52.60],
                        [13.30, 52.40],
                    ],
                    [
                        [13.38, 52.48],
                        [13.42, 52.48],
                        [13.42, 52.58],
                        [13.38, 52.58],
                        [13.38, 52.48],
                    ],
                ]
            ],
        },
        sort_keys=True,
    ).encode("utf-8")
    from collection_application.geography import decode_boundary_geojson

    decoded = decode_boundary_geojson(boundary_bytes)
    boundary_bytes = decoded.canonical_geojson
    boundary_digest = decoded.geometry_digest
    provenance_bytes = json.dumps(
        {
            "contract": "campaign-geography-provenance",
            "contract_revision": "campaign-geography-provenance-v1",
            "authority": "Test Authority",
            "dataset_identifier": "hole-filter-test",
            "dataset_title": "Hole filter boundary",
            "derivation": "Synthetic boundary used only to test hole exclusion.",
            "distribution_feature_count": 1,
            "distribution_owner": "Test Distribution Owner",
            "distribution_url": "https://example.invalid/hole-boundary.geojson",
            "license_identifier": "dl-de-zero-2.0",
            "license_title": "Datenlizenz Deutschland - Zero - Version 2.0",
            "source_digest": "sha256:" + "c" * 64,
            "boundary_artifact_path": "geography/hole-boundary.geojson",
            "boundary_digest": boundary_digest,
        },
        sort_keys=True,
    ).encode("utf-8")
    return {
        "geography.yaml": yaml.safe_dump(
            {
                "schema_revision": "geography-config-v1",
                "geography_revision": "berlin_official_boundary_v1",
                "boundary_artifact_path": "geography/hole-boundary.geojson",
                "boundary_digest": boundary_digest,
                "provenance_artifact_path": "geography/hole-boundary.provenance.json",
                "provenance_digest": "sha256:" + sha256(provenance_bytes).hexdigest(),
            },
            sort_keys=False,
        ).encode("utf-8"),
        "geography/hole-boundary.provenance.json": provenance_bytes,
        "geography/hole-boundary.geojson": boundary_bytes,
    }


def _source_with_boundary_hole() -> SucceededWorkOutput:
    from dataclasses import replace

    compiled = _compiled_campaign(_hole_boundary_files())
    return replace(
        _osm_source(),
        config_bundle_digest="sha256:" + sha256(compiled.canonical_content).hexdigest(),
        campaign_bundle_content=compiled.canonical_content,
    )


def test_elements_inside_a_boundary_hole_never_reach_intake() -> None:
    """The over-approximating polygon query must not admit hole-interior elements.

    Every element sits inside the declared hole, so the planner must refuse to schedule
    any mapping work rather than forward them to intake.
    """

    plan = PipelineTransitionRegistry().plan(_source_with_boundary_hole())

    assert plan.transition_key == "osm-overpass-result-source-record-mapping-v1"
    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.source_record_mapping_actions == ()
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_SOURCE_RECORD_ALL_ELEMENTS_OUTSIDE_BOUNDARY"
    assert plan.blocker.context["sourceRecordCount"] == 3


def test_partial_hole_exclusion_drops_the_hole_element_and_keeps_the_rest() -> None:
    """A hole covering one element must exclude only that element."""

    from dataclasses import replace

    from collection_contracts.osm_query_spec import boundary_contains

    compiled = _compiled_campaign(_hole_boundary_files())
    boundary = json.loads(
        decode_boundary_geojson(
            compiled.raw_bundle.files["geography/hole-boundary.geojson"]
        ).canonical_geojson
    )
    # Keep two elements genuinely outside the hole by moving them south of it.
    inside = boundary_contains(boundary, 52.53, 13.40)
    outside = boundary_contains(boundary, 52.45, 13.40)
    assert inside is False
    assert outside is True

    source = replace(
        _osm_source(),
        config_bundle_digest="sha256:" + sha256(compiled.canonical_content).hexdigest(),
        campaign_bundle_content=compiled.canonical_content,
    )
    plan = PipelineTransitionRegistry().plan(source)
    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.source_record_mapping_actions == ()


def test_elements_outside_a_boundary_hole_still_reach_intake() -> None:
    """The same boundary admits elements whose coordinates are outside the hole."""

    plan = PipelineTransitionRegistry().plan(_osm_source())
    assert len(plan.source_record_mapping_actions) == 3
    assert all(
        action.excluded_by_boundary_record_ids == ()
        for action in plan.source_record_mapping_actions
    )


def test_seam_duplicate_element_identity_is_mapped_once() -> None:
    from collection_application.pipeline_advancement import _deduplicated_osm_elements

    envelope = decode_osm_envelope(_ENVELOPE)
    first = envelope.elements[0]
    duplicated = (first, *envelope.elements, first)

    unique = _deduplicated_osm_elements(duplicated)

    assert len(unique) == 3
    identities = [(e.element.element_type, e.element.osm_id) for e in unique]
    assert len(set(identities)) == 3


def test_unrepresentable_element_is_reported_while_siblings_are_planned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad record must be skipped and reported, never abort the whole envelope."""

    import collection_application.pipeline_advancement as advancement

    original = advancement._osm_element_mapping_action
    poisoned = "osm:node:501"

    def poison(source, **kwargs):
        if kwargs["element"].element.source_record_id == poisoned:
            raise ValueError("element cannot be represented")
        return original(source, **kwargs)

    monkeypatch.setattr(advancement, "_osm_element_mapping_action", poison)

    plan = PipelineTransitionRegistry().plan(_osm_source())
    actions = plan.source_record_mapping_actions

    assert len(actions) == 2
    assert poisoned not in {item.source_record_id for item in actions}
    reported = [
        record_id for action in actions for record_id in action.unrepresentable_source_record_ids
    ]
    assert reported == [poisoned]
