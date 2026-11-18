from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest
import yaml

from collection_application import CampaignSnapshotService, RawCampaignBundle
from collection_application.campaign_snapshot_service import CompiledCampaignBundle
from collection_application.canonicalization import sha256_json
from collection_application.pipeline_advancement import (
    ApplyPipelineAdvancement,
    ArtifactIdentity,
    ClaimPipelineAdvancement,
    PipelineAdvancementConflict,
    PipelineAdvancementLease,
    PipelineAdvancementService,
    PipelineAdvancementState,
    PipelineAdvancementStatus,
    PipelineBlocker,
    PipelineTransitionDisposition,
    PipelineTransitionRegistry,
    SucceededWorkOutput,
    _transition_plan_digest,
)
from collection_contracts import (
    ExtractionRequest,
    ManualImportFormat,
    OwnerContextError,
    decode_website_route_context,
)
from collection_domain import WorkStage
from manual_import_core import (
    build_manual_import_plan,
    canonical_manual_import_plan_json,
    canonical_manual_import_record_json,
    materialize_manual_import_record,
)
from official_http_contracts import HttpAcquisitionManifest, ResponseHeader

_RUN_ID = UUID("00000000-0000-0000-0000-000000000201")
_STAGE_RUN_ID = UUID("00000000-0000-0000-0000-000000000202")
_WORK_ID = UUID("00000000-0000-0000-0000-000000000203")
_OUTPUT_ID = UUID("00000000-0000-0000-0000-000000000204")
_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000205")
_OTHER_ID = UUID("00000000-0000-0000-0000-000000000206")
_ADVANCEMENT_ID = UUID("00000000-0000-0000-0000-000000000207")
_LEASE_ID = UUID("00000000-0000-0000-0000-000000000208")
_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64


def _artifact(
    artifact_id: UUID,
    role: str,
    digest: str = _DIGEST_A,
) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_id=artifact_id,
        role=role,
        content_digest=digest,
        size_bytes=123,
        content_type="application/json",
    )


def _manual_plan_source(
    *,
    output_role: str = "manual_import_plan",
    output_digest: str = _DIGEST_B,
    input_artifacts: tuple[ArtifactIdentity, ...] | None = None,
) -> SucceededWorkOutput:
    return SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_STAGE_RUN_ID,
        stage=WorkStage.DISCOVERY,
        capability="manual_import",
        output_contract="manual-import-plan@1",
        output_digest=output_digest,
        output_artifact=_artifact(_OUTPUT_ID, output_role, output_digest),
        input_artifacts=(
            (_artifact(_SOURCE_ID, "manual_source:csv:partial"),)
            if input_artifacts is None
            else input_artifacts
        ),
    )


def _status() -> PipelineAdvancementStatus:
    return PipelineAdvancementStatus(
        advancement_id=_ADVANCEMENT_ID,
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        state=PipelineAdvancementState.PENDING,
        transition_key="manual-import-plan-admission",
        transition_plan_digest=_DIGEST_C,
        revision=0,
        attempt_count=0,
        result_digest=None,
        blocker=None,
        created_at_utc=_NOW,
        updated_at_utc=_NOW,
    )


def test_manual_import_plan_routes_to_exact_admission_owner() -> None:
    plan = PipelineTransitionRegistry().plan(_manual_plan_source())

    assert plan.transition_key == "manual-import-plan-admission"
    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert plan.blocker is None
    assert plan.plan_digest.startswith("sha256:")


def test_transition_digest_is_order_independent_but_evidence_bound() -> None:
    source = _artifact(_SOURCE_ID, "manual_source:csv:partial")
    other = _artifact(_OTHER_ID, "manual_import_schema", _DIGEST_C)
    registry = PipelineTransitionRegistry()

    first = registry.plan(_manual_plan_source(input_artifacts=(source, other)))
    reordered = registry.plan(_manual_plan_source(input_artifacts=(other, source)))
    changed = registry.plan(
        _manual_plan_source(
            input_artifacts=(
                _artifact(_SOURCE_ID, "manual_source:csv:partial", _DIGEST_B),
                other,
            )
        )
    )

    assert first.plan_digest == reordered.plan_digest
    assert first.plan_digest != changed.plan_digest


def test_transition_digest_preserves_legacy_and_binds_website_source_provenance() -> None:
    source = _manual_plan_source()
    registry = PipelineTransitionRegistry()

    legacy_first = registry.plan(replace(source, source_binding_key="seed"))
    legacy_changed = registry.plan(replace(source, source_binding_key="other"))
    assert legacy_first.plan_digest == legacy_changed.plan_digest

    first = _transition_plan_digest(
        source,
        transition_key="manual-record-website-routing-v1",
        disposition=PipelineTransitionDisposition.APPLY,
        blocker=None,
    )
    changed = _transition_plan_digest(
        replace(source, source_binding_key="other"),
        transition_key="manual-record-website-routing-v1",
        disposition=PipelineTransitionDisposition.APPLY,
        blocker=None,
    )
    assert first != changed


def test_manual_import_plan_requires_one_canonical_source_binding() -> None:
    registry = PipelineTransitionRegistry()

    missing = registry.plan(_manual_plan_source(input_artifacts=()))
    conflicting = registry.plan(
        _manual_plan_source(
            input_artifacts=(
                _artifact(_SOURCE_ID, "manual_source:csv:partial"),
                _artifact(_OTHER_ID, "manual_import_source:json:partial"),
            )
        )
    )

    assert missing.disposition is PipelineTransitionDisposition.BLOCK
    assert missing.blocker is not None
    assert missing.blocker.code == "PIPELINE_INPUT_ARTIFACT_MISSING"
    assert conflicting.blocker is not None
    assert conflicting.blocker.code == "PIPELINE_INPUT_ARTIFACT_CONFLICT"


def test_manual_record_remains_explicitly_blocked_without_source_owner() -> None:
    source = SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_STAGE_RUN_ID,
        stage=WorkStage.DISCOVERY,
        capability="manual_record",
        output_contract="manual-import-record@1",
        output_digest=_DIGEST_B,
        output_artifact=_artifact(_OUTPUT_ID, "manual_import_record", _DIGEST_B),
        input_artifacts=(_artifact(_SOURCE_ID, "manual_source:csv:partial"),),
    )

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "MANUAL_RECORD_DOWNSTREAM_SOURCE_UNAVAILABLE"


def _routed_campaign_snapshot(
    *,
    route_source_binding: str = "manual_seed_import",
    seed_website_url: str | None = None,
) -> tuple[bytes, CompiledCampaignBundle]:
    campaign_root = Path(__file__).resolve().parents[3] / "campaigns" / "berlin_recording_services"
    files = {
        path.relative_to(campaign_root).as_posix(): path.read_bytes()
        for path in campaign_root.rglob("*")
        if path.is_file()
    }
    if seed_website_url is not None:
        files["discovery/manual_seeds.csv"] = files["discovery/manual_seeds.csv"].replace(
            b"https://hansastudios.de/", seed_website_url.encode("utf-8"), 1
        )
    campaign = yaml.safe_load(files["campaign.yaml"])
    campaign["enabled_source_bindings"].append("official_website")
    if route_source_binding == "manual_seed_import_secondary":
        campaign["enabled_source_bindings"].append("manual_seed_import_secondary")
    files["campaign.yaml"] = yaml.safe_dump(campaign, sort_keys=False).encode()

    bindings = yaml.safe_load(files["source_bindings.yaml"])
    if route_source_binding == "manual_seed_import_secondary":
        secondary_manual_binding = dict(
            next(item for item in bindings["items"] if item["key"] == "manual_seed_import")
        )
        secondary_manual_binding["key"] = "manual_seed_import_secondary"
        bindings["items"].append(secondary_manual_binding)
    bindings["items"].append(
        {
            "key": "official_website",
            "source_key": "official_website",
            "connector_key": "organization_website",
            "source_policy_key": "website_test_policy",
            "capability": "http_fetch",
            "seed_provider": {"kind": "not_applicable"},
            "crawl_budget": {
                "state": "defined",
                "max_work_units": 20,
                "max_total_bytes": 10_000_000,
            },
            "schedule_enabled": False,
            "extraction_profile": "website-test-profile-v1",
        }
    )
    bindings["seed_routing"] = {
        "state": "configured",
        "routes": [
            {
                "key": "manual_website",
                "source_binding": route_source_binding,
                "source_field": "website",
                "target_binding": "official_website",
                "route_kind": "official_website_page",
                "selection": "exclusive",
                "page_interests": [
                    {"key": "contact", "tokens": ["contact", "kontakt"], "priority": 80}
                ],
            }
        ],
    }
    files["source_bindings.yaml"] = yaml.safe_dump(bindings, sort_keys=False).encode()
    policy = {
        "schema_revision": "source-policy-v1",
        "policy_key": "website_test_policy",
        "policy_revision": "website-test-policy-v1",
        "source_key": "official_website",
        "source_type": "official_website",
        "legal_status": "approved",
        "terms_review": {
            "state": "reviewed",
            "terms_url": "https://hansastudios.de/terms",
            "reviewed_at": "2026-01-01",
            "expires_at": "2099-01-01",
            "reviewer": "test-reviewer",
        },
        "access": {
            "kind": "network",
            "allowed_methods": ["GET"],
            "allowed_host_patterns": ["hansastudios.de"],
            "max_requests_per_second": 1,
            "max_concurrency": 1,
            "connect_timeout_seconds": 10,
            "read_timeout_seconds": 20,
            "max_encoded_bytes": 1_000_000,
            "max_decoded_bytes": 2_000_000,
            "content_type_allowlist": ["text/plain", "text/html", "application/xml"],
            "user_agent": {
                "state": "configured",
                "value": "CollectorTest/1.0 (+https://hansastudios.de/contact)",
            },
            "redirect_limit": 3,
        },
        "storage_class": "collector-structured-evidence-v1",
        "retention_days": 30,
        "allowed_fields": ["website"],
        "prohibited_fields": ["review_text"],
        "attribution_required": False,
        "browser": "forbidden",
        "screenshots": "forbidden",
        "retry_budget": 2,
        "stop_on_status_codes": [401, 403, 429],
        "reviewer": "test-reviewer",
        "approver": "test-approver",
    }
    files["source_policies/website_test_policy.yaml"] = yaml.safe_dump(
        policy, sort_keys=False
    ).encode()

    class Source:
        def read(self, campaign_key: str, correlation_id: str) -> RawCampaignBundle:
            assert campaign_key == "berlin_recording_services"
            assert correlation_id == "website-route-test"
            return RawCampaignBundle(campaign_key, MappingProxyType(files))

    compiled = CampaignSnapshotService(Source()).compile(
        "berlin_recording_services", "website-route-test"
    )
    return compiled.canonical_content, compiled


def _manual_record_for_snapshot(compiled: CompiledCampaignBundle):
    source_bytes = compiled.raw_bundle.files["discovery/manual_seeds.csv"]
    manual_plan = build_manual_import_plan(source_bytes, format=ManualImportFormat.CSV)
    plan_bytes = canonical_manual_import_plan_json(manual_plan).encode("utf-8")
    record_document = materialize_manual_import_record(
        plan_bytes,
        source_artifact_role="manual_source:csv:atomic",
        plan_record_position=0,
    )
    record_bytes = canonical_manual_import_record_json(record_document).encode("utf-8")
    record_digest = "sha256:" + hashlib.sha256(record_bytes).hexdigest()
    source_digest = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
    source = SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_STAGE_RUN_ID,
        stage=WorkStage.DISCOVERY,
        capability="manual_record",
        output_contract="manual-import-record@1",
        output_digest=record_document.content_digest,
        output_artifact=ArtifactIdentity(
            artifact_id=_OUTPUT_ID,
            role="manual_import_record",
            content_digest=record_digest,
            size_bytes=len(record_bytes),
            content_type="application/vnd.collection.manual-import-record+json",
        ),
        input_artifacts=(
            ArtifactIdentity(
                artifact_id=_SOURCE_ID,
                role="manual_source:csv:atomic",
                content_digest=source_digest,
                size_bytes=len(source_bytes),
                content_type="text/csv",
            ),
        ),
        source_binding_key="manual_seed_import",
        config_bundle_digest=compiled.snapshot.bundle_digest,
        campaign_bundle_content=compiled.canonical_content,
        output_content=record_bytes,
    )
    return source, record_document


def _official_http_output(
    compiled: CompiledCampaignBundle,
    *,
    request_kind: str = "robots",
    robots_allowed: bool = True,
    unchanged: bool = False,
) -> SucceededWorkOutput:
    from collection_application.pipeline_advancement import WebsiteRouteAction

    manual_source, _ = _manual_record_for_snapshot(compiled)
    route_plan = PipelineTransitionRegistry().plan(manual_source)
    action = route_plan.website_actions[0]
    assert isinstance(action, WebsiteRouteAction)
    lineage_artifact = action.manual_record_lineage_artifact
    assert lineage_artifact is not None
    context = decode_website_route_context(action.context_payload)
    request_document = json.loads(action.request_payload)
    request_bytes = action.request_payload
    context_bytes = action.context_payload
    if request_kind != "robots":
        request_document.update(
            requestKind=request_kind,
            url=str(context.source_url),
            robotsAllowed=True,
            robotsArtifactId="00000000-0000-0000-0000-000000000308",
            robotsDecisionDigest=_DIGEST_B,
            robotsManifestDigest=_DIGEST_A,
            pageInterests=[item.model_dump(mode="json") for item in context.page_interests],
        )
        request_bytes = json.dumps(request_document, sort_keys=True, separators=(",", ":")).encode()
        context = context.model_copy(
            update={"request_digest": "sha256:" + hashlib.sha256(request_bytes).hexdigest()}
        )
        context_bytes = json.dumps(
            context.model_dump(mode="json", by_alias=True), sort_keys=True, separators=(",", ":")
        ).encode()
    manifest = HttpAcquisitionManifest(
        request_id=context.request_id,
        source_key=context.source_key,
        source_policy_digest=context.source_policy_digest,
        request_kind=request_kind,  # type: ignore[arg-type]
        requested_url=(
            "https://hansastudios.de/robots.txt"
            if request_kind == "robots"
            else str(context.source_url)
        ),
        final_url=(
            "https://hansastudios.de/robots.txt"
            if request_kind == "robots"
            else str(context.source_url)
        ),
        outcome="unchanged" if unchanged else "fetched",
        status_code=304 if unchanged else 200,
        response_headers=(
            ()
            if unchanged
            else (
                ResponseHeader(
                    name="content-type",
                    value="text/plain" if request_kind == "robots" else "text/html; charset=utf-8",
                ),
            )
        ),
        remote_ip_address="93.184.216.34",
        encoded_size_bytes=0 if unchanged else 128,
        decoded_size_bytes=0 if unchanged else 128,
        observed_at_utc=_NOW,
        raw_artifact_digest=None if unchanged else _DIGEST_C,
        reused_artifact_id=(UUID("00000000-0000-0000-0000-00000000030e") if unchanged else None),
        reused_content_digest=_DIGEST_C if unchanged else None,
        robots_allowed=robots_allowed if request_kind == "robots" else True,
        robots_artifact_id=(
            None if request_kind == "robots" else UUID("00000000-0000-0000-0000-000000000308")
        ),
        robots_decision_digest=_DIGEST_B,
        robots_target_url=str(context.source_url),
    )
    manifest_bytes = manifest.to_bytes()
    return SucceededWorkOutput(
        source_work_unit_id=UUID("00000000-0000-0000-0000-000000000309"),
        run_id=manual_source.run_id,
        stage_run_id=UUID("00000000-0000-0000-0000-00000000030a"),
        stage=WorkStage.ACQUISITION,
        capability="http_fetch",
        output_contract="official-http-acquisition@1",
        output_digest="sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        output_artifact=ArtifactIdentity(
            artifact_id=UUID("00000000-0000-0000-0000-00000000030b"),
            role="http_acquisition_manifest",
            content_digest="sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
            size_bytes=len(manifest_bytes),
            content_type="application/vnd.collection.official-http-acquisition+json",
        ),
        input_artifacts=(
            ArtifactIdentity(
                artifact_id=UUID("00000000-0000-0000-0000-00000000030c"),
                role="http_request",
                content_digest="sha256:" + hashlib.sha256(request_bytes).hexdigest(),
                size_bytes=len(request_bytes),
                content_type="application/vnd.collection.official-http-request+json",
            ),
            ArtifactIdentity(
                artifact_id=UUID("00000000-0000-0000-0000-00000000030d"),
                role="website_route_context",
                content_digest="sha256:" + hashlib.sha256(context_bytes).hexdigest(),
                size_bytes=len(context_bytes),
                content_type="application/vnd.collection.website-route-context+json",
            ),
            lineage_artifact,
        )
        + (
            (
                ArtifactIdentity(
                    artifact_id=UUID("00000000-0000-0000-0000-000000000308"),
                    role="robots_decision",
                    content_digest=_DIGEST_A,
                    size_bytes=256,
                    content_type="application/vnd.collection.official-http-acquisition+json",
                ),
            )
            if request_kind == "page"
            else ()
        )
        + (
            (
                ArtifactIdentity(
                    artifact_id=UUID("00000000-0000-0000-0000-00000000030e"),
                    role="previous_raw_artifact",
                    content_digest=_DIGEST_C,
                    size_bytes=128,
                    content_type="text/html; charset=utf-8",
                ),
            )
            if unchanged
            else ()
        ),
        related_output_artifacts=(
            ()
            if unchanged
            else (
                ArtifactIdentity(
                    artifact_id=UUID("00000000-0000-0000-0000-00000000030e"),
                    role="http_raw_response",
                    content_digest=_DIGEST_C,
                    size_bytes=128,
                    content_type="text/plain"
                    if request_kind == "robots"
                    else "text/html; charset=utf-8",
                ),
            )
        ),
        source_binding_key=context.target_binding_key,
        config_bundle_digest=compiled.snapshot.bundle_digest,
        campaign_bundle_content=compiled.canonical_content,
        output_content=manifest_bytes,
        http_request_content=request_bytes,
        website_route_context_content=context_bytes,
        manual_record_lineage_content=action.manual_record_lineage_content,
    )


def test_website_robots_request_keeps_exact_page_context_for_successor() -> None:
    from official_http import decode_http_request

    _, compiled = _routed_campaign_snapshot()
    source, record = _manual_record_for_snapshot(compiled)

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert len(plan.website_actions) == 1
    action = plan.website_actions[0]
    request = decode_http_request(action.request_payload)
    context = decode_website_route_context(action.context_payload)
    assert request.request_kind == "robots"
    assert request.url == "https://hansastudios.de/robots.txt"
    assert request.page_interests == ()
    assert str(context.source_url) == str(record.record.website)
    assert tuple(context.page_interests[0].tokens) == ("contact", "kontakt")
    assert context.page_interests[0].key == "contact"
    assert context.record_digest == record.content_digest
    assert context.campaign_bundle_digest == compiled.snapshot.bundle_digest
    assert context.request_id == request.request_id
    assert context.request_digest == action.input_digest

    duplicate_key_payload = action.context_payload.replace(
        b'"routeKey":"manual_website"',
        b'"routeKey":"manual_website","routeKey":"manual_website"',
        1,
    )
    with pytest.raises(ValueError, match="owner contract"):
        decode_website_route_context(duplicate_key_payload)


def test_official_http_transition_accepts_serialized_robots_manifest_and_routes_page() -> None:
    _, compiled = _routed_campaign_snapshot()
    source = _official_http_output(compiled)

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert len(plan.website_actions) == 1
    action = plan.website_actions[0]
    request = json.loads(action.request_payload)
    assert request["requestKind"] == "page"
    context = decode_website_route_context(source.website_route_context_content or b"")
    assert request["url"] == str(context.source_url)
    assert request["robotsAllowed"] is True
    assert action.robots_decision_artifact_id == source.output_artifact.artifact_id
    assert action.semantic_key


def test_official_http_page_result_plans_extraction_from_raw_artifact() -> None:
    _, compiled = _routed_campaign_snapshot()
    source = _official_http_output(compiled, request_kind="page")

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert plan.website_actions == ()
    assert len(plan.extraction_actions) == 1
    action = plan.extraction_actions[0]
    request = ExtractionRequest.model_validate_json(action.request_payload)
    assert request.raw_artifact_digest == _DIGEST_C
    assert action.raw_artifact_id == source.related_output_artifacts[0].artifact_id
    assert (
        request.source_record_id
        == decode_website_route_context(source.website_route_context_content or b"").record_digest
    )


def test_official_http_unchanged_page_plans_extraction_from_exact_previous_artifact() -> None:
    _, compiled = _routed_campaign_snapshot()
    source = _official_http_output(compiled, request_kind="page", unchanged=True)

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert len(plan.extraction_actions) == 1
    previous = next(item for item in source.input_artifacts if item.role == "previous_raw_artifact")
    action = plan.extraction_actions[0]
    request = ExtractionRequest.model_validate_json(action.request_payload)
    assert action.raw_artifact_id == previous.artifact_id
    assert request.raw_artifact_digest == previous.content_digest == _DIGEST_C


def test_official_http_unchanged_page_rejects_mismatched_previous_artifact() -> None:
    _, compiled = _routed_campaign_snapshot()
    source = _official_http_output(compiled, request_kind="page", unchanged=True)
    previous = next(item for item in source.input_artifacts if item.role == "previous_raw_artifact")
    altered_previous = replace(previous, content_digest=_DIGEST_B)
    source = replace(
        source,
        input_artifacts=tuple(
            altered_previous if item is previous else item for item in source.input_artifacts
        ),
    )

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_PAGE_EXTRACTION_IDENTITY_MISMATCH"
    assert plan.extraction_actions == ()


def test_official_http_transition_blocks_disallowed_seed_without_page_work() -> None:
    _, compiled = _routed_campaign_snapshot()
    source = _official_http_output(compiled, robots_allowed=False)

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_WEBSITE_ROBOTS_DISALLOWED"
    assert plan.website_actions == ()


def test_official_http_sitemap_result_returns_exact_unsupported_kind() -> None:
    request_kind = "sitemap"
    _, compiled = _routed_campaign_snapshot()
    source = _official_http_output(compiled, request_kind=request_kind)

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.transition_key == f"official-http-{request_kind}-routing-v1"
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_TRANSITION_UNSUPPORTED"
    assert plan.blocker.context["requestKind"] == request_kind


def test_website_route_fails_closed_for_wrong_binding_provenance() -> None:
    _, compiled = _routed_campaign_snapshot()
    source, _ = _manual_record_for_snapshot(compiled)

    plan = PipelineTransitionRegistry().plan(replace(source, source_binding_key="unknown_seed"))

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "MANUAL_RECORD_ROUTE_PROVENANCE_MISMATCH"


def test_website_route_missing_for_manual_seed_binding_blocks_without_work() -> None:
    _, compiled = _routed_campaign_snapshot(route_source_binding="manual_seed_import_secondary")
    source, _ = _manual_record_for_snapshot(compiled)

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.transition_key == "manual-record-website-routing-v1"
    assert plan.blocker is not None
    assert plan.blocker.code == "MANUAL_RECORD_ROUTE_UNAVAILABLE"
    assert plan.blocker.context["sourceBindingKey"] == "manual_seed_import"
    assert plan.website_actions == ()


def test_non_fan_out_route_ambiguity_blocks_without_work() -> None:
    _, compiled = _routed_campaign_snapshot()
    source, _ = _manual_record_for_snapshot(compiled)
    snapshot = json.loads(compiled.canonical_content)
    bindings_component = next(
        item for item in snapshot["components"] if item["path"] == "source_bindings.yaml"
    )
    routes = bindings_component["content"]["seed_routing"]["routes"]
    second_route = dict(routes[0])
    second_route["key"] = "manual_website_secondary"
    routes.append(second_route)
    bindings_component["digest"] = sha256_json(bindings_component["content"])
    snapshot_content = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    source = replace(
        source,
        config_bundle_digest="sha256:" + hashlib.sha256(snapshot_content).hexdigest(),
        campaign_bundle_content=snapshot_content,
    )

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "MANUAL_RECORD_ROUTE_INPUT_INVALID"
    assert plan.blocker.context["reason"] == "ValidationError"
    assert plan.website_actions == ()


def _replace_snapshot_component_content(
    source: SucceededWorkOutput, component_path: str, content: dict[str, object]
) -> SucceededWorkOutput:
    assert source.campaign_bundle_content is not None
    snapshot = json.loads(source.campaign_bundle_content)
    component = next(item for item in snapshot["components"] if item["path"] == component_path)
    component["content"] = content
    component["digest"] = sha256_json(content)
    snapshot_content = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return replace(
        source,
        config_bundle_digest="sha256:" + hashlib.sha256(snapshot_content).hexdigest(),
        campaign_bundle_content=snapshot_content,
    )


def test_route_with_manual_access_policy_in_snapshot_blocks_without_work() -> None:
    _, compiled = _routed_campaign_snapshot()
    source, _ = _manual_record_for_snapshot(compiled)
    snapshot = json.loads(source.campaign_bundle_content or b"")
    policy_component = next(
        item
        for item in snapshot["components"]
        if item["path"] == "source_policies/website_test_policy.yaml"
    )
    policy = dict(policy_component["content"])
    policy["access"] = {
        "kind": "manual",
        "accepted_formats": ["csv"],
        "accepted_encodings": ["utf-8"],
        "max_file_bytes": 1_048_576,
        "partial_mode_allowed": True,
    }
    source = _replace_snapshot_component_content(
        source, "source_policies/website_test_policy.yaml", policy
    )

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "MANUAL_RECORD_ROUTE_POLICY_INVALID"
    assert plan.blocker.context["reason"] == "ValidationError"
    assert plan.website_actions == ()


def test_route_to_unlisted_website_host_blocks_without_work() -> None:
    _, compiled = _routed_campaign_snapshot(seed_website_url="https://unauthorized.example/")
    source, _ = _manual_record_for_snapshot(compiled)

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "MANUAL_RECORD_ROUTE_HOST_FORBIDDEN"
    assert plan.blocker.context["reason"] == "host_not_allowed"
    assert plan.blocker.context["host"] == "unauthorized.example"
    assert plan.website_actions == ()


def test_unknown_transition_is_never_silently_accepted() -> None:
    source = SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_STAGE_RUN_ID,
        stage=WorkStage.ACQUISITION,
        capability="future_capability",
        output_contract="future-output@1",
        output_digest=_DIGEST_B,
        output_artifact=_artifact(_OUTPUT_ID, "future_output", _DIGEST_B),
        input_artifacts=(),
    )

    plan = PipelineTransitionRegistry().plan(source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_TRANSITION_UNSUPPORTED"


def test_invalid_terminal_status_cannot_be_constructed() -> None:
    with pytest.raises(ValueError, match="applied pipeline advancement"):
        PipelineAdvancementStatus(
            advancement_id=_ADVANCEMENT_ID,
            source_work_unit_id=_WORK_ID,
            run_id=_RUN_ID,
            state=PipelineAdvancementState.APPLIED,
            transition_key="manual-import-plan-admission",
            transition_plan_digest=_DIGEST_C,
            revision=1,
            attempt_count=1,
            result_digest=None,
            blocker=None,
            created_at_utc=_NOW,
            updated_at_utc=_NOW,
        )


def test_lease_contract_binds_exact_dagster_execution_and_expiry() -> None:
    lease = PipelineAdvancementLease(
        advancement_id=_ADVANCEMENT_ID,
        source_work_unit_id=_WORK_ID,
        lease_id=_LEASE_ID,
        lease_token="lease-token-1",
        worker_id="pipeline-supervisor-1",
        dagster_execution_id="dagster-run-1",
        dagster_build_id="build-1",
        transition_key="manual-import-plan-admission",
        transition_plan_digest=_DIGEST_C,
        revision=1,
        attempt_number=1,
        issued_at_utc=_NOW,
        expires_at_utc=_NOW + timedelta(minutes=5),
    )

    assert lease.dagster_execution_id == "dagster-run-1"
    assert lease.expires_at_utc > lease.issued_at_utc

    with pytest.raises(ValueError, match="expiry"):
        PipelineAdvancementLease(
            advancement_id=_ADVANCEMENT_ID,
            source_work_unit_id=_WORK_ID,
            lease_id=_LEASE_ID,
            lease_token="lease-token-1",
            worker_id="pipeline-supervisor-1",
            dagster_execution_id="dagster-run-1",
            dagster_build_id="build-1",
            transition_key="manual-import-plan-admission",
            transition_plan_digest=_DIGEST_C,
            revision=1,
            attempt_number=1,
            issued_at_utc=_NOW,
            expires_at_utc=_NOW,
        )


class Port:
    def __init__(self) -> None:
        self.source = None
        self.plan = None

    def register(self, source, plan, *, correlation_id):
        assert correlation_id == "pipeline-test"
        self.source = source
        self.plan = plan
        return _status()

    def claim(self, command):
        assert isinstance(command, ClaimPipelineAdvancement)
        return None

    def apply(self, command):
        assert isinstance(command, ApplyPipelineAdvancement)
        return _status()

    def block(self, command):
        raise AssertionError(command)


def test_service_registers_registry_plan_through_port() -> None:
    port = Port()

    result = PipelineAdvancementService(port).register(
        _manual_plan_source(),
        correlation_id="pipeline-test",
    )

    assert result == _status()
    assert port.source is not None
    assert port.plan is not None
    assert port.plan.disposition is PipelineTransitionDisposition.APPLY


def test_port_conflict_is_exposed_as_owner_context_error() -> None:
    class FailingPort(Port):
        def register(self, source, plan, *, correlation_id):
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_OUTPUT_CONFLICT",
                message="Source output changed.",
                context={"sourceWorkUnitId": str(source.source_work_unit_id)},
                required_action="Reload the exact successful work output.",
            )

    with pytest.raises(OwnerContextError) as captured:
        PipelineAdvancementService(FailingPort()).register(
            _manual_plan_source(),
            correlation_id="pipeline-test",
        )

    assert captured.value.envelope.owner == "PipelineAdvancement"
    assert captured.value.envelope.code == "PIPELINE_SOURCE_OUTPUT_CONFLICT"
    assert captured.value.envelope.correlation_id == "pipeline-test"


def test_blocker_context_is_immutable() -> None:
    blocker = PipelineBlocker(
        owner="PipelineAdvancement",
        code="PIPELINE_TRANSITION_UNSUPPORTED",
        message="Unsupported.",
        required_action="Add an owner.",
        context={"key": "value"},
    )

    with pytest.raises(TypeError):
        blocker.context["key"] = "changed"  # type: ignore[index]
