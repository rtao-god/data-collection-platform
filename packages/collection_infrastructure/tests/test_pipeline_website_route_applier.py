from __future__ import annotations

import hashlib
import json
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from collection_application import ArtifactKind, WorkEngineConflict
from collection_application.canonicalization import sha256_json
from collection_application.pipeline_advancement import (
    ApplyPipelineAdvancement,
    ArtifactIdentity,
    ClaimPipelineAdvancement,
    PipelineAdvancementConflict,
    PipelineAdvancementState,
    PipelineTransitionDisposition,
    PipelineTransitionRegistry,
    SucceededWorkOutput,
)
from collection_contracts import ManualImportFormat, decode_website_route_context
from collection_domain import WorkCapability, WorkStage
from collection_infrastructure.postgres.pipeline_advancement import (
    PostgresPipelineAdvancementRepository,
)
from collection_infrastructure.postgres.website_route_applier import (
    website_route_transition_appliers,
)
from manual_import_core import (
    build_manual_import_plan,
    canonical_manual_import_plan_json,
    canonical_manual_import_record_json,
    materialize_manual_import_record,
)
from official_http_contracts import HttpAcquisitionManifest, ResponseHeader

_RUN_ID = UUID("00000000-0000-0000-0000-000000000501")
_DISCOVERY_STAGE_ID = UUID("00000000-0000-0000-0000-000000000502")
_ACQUISITION_STAGE_ID = UUID("00000000-0000-0000-0000-000000000503")
_EXTRACTION_STAGE_ID = UUID("00000000-0000-0000-0000-000000000551")
_WORK_ID = UUID("00000000-0000-0000-0000-000000000504")
_OUTPUT_ID = UUID("00000000-0000-0000-0000-000000000505")
_SOURCE_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000506")
_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _configured_route_source() -> SucceededWorkOutput:
    campaign = {
        "schema_revision": "campaign-v1",
        "campaign_key": "website_route_test",
        "display_name": "Website route test",
        "locales": ["en"],
        "timezone": "Europe/Berlin",
        "entity_kinds": ["organization"],
        "target_categories": ["recording_service"],
        "geography_revision": "test-geography-v1",
        "enabled_source_bindings": ["manual_seed_import", "official_website"],
        "quality_profile": "test-quality-v1",
        "export_profile": "test-export-v1",
        "readiness": {"state": "ready"},
    }
    bindings = {
        "schema_revision": "source-bindings-v1",
        "items": [
            {
                "key": "manual_seed_import",
                "source_key": "manual_seed",
                "connector_key": "manual_seed",
                "source_policy_key": "manual_seed_policy",
                "capability": "manual_import",
                "seed_provider": {
                    "kind": "file",
                    "path": "discovery/manual.json",
                    "format": "json",
                },
                "crawl_budget": {"state": "not_applicable"},
                "schedule_enabled": False,
                "extraction_profile": "manual-seed-test-v1",
            },
            {
                "key": "official_website",
                "source_key": "official_website",
                "connector_key": "official_http",
                "source_policy_key": "website_policy",
                "capability": "http_fetch",
                "seed_provider": {"kind": "not_applicable"},
                "crawl_budget": {
                    "state": "defined",
                    "max_work_units": 10,
                    "max_total_bytes": 1_000_000,
                },
                "schedule_enabled": False,
                "extraction_profile": "website-test-v1",
            },
        ],
        "seed_routing": {
            "state": "configured",
            "routes": [
                {
                    "key": "manual_website",
                    "source_field": "website",
                    "source_binding": "manual_seed_import",
                    "target_binding": "official_website",
                    "route_kind": "official_website_page",
                    "selection": "exclusive",
                    "page_interests": [
                        {"key": "contact", "tokens": ["contact", "kontakt"], "priority": 80}
                    ],
                }
            ],
        },
    }
    manual_policy = {
        "schema_revision": "source-policy-v1",
        "policy_key": "manual_seed_policy",
        "policy_revision": "manual-seed-test-v1",
        "source_key": "manual_seed",
        "source_type": "manual_import",
        "legal_status": "approved",
        "terms_review": {"state": "not_applicable", "reason": "Operator-provided test data"},
        "access": {
            "kind": "manual",
            "accepted_formats": ["json"],
            "accepted_encodings": ["utf-8"],
            "max_file_bytes": 10_000,
            "partial_mode_allowed": False,
        },
        "storage_class": "test-structured-v1",
        "retention_days": 30,
        "allowed_fields": ["website"],
        "prohibited_fields": ["review_text"],
        "attribution_required": False,
        "browser": "forbidden",
        "screenshots": "forbidden",
        "retry_budget": 0,
        "stop_on_status_codes": [],
        "reviewer": "test-reviewer",
        "approver": "test-approver",
    }
    website_policy = {
        "schema_revision": "source-policy-v1",
        "policy_key": "website_policy",
        "policy_revision": "website-test-v1",
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
            "content_type_allowlist": ["text/plain", "text/html"],
            "user_agent": {
                "state": "configured",
                "value": "CollectorTest/1.0 (+https://hansastudios.de/contact)",
            },
            "redirect_limit": 3,
        },
        "storage_class": "test-raw-v1",
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
    components = [
        {"path": path, "digest": sha256_json(content), "content": content}
        for path, content in sorted(
            {
                "campaign.yaml": campaign,
                "source_bindings.yaml": bindings,
                "source_policies/manual_seed_policy.yaml": manual_policy,
                "source_policies/website_policy.yaml": website_policy,
            }.items()
        )
    ]
    bundle_content = _canonical_json_bytes(
        {"campaignKey": "website_route_test", "components": components}
    )
    bundle_digest = _content_digest(bundle_content)

    raw_source = _canonical_json_bytes(
        [
            {
                "expected_entity_kind": "organization",
                "display_name": "Hansa Test Studios",
                "category": "recording_studio",
                "website": "https://hansastudios.de/start",
                "osm_id": None,
                "reference_urls": [],
                "note": None,
                "provenance": "operator-provided test input",
            }
        ]
    )
    import_plan = build_manual_import_plan(raw_source, format=ManualImportFormat.JSON)
    record_document = materialize_manual_import_record(
        canonical_manual_import_plan_json(import_plan).encode("utf-8"),
        source_artifact_role="manual_source:json:atomic",
        plan_record_position=0,
    )
    output_content = canonical_manual_import_record_json(record_document).encode("utf-8")
    source_digest = _content_digest(raw_source)
    return SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_DISCOVERY_STAGE_ID,
        stage=WorkStage.DISCOVERY,
        capability="manual_record",
        output_contract="manual-import-record@1",
        output_digest=record_document.content_digest,
        output_artifact=ArtifactIdentity(
            artifact_id=_OUTPUT_ID,
            role="manual_import_record",
            content_digest=_content_digest(output_content),
            size_bytes=len(output_content),
            content_type="application/vnd.collection.manual-import-record+json",
        ),
        input_artifacts=(
            ArtifactIdentity(
                artifact_id=_SOURCE_ARTIFACT_ID,
                role="manual_source:json:atomic",
                content_digest=source_digest,
                size_bytes=len(raw_source),
                content_type="application/json",
            ),
        ),
        source_binding_key="manual_seed_import",
        config_bundle_digest=bundle_digest,
        campaign_bundle_content=bundle_content,
        output_content=output_content,
    )


def _successful_robots_output(*, allowed: bool = True) -> SucceededWorkOutput:
    manual_output = _configured_route_source()
    manual_plan = PipelineTransitionRegistry().plan(manual_output)
    action = manual_plan.website_actions[0]
    lineage_artifact = action.manual_record_lineage_artifact
    assert lineage_artifact is not None
    context = decode_website_route_context(action.context_payload)
    request = json.loads(action.request_payload)
    raw_content = b"User-agent: *\nAllow: /\n" if allowed else b"User-agent: *\nDisallow: /start\n"
    raw_digest = _content_digest(raw_content)
    decision_digest = _content_digest(
        _canonical_json_bytes(
            {
                "allowed": allowed,
                "robotsArtifactDigest": raw_digest,
                "targetUrl": str(context.source_url),
            }
        )
    )
    manifest = HttpAcquisitionManifest(
        requestId=request["requestId"],
        sourceKey=request["sourceKey"],
        sourcePolicyDigest=request["sourcePolicyDigest"],
        requestKind="robots",
        requestedUrl=request["url"],
        finalUrl=request["url"],
        outcome="fetched",
        statusCode=200,
        responseHeaders=(ResponseHeader(name="content-type", value="text/plain"),),
        remoteIpAddress="93.184.216.34",
        encodedSizeBytes=len(raw_content),
        decodedSizeBytes=len(raw_content),
        observedAtUtc=_NOW,
        rawArtifactDigest=raw_digest,
        robotsAllowed=allowed,
        robotsDecisionDigest=decision_digest,
        robotsTargetUrl=str(context.source_url),
    )
    manifest_content = manifest.to_bytes()
    request_content = action.request_payload
    context_content = action.context_payload
    return SucceededWorkOutput(
        source_work_unit_id=UUID("00000000-0000-0000-0000-000000000529"),
        run_id=_RUN_ID,
        stage_run_id=_ACQUISITION_STAGE_ID,
        stage=WorkStage.ACQUISITION,
        capability="http_fetch",
        output_contract="official-http-acquisition@1",
        output_digest=manifest.digest,
        output_artifact=ArtifactIdentity(
            artifact_id=UUID("00000000-0000-0000-0000-000000000530"),
            role="http_acquisition_manifest",
            content_digest=manifest.digest,
            size_bytes=len(manifest_content),
            content_type="application/vnd.collection.official-http-acquisition+json",
        ),
        input_artifacts=(
            ArtifactIdentity(
                artifact_id=UUID("00000000-0000-0000-0000-000000000531"),
                role="http_request",
                content_digest=_content_digest(request_content),
                size_bytes=len(request_content),
                content_type="application/vnd.collection.official-http-request+json",
            ),
            ArtifactIdentity(
                artifact_id=UUID("00000000-0000-0000-0000-000000000532"),
                role="website_route_context",
                content_digest=_content_digest(context_content),
                size_bytes=len(context_content),
                content_type="application/vnd.collection.website-route-context+json",
            ),
            action.manual_record_lineage_artifact,
        ),
        related_output_artifacts=(
            ArtifactIdentity(
                artifact_id=UUID("00000000-0000-0000-0000-000000000533"),
                role="http_raw_response",
                content_digest=raw_digest,
                size_bytes=len(raw_content),
                content_type="text/plain",
            ),
        ),
        source_binding_key="official_website",
        config_bundle_digest=manual_output.config_bundle_digest,
        campaign_bundle_content=manual_output.campaign_bundle_content,
        output_content=manifest_content,
        http_request_content=request_content,
        website_route_context_content=context_content,
        manual_record_lineage_content=action.manual_record_lineage_content,
    )


class FakeResult:
    def __init__(
        self,
        *,
        one: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
        scalar_values: list[object] | None = None,
        rowcount: int = 1,
    ) -> None:
        self._one = one
        self._rows = rows or []
        self._scalar_values = scalar_values or []
        self.rowcount = rowcount

    def mappings(self) -> FakeResult:
        return self

    def scalars(self) -> FakeResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return None if self._one is None else dict(self._one)

    def all(self) -> list[object]:
        return (
            list(self._scalar_values) if self._scalar_values else [dict(row) for row in self._rows]
        )


class FakeConnection:
    def __init__(self) -> None:
        self.row: dict[str, object] | None = None
        self.events: list[dict[str, object]] = []
        self.published: list[object] = []
        self.enqueued: list[object] = []

    def execute(self, statement: Any) -> FakeResult:
        if isinstance(statement, sa.sql.dml.Insert):
            values = _statement_values(statement)
            if statement.table.name == "pipeline_advancements":
                self.row = values
            else:
                self.events.append(values)
            return FakeResult(rowcount=1)
        if isinstance(statement, sa.sql.dml.Update):
            assert self.row is not None
            self.row.update(_statement_values(statement))
            return FakeResult(rowcount=1)
        assert isinstance(statement, sa.sql.selectable.Select)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "FROM runs.stage_runs" in sql:
            stage = _stage_run_filter(statement)
            return FakeResult(
                scalar_values=[
                    _EXTRACTION_STAGE_ID if stage == "extraction" else _ACQUISITION_STAGE_ID
                ]
            )
        if "lease_expires_at_utc <=" in sql:
            return FakeResult(rows=[])
        if "source_work_unit_id =" in sql:
            return FakeResult(one=None if self.row is None else self.row)
        if " LIMIT " in sql and "state =" in sql:
            if self.row is not None and self.row["state"] == "pending":
                return FakeResult(one=self.row)
            return FakeResult(one=None)
        return FakeResult(one=self.row)


class FakeBegin(AbstractContextManager[FakeConnection]):
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self._snapshot: tuple[object, ...] | None = None

    def __enter__(self) -> FakeConnection:
        self._snapshot = (
            None if self._connection.row is None else dict(self._connection.row),
            list(self._connection.events),
            list(self._connection.published),
            list(self._connection.enqueued),
        )
        return self._connection

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is not None and self._snapshot is not None:
            row, events, published, enqueued = self._snapshot
            self._connection.row = row  # type: ignore[assignment]
            self._connection.events = events  # type: ignore[assignment]
            self._connection.published = published  # type: ignore[assignment]
            self._connection.enqueued = enqueued  # type: ignore[assignment]
        return None


class FakeEngine:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def begin(self) -> FakeBegin:
        return FakeBegin(self.connection)


class FakePublisher:
    def publish_in_transaction(self, connection, command):
        assert command.artifact_kind is ArtifactKind.DERIVED_ARTIFACT
        connection.published.append(command)
        return SimpleNamespace(artifact_id=command.artifact_id)


class FakeWorkEngine:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def enqueue_work_in_transaction(self, connection, spec) -> None:
        connection.enqueued.append(spec)
        if self.fail:
            raise WorkEngineConflict(
                code="WORK_OWNER_STATE_INVALID",
                message="The acquisition stage is not open.",
                context={"runId": str(spec.run_id)},
                required_action="Open the acquisition stage before applying route work.",
            )


def _repository(engine, source, work_engine, publisher):
    plan = PipelineTransitionRegistry().plan(source)
    assert plan.disposition in {
        PipelineTransitionDisposition.APPLY,
        PipelineTransitionDisposition.BLOCK,
    }
    assert plan.transition_key in {
        "manual-record-website-routing-v1",
        "official-http-robots-routing-v1",
    }
    appliers = website_route_transition_appliers(
        work_engine,  # type: ignore[arg-type]
        publisher,  # type: ignore[arg-type]
        clock=lambda: _NOW,
    )
    identifiers = iter(UUID(f"00000000-0000-0000-0000-{value:012d}") for value in range(510, 514))
    repository = PostgresPipelineAdvancementRepository(
        engine,  # type: ignore[arg-type]
        source_reader=lambda _connection, work_id: source,
        transition_appliers=appliers,
        clock=lambda: _NOW,
        uuid_factory=lambda: next(identifiers),
        lease_token_factory=lambda: "website-route-lease-token",
    )
    return repository, plan


def _apply(repository, plan, source) -> None:
    registered = repository.register(source, plan, correlation_id="route-test")
    assert registered.state is PipelineAdvancementState.PENDING
    lease = repository.claim(
        ClaimPipelineAdvancement(
            worker_id="pipeline-supervisor-test",
            dagster_execution_id="route-run-test",
            dagster_build_id="route-build-test",
            lease_duration=timedelta(minutes=5),
            correlation_id="route-test",
        )
    )
    assert lease is not None
    repository.apply(
        ApplyPipelineAdvancement(
            advancement_id=lease.advancement_id,
            expected_revision=lease.revision,
            lease_id=lease.lease_id,
            lease_token=lease.lease_token,
            dagster_execution_id=lease.dagster_execution_id,
            transition_plan_digest=lease.transition_plan_digest,
            result_digest=_result_digest(plan),
            correlation_id="route-test",
        )
    )


def test_route_applier_persists_request_and_context_as_leased_http_inputs() -> None:
    engine = FakeEngine()
    source = _configured_route_source()
    plan = PipelineTransitionRegistry().plan(source)
    publisher = FakePublisher()
    work_engine = FakeWorkEngine()
    repository, plan = _repository(engine, source, work_engine, publisher)

    _apply(repository, plan, source)

    row = engine.connection.row
    assert row is not None
    assert row["state"] == "applied"
    assert [event["event_kind"] for event in engine.connection.events] == ["leased", "applied"]
    assert len(engine.connection.published) == 3
    assert len(engine.connection.enqueued) == 1
    spec = engine.connection.enqueued[0]
    assert spec.stage is WorkStage.ACQUISITION
    assert spec.source_key == "official_website"
    assert spec.source_binding_key == "official_website"
    assert spec.expected_output_contract == "official-http-acquisition@1"
    assert tuple(item.role for item in spec.input_artifacts) == (
        "http_request",
        "website_route_context",
        "manual_record_lineage",
    )
    assert tuple(item.artifact_id for item in spec.input_artifacts) == tuple(
        command.artifact_id for command in engine.connection.published
    )
    assert engine.connection.published[0].content == plan.website_actions[0].request_payload
    assert engine.connection.published[1].content == plan.website_actions[0].context_payload
    request = json.loads(engine.connection.published[0].content)
    assert request["requestKind"] == "robots"
    assert request["url"] == "https://hansastudios.de/robots.txt"
    context = json.loads(engine.connection.published[1].content)
    assert context["sourceUrl"] == "https://hansastudios.de/start"
    assert context["requestDigest"] == plan.website_actions[0].input_digest
    assert context["routeKey"] == "manual_website"


def test_robots_allow_applies_page_work_with_exact_robots_artifact_provenance() -> None:
    engine = FakeEngine()
    source = _successful_robots_output()
    plan = PipelineTransitionRegistry().plan(source)
    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert plan.transition_key == "official-http-robots-routing-v1"
    assert plan.website_actions[0].robots_decision_artifact_id == source.output_artifact.artifact_id
    assert (
        plan.website_actions[0].robots_raw_artifact_id
        == source.related_output_artifacts[0].artifact_id
    )
    repository, plan = _repository(
        engine,
        source,
        FakeWorkEngine(),
        FakePublisher(),
    )

    _apply(repository, plan, source)
    replayed = repository.register(source, plan, correlation_id="route-test")

    assert replayed.state is PipelineAdvancementState.APPLIED
    assert engine.connection.row is not None
    assert engine.connection.row["state"] == "applied"
    assert [event["event_kind"] for event in engine.connection.events] == ["leased", "applied"]
    assert len(engine.connection.published) == 2
    assert len(engine.connection.enqueued) == 1
    request = json.loads(engine.connection.published[0].content)
    context = decode_website_route_context(engine.connection.published[1].content)
    assert request["requestKind"] == "page"
    assert request["url"] == "https://hansastudios.de/start"
    assert request["robotsArtifactId"] == str(source.output_artifact.artifact_id)
    assert (
        request["robotsDecisionDigest"] == json.loads(source.output_content)["robotsDecisionDigest"]
    )
    assert str(context.source_url) == request["url"]
    spec = engine.connection.enqueued[0]
    inputs_by_role = {item.role: item for item in spec.input_artifacts}
    assert inputs_by_role["robots_decision"].artifact_id == source.output_artifact.artifact_id
    assert (
        inputs_by_role["robots_raw_response"].artifact_id
        == source.related_output_artifacts[0].artifact_id
    )
    assert inputs_by_role["manual_record_lineage"].artifact_id == next(
        item.artifact_id for item in source.input_artifacts if item.role == "manual_record_lineage"
    )


def test_robots_disallow_persists_blocker_without_creating_page_work() -> None:
    engine = FakeEngine()
    source = _successful_robots_output(allowed=False)
    plan = PipelineTransitionRegistry().plan(source)
    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_WEBSITE_ROBOTS_DISALLOWED"
    repository, plan = _repository(
        engine,
        source,
        FakeWorkEngine(),
        FakePublisher(),
    )

    status = repository.register(source, plan, correlation_id="route-test")

    assert status.state is PipelineAdvancementState.BLOCKED
    assert status.blocker is not None
    assert status.blocker.code == "PIPELINE_WEBSITE_ROBOTS_DISALLOWED"
    assert engine.connection.row is not None
    assert engine.connection.row["state"] == "blocked"
    assert [event["event_kind"] for event in engine.connection.events] == ["registered_block"]
    assert engine.connection.published == []
    assert engine.connection.enqueued == []


def test_route_enqueue_failure_rolls_back_work_artifacts_and_advancement_apply() -> None:
    engine = FakeEngine()
    source = _configured_route_source()
    plan = PipelineTransitionRegistry().plan(source)
    publisher = FakePublisher()
    work_engine = FakeWorkEngine(fail=True)
    repository, plan = _repository(engine, source, work_engine, publisher)
    _apply_registration_and_claim(repository, source, plan)
    lease = _current_lease(engine.connection.row)

    with pytest.raises(PipelineAdvancementConflict) as raised:
        repository.apply(
            ApplyPipelineAdvancement(
                advancement_id=lease.advancement_id,
                expected_revision=lease.revision,
                lease_id=lease.lease_id,
                lease_token=lease.lease_token,
                dagster_execution_id=lease.dagster_execution_id,
                transition_plan_digest=lease.transition_plan_digest,
                result_digest=_result_digest(plan),
                correlation_id="route-test",
            )
        )

    assert raised.value.code == "WORK_OWNER_STATE_INVALID"
    assert engine.connection.row is not None
    assert engine.connection.row["state"] == "leased"
    assert [event["event_kind"] for event in engine.connection.events] == ["leased"]
    assert engine.connection.published == []
    assert engine.connection.enqueued == []


def test_wrong_source_binding_is_blocked_before_any_route_work_is_created() -> None:
    source = _configured_route_source()
    invalid_source = SucceededWorkOutput(
        source_work_unit_id=source.source_work_unit_id,
        run_id=source.run_id,
        stage_run_id=source.stage_run_id,
        stage=source.stage,
        capability=source.capability,
        output_contract=source.output_contract,
        output_digest=source.output_digest,
        output_artifact=source.output_artifact,
        input_artifacts=source.input_artifacts,
        source_binding_key="unconfigured_seed",
        config_bundle_digest=source.config_bundle_digest,
        campaign_bundle_content=source.campaign_bundle_content,
        output_content=source.output_content,
    )

    plan = PipelineTransitionRegistry().plan(invalid_source)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "MANUAL_RECORD_ROUTE_PROVENANCE_MISMATCH"
    assert plan.website_actions == ()


def _stage_run_filter(statement: Any) -> object:
    compiled = statement.compile(dialect=postgresql.dialect())
    return next(
        (value for value in compiled.params.values() if isinstance(value, str)),
        None,
    )


def _successful_page_output() -> SucceededWorkOutput:
    robots_output = _successful_robots_output()
    robots_plan = PipelineTransitionRegistry().plan(robots_output)
    assert robots_plan.disposition is PipelineTransitionDisposition.APPLY
    page_action = robots_plan.website_actions[0]
    lineage_artifact = page_action.manual_record_lineage_artifact
    assert lineage_artifact is not None
    assert page_action.robots_decision_artifact_id is not None
    assert page_action.robots_raw_artifact_id is not None
    request = json.loads(page_action.request_payload)
    context = decode_website_route_context(page_action.context_payload)
    raw_content = b"<html><head><title>Hansa Studios</title></head><body></body></html>"
    raw_digest = _content_digest(raw_content)
    manifest = HttpAcquisitionManifest(
        requestId=request["requestId"],
        sourceKey=request["sourceKey"],
        sourcePolicyDigest=request["sourcePolicyDigest"],
        requestKind="page",
        requestedUrl=request["url"],
        finalUrl=request["url"],
        outcome="fetched",
        statusCode=200,
        responseHeaders=(ResponseHeader(name="content-type", value="text/html; charset=utf-8"),),
        remoteIpAddress="93.184.216.34",
        encodedSizeBytes=len(raw_content),
        decodedSizeBytes=len(raw_content),
        observedAtUtc=_NOW,
        rawArtifactDigest=raw_digest,
        robotsAllowed=True,
        robotsArtifactId=page_action.robots_decision_artifact_id,
        robotsDecisionDigest=request["robotsDecisionDigest"],
        robotsTargetUrl=str(context.source_url),
    )
    manifest_content = manifest.to_bytes()
    request_content = page_action.request_payload
    context_content = page_action.context_payload
    return SucceededWorkOutput(
        source_work_unit_id=UUID("00000000-0000-0000-0000-000000000540"),
        run_id=_RUN_ID,
        stage_run_id=_ACQUISITION_STAGE_ID,
        stage=WorkStage.ACQUISITION,
        capability="http_fetch",
        output_contract="official-http-acquisition@1",
        output_digest=manifest.digest,
        output_artifact=ArtifactIdentity(
            artifact_id=UUID("00000000-0000-0000-0000-000000000543"),
            role="http_acquisition_manifest",
            content_digest=manifest.digest,
            size_bytes=len(manifest_content),
            content_type="application/vnd.collection.official-http-acquisition+json",
        ),
        input_artifacts=(
            ArtifactIdentity(
                artifact_id=UUID("00000000-0000-0000-0000-000000000541"),
                role="http_request",
                content_digest=_content_digest(request_content),
                size_bytes=len(request_content),
                content_type="application/vnd.collection.official-http-request+json",
            ),
            ArtifactIdentity(
                artifact_id=UUID("00000000-0000-0000-0000-000000000542"),
                role="website_route_context",
                content_digest=_content_digest(context_content),
                size_bytes=len(context_content),
                content_type="application/vnd.collection.website-route-context+json",
            ),
            ArtifactIdentity(
                artifact_id=page_action.robots_decision_artifact_id,
                role="robots_decision",
                content_digest=robots_output.output_artifact.content_digest,
                size_bytes=robots_output.output_artifact.size_bytes,
                content_type=robots_output.output_artifact.content_type,
            ),
            ArtifactIdentity(
                artifact_id=page_action.robots_raw_artifact_id,
                role="robots_raw_response",
                content_digest=robots_output.related_output_artifacts[0].content_digest,
                size_bytes=robots_output.related_output_artifacts[0].size_bytes,
                content_type="text/plain",
            ),
            lineage_artifact,
        ),
        related_output_artifacts=(
            ArtifactIdentity(
                artifact_id=UUID("00000000-0000-0000-0000-000000000544"),
                role="http_raw_response",
                content_digest=raw_digest,
                size_bytes=len(raw_content),
                content_type="text/html; charset=utf-8",
            ),
        ),
        source_binding_key="official_website",
        config_bundle_digest=robots_output.config_bundle_digest,
        campaign_bundle_content=robots_output.campaign_bundle_content,
        output_content=manifest_content,
        http_request_content=request_content,
        website_route_context_content=context_content,
        manual_record_lineage_content=robots_output.manual_record_lineage_content,
    )


def _unchanged_page_output() -> SucceededWorkOutput:
    source = _successful_page_output()
    raw_artifact = source.related_output_artifacts[0]
    manifest = HttpAcquisitionManifest.model_validate_json(source.output_content or b"")
    unchanged_manifest = manifest.model_copy(
        update={
            "outcome": "unchanged",
            "status_code": 304,
            "response_headers": (),
            "encoded_size_bytes": 0,
            "decoded_size_bytes": 0,
            "raw_artifact_digest": None,
            "reused_artifact_id": raw_artifact.artifact_id,
            "reused_content_digest": raw_artifact.content_digest,
        }
    )
    manifest_content = unchanged_manifest.to_bytes()
    previous_artifact = replace(raw_artifact, role="previous_raw_artifact")
    return replace(
        source,
        output_digest=unchanged_manifest.digest,
        output_artifact=replace(
            source.output_artifact,
            content_digest=unchanged_manifest.digest,
            size_bytes=len(manifest_content),
        ),
        input_artifacts=(*source.input_artifacts, previous_artifact),
        related_output_artifacts=(),
        output_content=manifest_content,
    )


def _extraction_repository(engine, source, work_engine, publisher):
    from collection_infrastructure.postgres.page_extraction_applier import (
        page_extraction_transition_appliers,
    )

    plan = PipelineTransitionRegistry().plan(source)
    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert plan.transition_key == "official-http-page-extraction-v1"
    assert plan.website_actions == ()
    assert len(plan.extraction_actions) == 1
    appliers = page_extraction_transition_appliers(
        work_engine,  # type: ignore[arg-type]
        publisher,  # type: ignore[arg-type]
        clock=lambda: _NOW,
    )
    identifiers = iter(UUID(f"00000000-0000-0000-0000-{value:012d}") for value in range(550, 554))
    repository = PostgresPipelineAdvancementRepository(
        engine,  # type: ignore[arg-type]
        source_reader=lambda _connection, work_id: source,
        transition_appliers=appliers,
        clock=lambda: _NOW,
        uuid_factory=lambda: next(identifiers),
        lease_token_factory=lambda: "website-route-lease-token",
    )
    return repository, plan


def test_page_result_applies_deterministic_extraction_work() -> None:
    engine = FakeEngine()
    source = _successful_page_output()
    publisher = FakePublisher()
    work_engine = FakeWorkEngine()
    repository, plan = _extraction_repository(engine, source, work_engine, publisher)

    _apply(repository, plan, source)

    action = plan.extraction_actions[0]
    assert engine.connection.row is not None
    assert engine.connection.row["state"] == "applied"
    assert [event["event_kind"] for event in engine.connection.events] == ["leased", "applied"]
    assert len(engine.connection.published) == 1
    command = engine.connection.published[0]
    assert command.content == action.request_payload
    assert command.content_type == "application/vnd.collection.extraction-request+json"
    assert command.source_policy_digest == action.source_policy_digest
    assert len(engine.connection.enqueued) == 1
    spec = engine.connection.enqueued[0]
    assert spec.work_id == action.work_id
    assert spec.stage is WorkStage.EXTRACTION
    assert spec.capability is WorkCapability.EXTRACTION
    assert spec.source_key is None
    assert spec.source_binding_key == "official_website"
    assert spec.expected_output_contract == "extracted-record@1"
    assert spec.input_digest == action.input_digest
    assert spec.stage_run_id == _EXTRACTION_STAGE_ID
    assert tuple(item.role for item in spec.input_artifacts) == (
        "raw_source_document",
        "extraction_request",
        "manual_record_lineage",
    )
    assert spec.input_artifacts[0].artifact_id == action.raw_artifact_id
    assert spec.input_artifacts[0].artifact_id == source.related_output_artifacts[0].artifact_id
    assert spec.input_artifacts[1].artifact_id == command.artifact_id
    assert spec.input_artifacts[2].artifact_id == action.manual_record_lineage_artifact.artifact_id


def test_unchanged_page_applies_extraction_from_exact_previous_raw_artifact() -> None:
    engine = FakeEngine()
    source = _unchanged_page_output()
    previous = next(item for item in source.input_artifacts if item.role == "previous_raw_artifact")
    publisher = FakePublisher()
    repository, plan = _extraction_repository(engine, source, FakeWorkEngine(), publisher)

    _apply(repository, plan, source)

    action = plan.extraction_actions[0]
    assert len(engine.connection.enqueued) == 1
    spec = engine.connection.enqueued[0]
    assert spec.input_artifacts[0].role == "raw_source_document"
    assert spec.input_artifacts[0].artifact_id == previous.artifact_id == action.raw_artifact_id
    assert spec.input_digest == action.input_digest
    assert engine.connection.enqueued[0].work_id == action.work_id
    assert engine.connection.row is not None
    assert engine.connection.row["state"] == "applied"


def test_extraction_enqueue_failure_rolls_back_request_and_advancement_apply() -> None:
    engine = FakeEngine()
    source = _successful_page_output()
    work_engine = FakeWorkEngine(fail=True)
    repository, plan = _extraction_repository(engine, source, work_engine, FakePublisher())
    _apply_registration_and_claim(repository, source, plan)
    lease = _current_lease(engine.connection.row)

    with pytest.raises(PipelineAdvancementConflict) as raised:
        repository.apply(
            ApplyPipelineAdvancement(
                advancement_id=lease.advancement_id,
                expected_revision=lease.revision,
                lease_id=lease.lease_id,
                lease_token=lease.lease_token,
                dagster_execution_id=lease.dagster_execution_id,
                transition_plan_digest=lease.transition_plan_digest,
                result_digest=_result_digest(plan),
                correlation_id="route-test",
            )
        )

    assert raised.value.code == "WORK_OWNER_STATE_INVALID"
    assert engine.connection.row is not None
    assert engine.connection.row["state"] == "leased"
    assert [event["event_kind"] for event in engine.connection.events] == ["leased"]
    assert engine.connection.published == []
    assert engine.connection.enqueued == []


def _apply_registration_and_claim(repository, source, plan) -> None:
    registered = repository.register(source, plan, correlation_id="route-test")
    assert registered.state is PipelineAdvancementState.PENDING
    lease = repository.claim(
        ClaimPipelineAdvancement(
            worker_id="pipeline-supervisor-test",
            dagster_execution_id="route-run-test",
            dagster_build_id="route-build-test",
            lease_duration=timedelta(minutes=5),
            correlation_id="route-test",
        )
    )
    assert lease is not None
    return None


def _current_lease(row: dict[str, object] | None):
    assert row is not None
    return SimpleNamespace(
        advancement_id=row["advancement_id"],
        revision=row["revision"],
        lease_id=row["active_lease_id"],
        lease_token="website-route-lease-token",
        dagster_execution_id=row["dagster_execution_id"],
        transition_plan_digest=row["transition_plan_digest"],
    )


def _result_digest(plan) -> str:
    from collection_application.pipeline_advancement import (
        extraction_work_result_digest,
        website_route_result_digest,
    )

    if plan.transition_key == "official-http-page-extraction-v1":
        return extraction_work_result_digest(plan)
    return website_route_result_digest(plan)


def _statement_values(statement: Any) -> dict[str, object]:
    compiled = statement.compile(dialect=postgresql.dialect())
    columns = {column.name for column in statement.table.columns}
    return {key: value for key, value in compiled.params.items() if key in columns}


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _content_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize(
    ("page_transfer_fault", "expected_code"),
    [
        (None, None),
        ("size", "OBJECT_READ_SIZE_MISMATCH"),
        ("digest", "OBJECT_READ_DIGEST_MISMATCH"),
        ("content_type", "OBJECT_READ_CONTENT_TYPE_MISMATCH"),
    ],
)
def test_manual_seed_robots_to_page_worker_vertical_uses_real_gateway_api(
    page_transfer_fault: str | None,
    expected_code: str | None,
) -> None:
    import httpx
    from fastapi.testclient import TestClient

    from collection_application import (
        ArtifactTransferService,
        LeaseExpirySweepResult,
        PreparedArtifactRead,
        PreparedArtifactUpload,
        VerifiedArtifactUpload,
        WorkCompletionResult,
        WorkCompletionStatus,
        WorkEngineService,
        WorkerRegistrationResult,
        WorkerRegistrationStatus,
    )
    from collection_contracts import ExtractedUrlValue, decode_extracted_record
    from collection_domain import SourcePermit, WorkCapability, WorkInputArtifact, WorkLease
    from http_worker.gateway import SdkHttpWorkerGateway
    from http_worker.worker import HttpWorker
    from official_http.fetch import HttpFetchResult
    from processing_worker import ProcessingWorker, ProcessingWorkerSettings
    from processing_worker.gateway import SdkProcessingGateway
    from source_connector_sdk import SourceWorkerGateway, WorkerGatewayFailure
    from worker_gateway import GatewayDependencies, WorkerAuthenticator, WorkerPrincipal, create_app

    worker_token = "vertical-worker-token-0000000000000001"
    worker_id = "http-worker-vertical-test"
    processing_worker_token = "vertical-processing-token-0000000000000001"
    processing_worker_id = "processing-worker-vertical-test"

    class Fetcher:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def fetch(self, request: Any) -> HttpFetchResult:
            self.requests.append(request)
            body = (
                b"User-agent: *\nAllow: /\n"
                if request.request_kind == "robots"
                else (
                    b'<html><head><script type="application/ld+json">'
                    b'{"@type":"LocalBusiness","name":"Hansa Test Studios",'
                    b'"url":"https://hansastudios.de/contact"}'
                    b"</script></head></html>"
                )
            )
            return HttpFetchResult(
                status_code=200,
                final_url=request.url,
                headers=(
                    ResponseHeader(
                        name="content-type",
                        value="text/plain" if request.request_kind == "robots" else "text/html",
                    ),
                ),
                remote_ip_address="93.184.216.34",
                encoded_size_bytes=len(body),
                decoded_size_bytes=len(body),
                observed_at_utc=_NOW,
                body=body,
            )

    class MemoryObjectStore(httpx.BaseTransport):
        def __init__(self) -> None:
            self.staging: dict[str, bytes] = {}
            self.objects: dict[str, bytes] = {}
            self.content_types: dict[str, str] = {}
            self.served: dict[str, tuple[bytes, str | None]] = {}
            self.reads: list[str] = []

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            identity = request.url.path.rsplit("/", maxsplit=1)[-1]
            if request.method == "PUT" and request.url.path.startswith("/upload/"):
                self.staging[identity] = request.read()
                content_type = request.headers.get("content-type")
                assert content_type is not None
                self.content_types[identity] = content_type
                return httpx.Response(200, request=request)
            if request.method == "GET" and request.url.path.startswith("/read/"):
                self.reads.append(identity)
                if identity in self.served:
                    content, served_content_type = self.served[identity]
                else:
                    content = self.objects.get(identity)
                    served_content_type = self.content_types.get(identity)
                headers = (
                    {"content-type": served_content_type} if served_content_type is not None else {}
                )
                return httpx.Response(
                    200 if content is not None else 404,
                    headers=headers,
                    content=content or b"",
                    request=request,
                )
            return httpx.Response(404, request=request)

    class FastApiTransport(httpx.BaseTransport):
        def __init__(self, client: TestClient) -> None:
            self.client = client

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            response = self.client.request(
                request.method,
                request.url.raw_path.decode("ascii"),
                headers=list(request.headers.multi_items()),
                content=request.read(),
            )
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=response.content,
                request=request,
            )

    object_store = MemoryObjectStore()

    class PostgresWorkEnginePortFake:
        """Database-boundary substitute; the real Worker Gateway API owns the lease protocol."""

        def __init__(self) -> None:
            self.pending: list[Any] = []
            self.active: dict[UUID, tuple[WorkLease, Any]] = {}
            self.completed: dict[UUID, Any] = {}
            self.registrations: list[Any] = []
            self.issued_leases: list[WorkLease] = []
            self.next_lease = 1

        def enqueue(self, spec: Any) -> None:
            self.pending.append(spec)

        def register_worker(self, command: Any) -> WorkerRegistrationResult:
            self.registrations.append(command)
            return WorkerRegistrationResult(
                worker_id=command.worker_id,
                status=WorkerRegistrationStatus.REGISTERED,
            )

        def acquire_lease(self, command: Any) -> WorkLease | None:
            index = next(
                (i for i, spec in enumerate(self.pending) if spec.capability == command.capability),
                None,
            )
            if index is None:
                return None
            spec = self.pending.pop(index)
            request_artifact = next(
                item
                for item in spec.input_artifacts
                if item.role in {"http_request", "extraction_request"}
            )
            request_content = object_store.objects[str(request_artifact.artifact_id)]
            request_payload = json.loads(request_content)
            issued = _NOW
            lease = WorkLease(
                lease_id=UUID(int=3_000 + self.next_lease),
                work_id=spec.work_id,
                lease_token=UUID(int=4_000 + self.next_lease),
                worker_id=command.worker_id,
                stage=spec.stage,
                capability=spec.capability,
                input_digest=spec.input_digest,
                expected_output_contract=spec.expected_output_contract,
                issued_at_utc=issued,
                expires_at_utc=issued + timedelta(minutes=5),
                heartbeat_deadline_utc=issued + timedelta(minutes=1),
                source_permit=(
                    SourcePermit(
                        source_key=spec.source_key,
                        policy_digest=request_payload["sourcePolicyDigest"],
                        permit_not_before_utc=issued,
                    )
                    if spec.source_key is not None
                    else None
                ),
                correlation_id=command.correlation_id,
                input_artifacts=tuple(
                    WorkInputArtifact(
                        artifact_id=item.artifact_id,
                        role=item.role,
                        content_digest=_content_digest(object_store.objects[str(item.artifact_id)]),
                        size_bytes=len(object_store.objects[str(item.artifact_id)]),
                        content_type=object_store.content_types[str(item.artifact_id)],
                    )
                    for item in spec.input_artifacts
                ),
            )
            self.next_lease += 1
            self.active[spec.work_id] = (lease, spec)
            self.issued_leases.append(lease)
            return lease

        def require_active(self, command: Any) -> tuple[WorkLease, Any]:
            active = self.active.get(command.work_id)
            assert active is not None
            lease, _spec = active
            lease.require_active(
                lease_id=command.lease_id,
                lease_token=command.lease_token,
                worker_id=command.worker_id,
                input_digest=command.input_digest,
                now_utc=_NOW,
            )
            return active

        def heartbeat(self, command: Any) -> WorkLease:
            lease, spec = self.require_active(command)
            renewed = lease.renew(
                now_utc=_NOW,
                lease_duration=timedelta(seconds=command.lease_duration_seconds),
                heartbeat_interval=timedelta(seconds=command.heartbeat_interval_seconds),
            )
            self.active[command.work_id] = (renewed, spec)
            return renewed

        def complete(self, command: Any) -> WorkCompletionResult:
            self.require_active(command)
            self.completed[command.work_id] = command
            self.active.pop(command.work_id)
            return WorkCompletionResult(
                work_id=command.work_id,
                status=WorkCompletionStatus.APPLIED,
                output_digest=command.output_digest,
                revision=1,
            )

        def expire_leases(self, command: Any) -> LeaseExpirySweepResult:
            del command
            return LeaseExpirySweepResult(expired_count=0, retry_wait_count=0, dead_letter_count=0)

    work_engine = PostgresWorkEnginePortFake()

    class PostgresArtifactTransferPortFake:
        """Database-boundary substitute with object bytes served by MemoryObjectStore."""

        def __init__(self) -> None:
            self.prepared_uploads: dict[UUID, Any] = {}
            self.verified: dict[UUID, VerifiedArtifactUpload] = {}
            self.read_commands: list[Any] = []

        def prepare_upload(self, command: Any) -> PreparedArtifactUpload:
            work_engine.require_active(command)
            self.prepared_uploads[command.upload_id] = command
            return PreparedArtifactUpload(
                upload_id=command.upload_id,
                method="PUT",
                url=f"https://object-store.local/upload/{command.upload_id}",
                required_headers={"content-type": command.content_type},
                expires_at_utc=_NOW + timedelta(minutes=5),
            )

        def verify_upload(self, command: Any) -> VerifiedArtifactUpload:
            work_engine.require_active(command)
            prepared = self.prepared_uploads[command.upload_id]
            content = object_store.staging[str(command.upload_id)]
            digest = _content_digest(content)
            assert digest == prepared.expected_digest
            assert len(content) == prepared.expected_size_bytes
            object_store.objects[str(command.upload_id)] = content
            result = VerifiedArtifactUpload(
                upload_id=command.upload_id,
                work_id=command.work_id,
                artifact_kind=prepared.artifact_kind,
                content_digest=digest,
                size_bytes=len(content),
                content_type=prepared.content_type,
                storage_reference=str(command.upload_id),
                verified_at_utc=_NOW,
            )
            self.verified[command.upload_id] = result
            return result

        def prepare_read(self, command: Any) -> PreparedArtifactRead:
            lease, spec = work_engine.require_active(command)
            assert command.artifact_id in {item.artifact_id for item in spec.input_artifacts}
            assert str(command.artifact_id) in object_store.objects
            self.read_commands.append(command)
            return PreparedArtifactRead(
                artifact_id=command.artifact_id,
                method="GET",
                url=f"https://object-store.local/read/{command.artifact_id}",
                expires_at_utc=lease.expires_at_utc,
            )

    artifact_transfer = PostgresArtifactTransferPortFake()
    authenticator = WorkerAuthenticator.from_plaintext_credentials(
        {
            worker_token: WorkerPrincipal(
                worker_id=worker_id,
                capabilities=frozenset({WorkCapability.HTTP_FETCH}),
            ),
            processing_worker_token: WorkerPrincipal(
                worker_id=processing_worker_id,
                capabilities=frozenset({WorkCapability.EXTRACTION}),
            ),
        }
    )
    app = create_app(
        GatewayDependencies(
            work_engine=WorkEngineService(work_engine),
            artifact_transfer=ArtifactTransferService(artifact_transfer),
            authenticator=authenticator,
            readiness_probe=lambda: None,
            expiry_interval_seconds=0,
        )
    )

    def identity(
        artifact_id: UUID, role: str, content: bytes, content_type: str
    ) -> ArtifactIdentity:
        return ArtifactIdentity(
            artifact_id=artifact_id,
            role=role,
            content_digest=_content_digest(content),
            size_bytes=len(content),
            content_type=content_type,
        )

    def run_worker(spec: Any, fetcher: Fetcher, gateway: SdkHttpWorkerGateway) -> None:
        work_engine.enqueue(spec)
        assert HttpWorker(gateway, fetcher=fetcher).run_once()
        assert spec.work_id in work_engine.completed

    manual_source = _configured_route_source()
    manual_engine = FakeEngine()
    _apply(
        *_repository(manual_engine, manual_source, FakeWorkEngine(), FakePublisher()),
        manual_source,
    )
    robots_spec = manual_engine.connection.enqueued[0]
    request_command, context_command, lineage_command = manual_engine.connection.published
    for command in (request_command, context_command, lineage_command):
        object_store.objects[str(command.artifact_id)] = command.content
        object_store.content_types[str(command.artifact_id)] = command.content_type

    with TestClient(app, raise_server_exceptions=False) as api_client:
        gateway_client = SourceWorkerGateway(
            base_url="http://worker-gateway.test",
            token=worker_token,
            gateway_transport=FastApiTransport(api_client),
            object_transport=object_store,
        )
        processing_gateway_client = SourceWorkerGateway(
            base_url="http://worker-gateway.test",
            token=processing_worker_token,
            gateway_transport=FastApiTransport(api_client),
            object_transport=object_store,
        )
        try:
            gateway = SdkHttpWorkerGateway(gateway_client)
            gateway.register(build_identity=worker_id)
            robots_fetcher = Fetcher()
            run_worker(robots_spec, robots_fetcher, gateway)
            assert len(robots_fetcher.requests) == 1
            assert robots_fetcher.requests[0].request_kind == "robots"

            robots_completion = work_engine.completed[robots_spec.work_id]
            manifest_id = next(
                item.upload_id
                for item in robots_completion.output_artifacts
                if item.role == "http_acquisition_manifest"
            )
            raw_id = next(
                item.upload_id
                for item in robots_completion.output_artifacts
                if item.role == "http_raw_response"
            )
            manifest_upload = artifact_transfer.verified[manifest_id]
            raw_upload = artifact_transfer.verified[raw_id]
            manifest_bytes = object_store.objects[str(manifest_id)]
            raw_bytes = object_store.objects[str(raw_id)]
            robots_manifest = HttpAcquisitionManifest.model_validate_json(
                manifest_bytes, strict=True
            )
            robots_source = SucceededWorkOutput(
                source_work_unit_id=robots_spec.work_id,
                run_id=manual_source.run_id,
                stage_run_id=_ACQUISITION_STAGE_ID,
                stage=WorkStage.ACQUISITION,
                capability="http_fetch",
                output_contract=robots_spec.expected_output_contract,
                output_digest=robots_completion.output_digest,
                output_artifact=identity(
                    manifest_id,
                    "http_acquisition_manifest",
                    manifest_bytes,
                    manifest_upload.content_type,
                ),
                input_artifacts=(
                    identity(
                        request_command.artifact_id,
                        "http_request",
                        request_command.content,
                        request_command.content_type,
                    ),
                    identity(
                        context_command.artifact_id,
                        "website_route_context",
                        context_command.content,
                        context_command.content_type,
                    ),
                    identity(
                        lineage_command.artifact_id,
                        "manual_record_lineage",
                        lineage_command.content,
                        lineage_command.content_type,
                    ),
                ),
                related_output_artifacts=(
                    identity(raw_id, "http_raw_response", raw_bytes, raw_upload.content_type),
                ),
                source_binding_key=robots_spec.source_binding_key,
                config_bundle_digest=manual_source.config_bundle_digest,
                campaign_bundle_content=manual_source.campaign_bundle_content,
                output_content=manifest_bytes,
                http_request_content=request_command.content,
                website_route_context_content=context_command.content,
                manual_record_lineage_content=lineage_command.content,
            )
            assert robots_manifest.robots_allowed is True
            assert robots_manifest.robots_target_url == "https://hansastudios.de/start"

            page_engine = FakeEngine()
            page_publisher = FakePublisher()
            page_plan = PipelineTransitionRegistry().plan(robots_source)
            page_repository, page_plan = _repository(
                page_engine, robots_source, FakeWorkEngine(), page_publisher
            )
            _apply(page_repository, page_plan, robots_source)
            page_spec = page_engine.connection.enqueued[0]
            for command in page_engine.connection.published:
                object_store.objects[str(command.artifact_id)] = command.content
                object_store.content_types[str(command.artifact_id)] = command.content_type
            page_fetcher = Fetcher()
            if page_transfer_fault is None:
                run_worker(page_spec, page_fetcher, gateway)
            else:
                page_request_artifact = page_engine.connection.published[0]
                page_request_artifact_id = str(page_request_artifact.artifact_id)
                canonical_request_bytes = object_store.objects[page_request_artifact_id]
                if page_transfer_fault == "size":
                    object_store.served[page_request_artifact_id] = (
                        canonical_request_bytes[:-1],
                        page_request_artifact.content_type,
                    )
                elif page_transfer_fault == "digest":
                    tampered_request_bytes = (
                        bytes([canonical_request_bytes[0] ^ 0x01]) + canonical_request_bytes[1:]
                    )
                    object_store.served[page_request_artifact_id] = (
                        tampered_request_bytes,
                        page_request_artifact.content_type,
                    )
                elif page_transfer_fault == "content_type":
                    object_store.served[page_request_artifact_id] = (
                        canonical_request_bytes,
                        "application/json",
                    )
                else:
                    raise AssertionError(f"unsupported transfer fault {page_transfer_fault}")
                work_engine.enqueue(page_spec)
                with pytest.raises(WorkerGatewayFailure) as rejected:
                    HttpWorker(gateway, fetcher=page_fetcher).run_once()
                assert rejected.value.envelope.owner == "SourceConnectorSdk.ObjectTransfer"
                assert rejected.value.envelope.code == expected_code
                assert page_request_artifact_id in object_store.reads
                assert page_fetcher.requests == []
                assert page_spec.work_id not in work_engine.completed
                return

            page_request = json.loads(page_engine.connection.published[0].content)
            assert page_request["url"] == "https://hansastudios.de/start"
            assert page_request["robotsArtifactId"] == str(manifest_id)
            assert page_request["robotsManifestDigest"] == _content_digest(manifest_bytes)
            assert page_request["robotsDecisionDigest"] == robots_manifest.robots_decision_digest
            page_lease = next(
                lease for lease in work_engine.issued_leases if lease.work_id == page_spec.work_id
            )
            page_reads = [
                command
                for command in artifact_transfer.read_commands
                if command.work_id == page_spec.work_id
            ]
            assert any(command.artifact_id == manifest_id for command in page_reads)
            assert all(command.lease_id == page_lease.lease_id for command in page_reads)
            assert manifest_id in {item.artifact_id for item in page_lease.input_artifacts}
            assert manifest_id in {UUID(value) for value in object_store.reads}
            assert page_fetcher.requests[0].url == "https://hansastudios.de/start"
            assert page_fetcher.requests[0].request_kind == "page"

            page_completion = work_engine.completed[page_spec.work_id]
            page_manifest_id = next(
                item.upload_id
                for item in page_completion.output_artifacts
                if item.role == "http_acquisition_manifest"
            )
            page_manifest_bytes = object_store.objects[str(page_manifest_id)]
            page_manifest = HttpAcquisitionManifest.model_validate_json(
                page_manifest_bytes, strict=True
            )
            assert page_manifest.robots_artifact_id == manifest_id
            assert page_manifest.robots_decision_digest == robots_manifest.robots_decision_digest
            assert page_manifest.robots_target_url == "https://hansastudios.de/start"

            page_raw_id = next(
                item.upload_id
                for item in page_completion.output_artifacts
                if item.role == "http_raw_response"
            )
            page_raw_upload = artifact_transfer.verified[page_raw_id]
            page_raw_bytes = object_store.objects[str(page_raw_id)]
            page_request_command, page_context_command = page_engine.connection.published
            page_input_content_types = {
                page_request_command.artifact_id: page_request_command.content_type,
                page_context_command.artifact_id: page_context_command.content_type,
                manifest_id: manifest_upload.content_type,
                raw_id: raw_upload.content_type,
                lineage_command.artifact_id: lineage_command.content_type,
            }
            page_source = SucceededWorkOutput(
                source_work_unit_id=page_spec.work_id,
                run_id=manual_source.run_id,
                stage_run_id=_ACQUISITION_STAGE_ID,
                stage=WorkStage.ACQUISITION,
                capability="http_fetch",
                output_contract=page_spec.expected_output_contract,
                output_digest=page_completion.output_digest,
                output_artifact=identity(
                    page_manifest_id,
                    "http_acquisition_manifest",
                    page_manifest_bytes,
                    artifact_transfer.verified[page_manifest_id].content_type,
                ),
                input_artifacts=tuple(
                    identity(
                        item.artifact_id,
                        item.role,
                        object_store.objects[str(item.artifact_id)],
                        page_input_content_types[item.artifact_id],
                    )
                    for item in page_spec.input_artifacts
                ),
                related_output_artifacts=(
                    identity(
                        page_raw_id,
                        "http_raw_response",
                        page_raw_bytes,
                        page_raw_upload.content_type,
                    ),
                ),
                source_binding_key=page_spec.source_binding_key,
                config_bundle_digest=manual_source.config_bundle_digest,
                campaign_bundle_content=manual_source.campaign_bundle_content,
                output_content=page_manifest_bytes,
                http_request_content=page_engine.connection.published[0].content,
                website_route_context_content=page_engine.connection.published[1].content,
                manual_record_lineage_content=lineage_command.content,
            )
            extraction_engine = FakeEngine()
            extraction_work_engine = FakeWorkEngine()
            extraction_repository, extraction_plan = _extraction_repository(
                extraction_engine,
                page_source,
                extraction_work_engine,
                FakePublisher(),
            )
            _apply(extraction_repository, extraction_plan, page_source)
            extraction_spec = extraction_engine.connection.enqueued[0]
            work_engine.enqueue(extraction_spec)
            for command in extraction_engine.connection.published:
                object_store.objects[str(command.artifact_id)] = command.content
                object_store.content_types[str(command.artifact_id)] = command.content_type

            processing_settings = ProcessingWorkerSettings(
                gateway_url="http://worker-gateway.test",
                gateway_token=processing_worker_token,
                build_identity=processing_worker_id,
                capability="extraction",
            )
            processing_gateway = SdkProcessingGateway(processing_gateway_client)
            processing_worker = ProcessingWorker(processing_gateway, processing_settings)
            processing_worker.register()
            extraction_result = processing_worker.run_once()

            assert extraction_result.acquired
            assert extraction_spec.work_id in work_engine.completed
            extraction_completion = work_engine.completed[extraction_spec.work_id]
            assert extraction_completion.output_contract == "extracted-record@1"
            extracted_id = next(
                item.upload_id
                for item in extraction_completion.output_artifacts
                if item.role == "extracted_record"
            )
            extracted_record = decode_extracted_record(object_store.objects[str(extracted_id)])
            assert extracted_record.source_url == "https://hansastudios.de/start"
            assert extracted_record.raw_artifact_digest == _content_digest(page_raw_bytes)
            assert any(
                field.field_key == "website"
                and isinstance(field.value, ExtractedUrlValue)
                and field.value.value == "https://hansastudios.de/contact"
                for field in extracted_record.fields
            )
            extraction_lease = next(
                lease
                for lease in work_engine.issued_leases
                if lease.work_id == extraction_spec.work_id
            )
            extraction_reads = [
                command
                for command in artifact_transfer.read_commands
                if command.work_id == extraction_spec.work_id
            ]
            assert {command.artifact_id for command in extraction_reads} == {
                item.artifact_id for item in extraction_spec.input_artifacts
            }
            assert all(
                command.lease_id == extraction_lease.lease_id for command in extraction_reads
            )
            assert len(work_engine.issued_leases) == 3
        finally:
            processing_gateway_client.close()
            gateway_client.close()
