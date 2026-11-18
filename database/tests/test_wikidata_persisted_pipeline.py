"""The Wikidata acquisition as the product actually persists it.

Nothing here is pooled from another test: a campaign snapshot is published, a run is
bootstrapped with its DISCOVERY unit, the real Wikidata worker leases that unit through the
real Worker Gateway, uploads the response and its envelope, the catalogue reads them back, the
planner fans out one mapping unit per accepted item, the real processing worker maps each one,
and candidate intake writes the candidate, its revision evidence and its review case.

The campaign under test is a test-only copy: its Wikidata binding is enabled while its policy
bytes stay the shipped ones, so `legal_status` remains `research_only` and the persisted
candidate must therefore be refused by the sealed-export reader.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from shutil import copytree
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from db_support import disposable_database
from fastapi.testclient import TestClient
from s3_support import FakeS3Client, S3Transport, object_store
from sqlalchemy.engine import Engine

from collection_application import (
    ArtifactTransferService,
    CampaignRunService,
    CampaignSnapshotPublicationService,
    CampaignSnapshotService,
    CreateCampaignRun,
    OwnedArtifactPublisherService,
    WorkEngineService,
)
from collection_application.pipeline_advancement import (
    ApplyPipelineAdvancement,
    ClaimPipelineAdvancement,
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    PipelineTransitionRegistry,
    candidate_intake_result_digest,
    resolution_snapshot_result_digest,
    source_record_mapping_result_digest,
)
from collection_contracts import ObservationState, decode_source_record_observation_batch
from collection_infrastructure import (
    PostgresCampaignRunStore,
    PostgresCampaignSnapshotStore,
    PostgresOwnedArtifactPublisher,
    PostgresWorkEngine,
)
from collection_infrastructure.export.service import ExportServiceError
from collection_infrastructure.filesystem_campaign_source import FilesystemCampaignBundleSource
from collection_infrastructure.postgres.artifact_transfer import PostgresArtifactTransfer
from collection_infrastructure.postgres.candidate_intake_applier import (
    candidate_intake_transition_appliers,
)
from collection_infrastructure.postgres.export_readers import (
    PostgresExportCandidatePort,
    PostgresExportCandidateReader,
)
from collection_infrastructure.postgres.pipeline_advancement import (
    PostgresPipelineAdvancementRepository,
)
from collection_infrastructure.postgres.resolution_scheduling import (
    PostgresResolutionBatchScheduler,
)
from collection_infrastructure.postgres.resolution_snapshot_applier import (
    resolution_snapshot_transition_appliers,
)
from collection_infrastructure.postgres.source_record_mapping_applier import (
    source_record_mapping_transition_appliers,
)
from collection_infrastructure.postgres.succeeded_work_catalog import (
    PostgresSucceededWorkCatalog,
)
from processing_worker.contracts import ProcessingWorkerSettings
from processing_worker.gateway import SdkProcessingGateway
from processing_worker.worker import ProcessingWorker
from resolution_contracts import decode_resolution_batch
from resolution_worker.contracts import ResolutionWorkerSettings
from resolution_worker.gateway import SdkResolutionGateway
from resolution_worker.worker import ResolutionWorker
from source_connector_sdk import SourceWorkerGateway
from wikidata_studios import WikidataFetchResult
from wikidata_worker.gateway import SdkWikidataWorkerGateway
from wikidata_worker.worker import WikidataWorker, WikidataWorkerPolicy
from worker_gateway import GatewayDependencies, WorkerAuthenticator, WorkerPrincipal, create_app

pytestmark = pytest.mark.integration

# Fixture clocks, not acquisition evidence: the real worker stamps the instant it read the
# source with `datetime.now(UTC)`. The fixture pins a past instant so a persisted record can
# never look future-dated, and the pipeline clock runs two seconds later so the ordering
# "acquire, then publish" stays truthful.
_ACQUIRED_AT = datetime(2026, 11, 1, 9, 29, 58, tzinfo=UTC)
_NOW = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
_CAMPAIGN_KEY = "wikidata_pipeline_campaign"
_CAMPAIGN_SOURCE = Path(__file__).resolve().parents[2] / "campaigns" / "berlin_recording_services"
_RESPONSE_PATH = (
    Path(__file__).resolve().parents[2]
    / "connectors"
    / "wikidata_studios"
    / "tests"
    / "fixtures"
    / "wbgetentities.json"
)
_BINDING_KEY = "wikidata_studios_berlin"
_ACCEPTED_SOURCE_RECORDS = {
    "wikidata:Q105762301",
    "wikidata:Q56298345",
    "wikidata:Q667023",
    "wikidata:Q7695694",
}
_AMBIGUOUS_SOURCE_RECORD = "wikidata:Q1372342"
# Q667023 (Hansa) and Q56298345 (Studio 1058) carry coordinates in the captured response;
# the other two accepted entities have no point at all, so intake records NOT_OBSERVED.
_LOCATED_SOURCE_RECORDS = {"wikidata:Q56298345", "wikidata:Q667023"}
_WIKIDATA_MAPPING_TRANSITION = "wikidata-entity-result-source-record-mapping-v1"
_WIKIDATA_TOKEN = "wikidata-worker-token-" + secrets.token_hex(8)
_PROCESSING_TOKEN = "processing-worker-token-" + secrets.token_hex(8)
_RESOLUTION_TOKEN = "resolution-worker-token-" + secrets.token_hex(8)
_WIKIDATA_WORKER = "wikidata-worker@integration"
_PROCESSING_WORKER = "processing-worker@integration"
_RESOLUTION_WORKER = "resolution-worker@integration"


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _copy_campaign(tmp_path: Path) -> Path:
    """Copy the shipped campaign and enable the Wikidata binding on the copy only.

    The shipped campaign owns the approved Wikidata policy but keeps the binding disabled:
    turning the source on belongs to the environment that deliberately activates it.
    """

    shipped = (_CAMPAIGN_SOURCE / "campaign.yaml").read_text(encoding="utf-8")
    assert f"- {_BINDING_KEY}\n" not in shipped
    assert "legal_status: approved" in (
        _CAMPAIGN_SOURCE / "source_policies/wikidata_berlin_studios_policy.yaml"
    ).read_text(encoding="utf-8")

    campaign = tmp_path / _CAMPAIGN_KEY
    copytree(_CAMPAIGN_SOURCE, campaign)
    campaign_yaml = (campaign / "campaign.yaml").read_text(encoding="utf-8")
    campaign_yaml = campaign_yaml.replace("berlin_recording_services", _CAMPAIGN_KEY)
    campaign_yaml = campaign_yaml.replace(
        "- osm_overpass_berlin\n",
        "- osm_overpass_berlin\n- " + _BINDING_KEY + "\n",
        1,
    )
    assert f"- {_BINDING_KEY}\n" in campaign_yaml
    (campaign / "campaign.yaml").write_text(campaign_yaml, encoding="utf-8")
    return tmp_path


class _Fetcher:
    """The captured response, served without touching the network."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.requests: list[object] = []

    def fetch(self, request: object) -> WikidataFetchResult:
        self.requests.append(request)
        return WikidataFetchResult(
            body=self._body,
            content_digest=_digest(self._body),
            content_type="application/json",
        )


class _FastApiTransport(httpx.BaseTransport):
    def __init__(self, client: TestClient) -> None:
        self._client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._client.request(
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


@dataclass
class _Pipeline:
    engine: Engine
    run_id: UUID
    discovery_work_id: UUID
    mapping_work_ids: tuple[UUID, ...]
    processing: ProcessingWorker
    catalog: PostgresSucceededWorkCatalog
    advancement: PostgresPipelineAdvancementRepository
    resolution_work_id: UUID | None = None


def _rows(engine: Engine, sql: str, **params: object) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.text(sql), params).mappings()]


def _row(engine: Engine, sql: str, **params: object) -> dict[str, Any]:
    rows = _rows(engine, sql, **params)
    assert len(rows) == 1, f"expected one row, got {len(rows)}"
    return rows[0]


def _work_rows(engine: Engine, run_id: UUID) -> list[dict[str, Any]]:
    return _rows(
        engine,
        "SELECT work_id, capability, stage, state, semantic_key, source_binding_key, "
        "expected_output_contract FROM work.work_units WHERE run_id = :run_id",
        run_id=run_id,
    )


def _advancement(engine: Engine, store: object) -> PostgresPipelineAdvancementRepository:
    catalog = PostgresSucceededWorkCatalog(engine, object_store=store)  # type: ignore[arg-type]
    work_engine = PostgresWorkEngine(engine, clock=lambda: _NOW)
    publisher = PostgresOwnedArtifactPublisher(engine, store, clock=lambda: _NOW)  # type: ignore[arg-type]
    appliers = {
        **source_record_mapping_transition_appliers(
            work_engine,
            publisher,
            clock=lambda: _NOW,
        ),
        **candidate_intake_transition_appliers(
            clock=lambda: _NOW,
            resolution_scheduler=PostgresResolutionBatchScheduler(
                work_engine,
                publisher,
                catalog,
                clock=lambda: _NOW,
            ),
        ),
        **resolution_snapshot_transition_appliers(catalog, clock=lambda: _NOW),
    }
    return PostgresPipelineAdvancementRepository(
        engine,
        source_reader=catalog,
        transition_appliers=appliers,
        clock=lambda: _NOW,
    )


def _read_source(pipeline: _Pipeline, work_id: UUID):
    with pipeline.engine.connect() as connection:
        return pipeline.catalog.read(connection, work_id)


def _apply_advancement(pipeline: _Pipeline, source_work_unit_id: UUID) -> str:
    source = _read_source(pipeline, source_work_unit_id)
    plan: PipelineTransitionPlan = PipelineTransitionRegistry().plan(source)
    assert plan.disposition is PipelineTransitionDisposition.APPLY, plan.blocker
    pipeline.advancement.register(source, plan, correlation_id="wikidata-pipeline")
    lease = pipeline.advancement.claim(
        ClaimPipelineAdvancement(
            worker_id="pipeline-supervisor",
            dagster_execution_id="dagster-run",
            dagster_build_id="dagster-build",
            lease_duration=timedelta(minutes=5),
            correlation_id="wikidata-pipeline",
        )
    )
    assert lease is not None, "the advancement was registered as blocked"
    if plan.resolution_snapshot_actions:
        result_digest = resolution_snapshot_result_digest(plan)
    elif plan.source_record_mapping_actions:
        result_digest = source_record_mapping_result_digest(plan)
    else:
        result_digest = candidate_intake_result_digest(plan)
    status = pipeline.advancement.apply(
        ApplyPipelineAdvancement(
            advancement_id=lease.advancement_id,
            expected_revision=lease.revision,
            lease_id=lease.lease_id,
            lease_token=lease.lease_token,
            dagster_execution_id=lease.dagster_execution_id,
            transition_plan_digest=lease.transition_plan_digest,
            result_digest=result_digest,
            correlation_id="wikidata-pipeline",
        )
    )
    return status.state.value


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Pipeline]:
    if not os.environ.get("COLLECTOR_DATABASE_URL", "").strip():
        pytest.fail("COLLECTOR_DATABASE_URL is required for the persisted Wikidata pipeline.")

    tmp_path = _copy_campaign(tmp_path_factory.mktemp("wikidata-pipeline"))
    for url in disposable_database("persisted wikidata pipeline"):
        engine = sa.create_engine(url, poolclass=sa.NullPool)
        try:
            yield _drive(engine, tmp_path)
        finally:
            engine.dispose()


def _drive(engine: Engine, tmp_path: Path) -> _Pipeline:
    client = FakeS3Client()
    store = object_store(client)
    artifacts = PostgresOwnedArtifactPublisher(engine, store, clock=lambda: _NOW)  # type: ignore[arg-type]
    publication = CampaignSnapshotPublicationService(
        CampaignSnapshotService(FilesystemCampaignBundleSource(tmp_path)),
        OwnedArtifactPublisherService(artifacts),
        PostgresCampaignSnapshotStore(engine),
    )
    run_id = uuid4()
    CampaignRunService(
        publication,
        OwnedArtifactPublisherService(artifacts),
        PostgresCampaignRunStore(engine, clock=lambda: _NOW),
        clock=lambda: _NOW,
    ).create(
        CreateCampaignRun(
            run_id=run_id,
            campaign_key=_CAMPAIGN_KEY,
            correlation_id="wikidata-pipeline",
        )
    )

    discovery_work_id = next(
        UUID(str(row["work_id"]))
        for row in _work_rows(engine, run_id)
        if row["capability"] == "wikidata_entity_query"
    )
    catalog = PostgresSucceededWorkCatalog(engine, object_store=store)  # type: ignore[arg-type]
    advancement = _advancement(engine, store)
    state = _Pipeline(
        engine=engine,
        run_id=run_id,
        discovery_work_id=discovery_work_id,
        mapping_work_ids=(),
        processing=None,  # type: ignore[arg-type]
        catalog=catalog,
        advancement=advancement,
    )

    def readiness_probe() -> None:
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT 1")).scalar_one() == 1

    app = create_app(
        GatewayDependencies(
            work_engine=WorkEngineService(PostgresWorkEngine(engine, clock=lambda: _NOW)),
            artifact_transfer=ArtifactTransferService(
                PostgresArtifactTransfer(engine, store, clock=lambda: _NOW)  # type: ignore[arg-type]
            ),
            authenticator=WorkerAuthenticator.from_plaintext_credentials(
                {
                    _WIKIDATA_TOKEN: WorkerPrincipal(
                        worker_id=_WIKIDATA_WORKER,
                        capabilities=frozenset({"wikidata_entity_query"}),
                    ),
                    _PROCESSING_TOKEN: WorkerPrincipal(
                        worker_id=_PROCESSING_WORKER,
                        capabilities=frozenset({"source_record_mapping"}),
                    ),
                    _RESOLUTION_TOKEN: WorkerPrincipal(
                        worker_id=_RESOLUTION_WORKER,
                        capabilities=frozenset({"entity_resolution"}),
                    ),
                }
            ),
            readiness_probe=readiness_probe,
            expiry_interval_seconds=0,
        )
    )
    api = TestClient(app, raise_server_exceptions=False)
    object_transport = S3Transport(client)

    def gateway(token: str) -> SourceWorkerGateway:
        return SourceWorkerGateway(
            base_url="http://worker-gateway.test",
            token=token,
            gateway_transport=_FastApiTransport(api),
            object_transport=object_transport,
        )

    with api:
        wikidata_gateway = SdkWikidataWorkerGateway(gateway(_WIKIDATA_TOKEN))
        wikidata_gateway.register(build_identity=_WIKIDATA_WORKER)
        assert (
            WikidataWorker(
                wikidata_gateway,
                _Fetcher(_RESPONSE_PATH.read_bytes()),
                policy=WikidataWorkerPolicy(
                    lease_duration_seconds=120, heartbeat_interval_seconds=30
                ),
                clock=lambda: _ACQUIRED_AT,
            ).run_once()
            is True
        )

        # The acquisition is persisted, so the catalogue - not the worker - decides what it
        # means.
        assert _apply_advancement(state, discovery_work_id) == "applied"

        mapping_work_ids = tuple(
            UUID(str(row["work_id"]))
            for row in _work_rows(engine, run_id)
            if row["capability"] == "source_record_mapping"
        )
        state.mapping_work_ids = mapping_work_ids

        settings = ProcessingWorkerSettings(
            gateway_url="http://worker-gateway.test",
            gateway_token=_PROCESSING_TOKEN,
            build_identity=_PROCESSING_WORKER,
            capability="source_record_mapping",
            lease_duration_seconds=120,
            heartbeat_interval_seconds=30,
            maximum_input_bytes=64 * 1024 * 1024,
            maximum_mapping_records=1,
        )
        processing_gateway = SdkProcessingGateway(gateway(_PROCESSING_TOKEN))
        processing_gateway.register(settings)
        processing = ProcessingWorker(processing_gateway, settings)
        state.processing = processing
        for _ in range(len(mapping_work_ids)):
            outcome = processing.run_once()
            assert outcome.acquired is True, outcome

        for work_id in mapping_work_ids:
            assert _apply_advancement(state, work_id) == "applied"

        # Candidate intake is done, so the run's single resolution batch exists and its unit
        # is leasable: scheduling is a consequence of the drained run, not an operator step.
        resolution_rows = tuple(
            row for row in _work_rows(engine, run_id) if row["capability"] == "entity_resolution"
        )
        assert len(resolution_rows) == 1, resolution_rows
        assert resolution_rows[0]["stage"] == "entity_resolution"
        assert resolution_rows[0]["state"] == "pending"
        resolution_work_id = UUID(str(resolution_rows[0]["work_id"]))
        state.resolution_work_id = resolution_work_id

        resolution_settings = ResolutionWorkerSettings(
            gateway_url="http://worker-gateway.test",
            gateway_token=_RESOLUTION_TOKEN,
            build_identity=_RESOLUTION_WORKER,
            lease_duration_seconds=120,
            heartbeat_interval_seconds=30,
        )
        resolution_gateway = SdkResolutionGateway(gateway(_RESOLUTION_TOKEN))
        resolution_worker = ResolutionWorker(resolution_gateway, resolution_settings)
        resolution_worker.register()
        outcome = resolution_worker.run_once()
        assert outcome.acquired is True, outcome
        assert outcome.output_digest is not None

        assert _apply_advancement(state, resolution_work_id) == "applied"

    return state


_COUNT_STATEMENTS = {
    "candidates": "SELECT count(*) AS total FROM candidates.candidates",
    "revisions": "SELECT count(*) AS total FROM candidates.candidate_revisions",
    "cases": "SELECT count(*) AS total FROM review.review_cases",
    "work": "SELECT count(*) AS total FROM work.work_units",
    "advancements": "SELECT count(*) AS total FROM work.pipeline_advancements",
}


def _counts(engine: Engine) -> dict[str, int]:
    return {name: _row(engine, statement)["total"] for name, statement in _COUNT_STATEMENTS.items()}


def test_the_discovery_unit_is_persisted_with_the_exact_worker_output(
    pipeline: _Pipeline,
) -> None:
    work = _row(
        pipeline.engine,
        "SELECT state, expected_output_contract, output_digest, active_lease_id, "
        "source_binding_key, attempt_count FROM work.work_units WHERE work_id = :work_id",
        work_id=pipeline.discovery_work_id,
    )

    assert work["state"] == "succeeded"
    assert work["expected_output_contract"] == "wikidata-entity-result@2"
    assert work["source_binding_key"] == _BINDING_KEY
    assert work["output_digest"].startswith("sha256:")
    assert work["active_lease_id"] is None
    assert work["attempt_count"] == 1

    source = _read_source(pipeline, pipeline.discovery_work_id)
    assert source.capability == "wikidata_entity_query"
    assert source.output_contract == "wikidata-entity-result@2"
    assert source.output_artifact.role == "wikidata_entity_result"
    assert source.output_artifact.size_bytes == len(source.output_content or b"")
    raw = next(
        item for item in source.related_output_artifacts if item.role == "wikidata_raw_response"
    )
    # The catalogue re-read the exact captured response out of object storage.
    assert raw.content_digest == _digest(_RESPONSE_PATH.read_bytes())
    assert raw.size_bytes == len(_RESPONSE_PATH.read_bytes())


def test_the_fan_out_persists_one_mapping_unit_per_accepted_item(pipeline: _Pipeline) -> None:
    units = [
        row
        for row in _work_rows(pipeline.engine, pipeline.run_id)
        if row["capability"] == "source_record_mapping"
    ]

    assert len(units) == len(_ACCEPTED_SOURCE_RECORDS)
    assert {row["state"] for row in units} == {"succeeded"}
    assert {row["source_binding_key"] for row in units} == {_BINDING_KEY}
    assert {row["stage"] for row in units} == {"normalization"}
    assert {row["expected_output_contract"] for row in units} == {
        "source-record-observation-batch@1"
    }

    batches = [
        decode_source_record_observation_batch(
            _read_source(pipeline, work_id).output_content or b""
        )
        for work_id in pipeline.mapping_work_ids
    ]
    assert {batch.source_record_id for batch in batches} == _ACCEPTED_SOURCE_RECORDS
    # The ambiguous item was refused by name and never became a mapping unit.
    assert _AMBIGUOUS_SOURCE_RECORD not in {batch.source_record_id for batch in batches}

    advancement = _row(
        pipeline.engine,
        "SELECT state, transition_key, source_binding_key FROM work.pipeline_advancements "
        "WHERE source_work_unit_id = :work_id",
        work_id=pipeline.discovery_work_id,
    )
    assert advancement["state"] == "applied"
    assert advancement["transition_key"] == _WIKIDATA_MAPPING_TRANSITION
    assert advancement["source_binding_key"] == _BINDING_KEY


def test_the_mapping_batches_keep_the_acquisition_time_and_field_provenance(
    pipeline: _Pipeline,
) -> None:
    batches = [
        decode_source_record_observation_batch(
            _read_source(pipeline, work_id).output_content or b""
        )
        for work_id in pipeline.mapping_work_ids
    ]

    assert {batch.source_key for batch in batches} == {"wikidata"}
    assert {batch.mapping_revision for batch in batches} == {
        "wikidata-entity-source-record-mapping-v1"
    }
    hansa = next(item for item in batches if item.source_record_id == "wikidata:Q667023")
    observed = [item for item in hansa.observations if item.state is ObservationState.OBSERVED]
    assert observed
    # Time is provenance: the observation carries the instant the worker read the source, not
    # the mapping run time, and never a future instant.
    assert {item.evidence.observed_at_utc for item in observed if item.evidence} == {_ACQUIRED_AT}
    assert _ACQUIRED_AT < _NOW
    category = next(item for item in observed if item.field_key == "category_recording_studio")
    assert category.value.normalized == "recording_studio"
    assert category.evidence is not None
    assert category.evidence.locator_value.startswith("/entities/Q667023/claims/P31/Q667023$")
    assert category.evidence.source_url == (
        "https://www.wikidata.org/wiki/Special:EntityData/Q667023.json"
    )


def test_every_accepted_item_reached_a_persisted_candidate_under_review(
    pipeline: _Pipeline,
) -> None:
    candidates = _rows(
        pipeline.engine,
        "SELECT candidate_id, entity_kind FROM candidates.candidates ORDER BY candidate_id",
    )
    assert len(candidates) == len(_ACCEPTED_SOURCE_RECORDS)
    assert {item["entity_kind"] for item in candidates} == {"place"}

    revisions = _rows(
        pipeline.engine,
        "SELECT revision, resolution_state, snapshot_digest, source_lineage_digest, "
        "normalized_payload FROM candidates.candidate_revisions ORDER BY candidate_id",
    )
    assert len(revisions) == len(_ACCEPTED_SOURCE_RECORDS)
    # A first intake is revision 0; nothing increments it.
    assert {item["revision"] for item in revisions} == {0}
    assert {item["resolution_state"] for item in revisions} == {"review"}
    assert {item["snapshot_digest"].startswith("sha256:") for item in revisions} == {True}
    assert {item["source_lineage_digest"].startswith("sha256:") for item in revisions} == {True}

    cases = _rows(
        pipeline.engine,
        "SELECT c.candidate_id, r.state, r.reason_codes FROM review.review_cases c "
        "JOIN review.review_case_revisions r ON r.case_id = c.case_id",
    )
    assert {item["candidate_id"] for item in cases} == {item["candidate_id"] for item in candidates}
    assert {item["state"] for item in cases} == {"open"}
    assert all("CANDIDATE_REQUIRES_REVIEW" in item["reason_codes"] for item in cases)

    evidence = _rows(
        pipeline.engine,
        "SELECT candidate_id, count(*) AS total FROM candidates.candidate_revision_evidence "
        "GROUP BY candidate_id",
    )
    assert {item["candidate_id"] for item in evidence} == {
        item["candidate_id"] for item in candidates
    }
    assert all(item["total"] >= 1 for item in evidence)

    for revision in revisions:
        payload = revision["normalized_payload"]
        categories = [
            item
            for item in payload["fieldObservations"]
            if item["fieldKey"] == "category_recording_studio"
        ]
        assert len(categories) == 1
        assert categories[0]["value"]["normalized"] == "recording_studio"
        assert categories[0]["evidence"]["locatorValue"].startswith("/entities/Q"), categories[0][
            "evidence"
        ]
        assert "/claims/P31/Q" in categories[0]["evidence"]["locatorValue"]
        assert categories[0]["evidence"]["observedAtUtc"] == _ACQUIRED_AT.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )


def test_the_run_bundle_carries_the_exact_approved_policy(pipeline: _Pipeline) -> None:
    source = _read_source(pipeline, pipeline.discovery_work_id)
    bundle = source.campaign_bundle_content
    assert bundle is not None
    # The run's own immutable config bundle carries the approved limited policy unchanged.
    document = json.loads(bundle)
    policies = {
        component["path"]: component["content"]
        for component in document["components"]
        if component["path"].startswith("source_policies/")
    }
    wikidata_policy = policies["source_policies/wikidata_berlin_studios_policy.yaml"]
    assert wikidata_policy["legal_status"] == "approved"
    assert wikidata_policy["policy_revision"] == "wikidata-berlin-studios-policy-v2"
    assert wikidata_policy["terms_review"]["state"] == "reviewed"
    assert wikidata_policy["terms_review"]["terms_url"] == (
        "https://creativecommons.org/publicdomain/zero/1.0/"
    )
    assert wikidata_policy["prohibited_fields"] == [
        "review_text",
        "review_author",
        "source_photo",
    ]
    assert _BINDING_KEY in json.dumps(document)

    run = _row(
        pipeline.engine,
        "SELECT campaign_key, config_bundle_digest"
        " FROM runs.collection_runs WHERE run_id = :run_id",
        run_id=pipeline.run_id,
    )
    assert run["campaign_key"] == _CAMPAIGN_KEY
    assert run["config_bundle_digest"].startswith("sha256:")


def _located_candidate_ids(pipeline: _Pipeline) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
    """Split the persisted candidates by the geography decision intake recorded."""

    located: list[UUID] = []
    unlocated: list[UUID] = []
    for row in PostgresExportCandidateReader(pipeline.engine).read_rows():
        geography = row.normalized_payload.get("geography")
        coverage = geography.get("coverage") if isinstance(geography, dict) else None
        (located if coverage in {"inside", "boundary"} else unlocated).append(row.candidate_id)
    return tuple(sorted(located)), tuple(sorted(unlocated))


def test_the_sealed_export_reads_the_approved_candidates(pipeline: _Pipeline) -> None:
    located, unlocated = _located_candidate_ids(pipeline)
    assert len(located) + len(unlocated) == len(_ACCEPTED_SOURCE_RECORDS)
    # Only an observed, in-market point becomes publishable; absence stays absent.
    assert len(located) == len(_LOCATED_SOURCE_RECORDS)
    assert len(unlocated) == len(_ACCEPTED_SOURCE_RECORDS) - len(_LOCATED_SOURCE_RECORDS)

    rows = {
        row.candidate_id: row
        for row in PostgresExportCandidateReader(pipeline.engine).read_rows()
        if row.candidate_id in set(located)
    }
    assert set(rows) == set(located)
    assert {row.legal_status for row in rows.values()} == {"approved"}
    # `approved` is the only status that converts into a publishable usage right.
    assert {row.usage_policy for row in rows.values()} == {"publishable"}
    assert {row.source_key for row in rows.values()} == {"wikidata"}

    sources = PostgresExportCandidatePort(pipeline.engine).read_candidates(
        candidate_revisions=tuple((candidate_id, 0) for candidate_id in located)
    )
    assert len(sources) == len(located)


def test_an_unlocated_candidate_is_not_exportable(pipeline: _Pipeline) -> None:
    _, unlocated = _located_candidate_ids(pipeline)
    assert unlocated

    with pytest.raises(ExportServiceError) as refused:
        PostgresExportCandidatePort(pipeline.engine).read_candidates(
            candidate_revisions=tuple((candidate_id, 0) for candidate_id in unlocated)
        )

    assert refused.value.code == "EXPORT_CANDIDATE_GEOGRAPHY_UNRESOLVED"
    assert refused.value.owner == "CandidateGeography"
    # Absence of a decision is recorded as unknown coverage, never as a valid point.
    assert refused.value.context["geographyCoverage"] == "unknown"


def test_replaying_the_persisted_output_adds_nothing(pipeline: _Pipeline) -> None:
    before = _counts(pipeline.engine)

    # The queue is drained, so the processing worker has nothing to acquire.
    assert pipeline.processing.run_once().acquired is False

    # Re-registering the already applied mapping transitions is idempotent: the same plan
    # digest describes the same successful output, so nothing new is scheduled.
    for work_id in pipeline.mapping_work_ids:
        source = _read_source(pipeline, work_id)
        plan = PipelineTransitionRegistry().plan(source)
        status = pipeline.advancement.register(source, plan, correlation_id="wikidata-replay")
        assert status.state.value == "applied"

    assert _counts(pipeline.engine) == before


def test_a_forged_success_output_cannot_be_advanced(pipeline: _Pipeline) -> None:
    source = _read_source(pipeline, pipeline.discovery_work_id)
    # A worker that claims a different output digest than the store holds must not get a
    # transition scheduled for it.
    forged = replace(source, output_digest=_digest(b"forged output"))
    plan = PipelineTransitionRegistry().plan(forged)
    assert plan.disposition is PipelineTransitionDisposition.APPLY

    with pytest.raises(PipelineAdvancementConflict) as refused:
        pipeline.advancement.register(forged, plan, correlation_id="wikidata-forgery")

    assert refused.value.code == "PIPELINE_SOURCE_OUTPUT_CONFLICT"


_RESOLUTION_WORK_QUERY = (
    "SELECT work_id, capability, stage, state, semantic_key, input_digest, "
    "expected_output_contract, attempt_count FROM work.work_units "
    "WHERE run_id = :run_id AND capability = 'entity_resolution'"
)


def test_the_last_intake_schedules_one_resolution_batch(pipeline: _Pipeline) -> None:
    assert pipeline.resolution_work_id is not None
    work = _row(pipeline.engine, _RESOLUTION_WORK_QUERY, run_id=pipeline.run_id)

    assert work["stage"] == "entity_resolution"
    assert work["expected_output_contract"] == "entity-resolution-snapshot@1"
    # The Work Engine binds a semantic key to an exact digest, so the batch identity is the
    # semantic key and the input digest of the unit that resolves it.
    assert str(work["semantic_key"]).startswith("sha256:")
    assert work["semantic_key"] == work["input_digest"]
    assert work["attempt_count"] == 1

    stage = _row(
        pipeline.engine,
        "SELECT state FROM runs.stage_runs WHERE run_id = :run_id AND stage = 'entity_resolution'",
        run_id=pipeline.run_id,
    )
    assert stage["state"] == "running"

    advancement = _row(
        pipeline.engine,
        "SELECT state, transition_key FROM work.pipeline_advancements "
        "WHERE source_work_unit_id = :work_id",
        work_id=pipeline.resolution_work_id,
    )
    assert advancement["state"] == "applied"
    assert advancement["transition_key"] == "entity-resolution-snapshot-quality-v1"


def test_the_resolution_snapshot_persists_clusters_and_quality(pipeline: _Pipeline) -> None:
    runs = _rows(
        pipeline.engine,
        "SELECT resolution_id, batch_digest, snapshot_digest, market_area_revision, "
        "market_area_digest, quality_policy_digest, supersedes_resolution_id, applied_at_utc "
        "FROM candidates.resolution_runs WHERE run_id = :run_id",
        run_id=pipeline.run_id,
    )
    assert len(runs) == 1
    resolution = runs[0]
    # First resolution for the run: nothing to supersede.
    assert resolution["supersedes_resolution_id"] is None
    assert resolution["batch_digest"].startswith("sha256:")
    assert resolution["snapshot_digest"].startswith("sha256:")
    assert resolution["market_area_digest"].startswith("sha256:")
    assert resolution["market_area_revision"]
    assert resolution["applied_at_utc"] == _NOW

    work = _row(pipeline.engine, _RESOLUTION_WORK_QUERY, run_id=pipeline.run_id)
    assert work["input_digest"] == resolution["batch_digest"]

    members = _rows(
        pipeline.engine,
        "SELECT m.candidate_id, c.lineage_kind FROM candidates.resolution_cluster_members m "
        "JOIN candidates.resolution_clusters c ON c.cluster_id = m.cluster_id "
        "WHERE c.resolution_id = :resolution_id",
        resolution_id=resolution["resolution_id"],
    )
    persisted = _rows(
        pipeline.engine,
        "SELECT candidate_id FROM candidates.candidates",
    )
    # Cluster membership covers the run's candidates exactly: no candidate is dropped and
    # none is invented.
    assert {item["candidate_id"] for item in members} == {
        item["candidate_id"] for item in persisted
    }
    assert {item["lineage_kind"] for item in members} == {"new"}

    evaluations = _rows(
        pipeline.engine,
        "SELECT candidate_id, candidate_revision, export_eligible, policy_digest, "
        "evaluation_digest, blockers FROM quality.quality_evaluations "
        "WHERE resolution_id = :resolution_id",
        resolution_id=resolution["resolution_id"],
    )
    assert {item["candidate_id"] for item in evaluations} == {
        item["candidate_id"] for item in persisted
    }
    assert {item["candidate_revision"] for item in evaluations} == {0}
    assert {item["policy_digest"] for item in evaluations} == {resolution["quality_policy_digest"]}
    assert all(str(item["evaluation_digest"]).startswith("sha256:") for item in evaluations)

    _, unlocated = _located_candidate_ids(pipeline)
    blockers = {item["candidate_id"]: tuple(item["blockers"]) for item in evaluations}
    # Every Berlin studio shares the name token `studio`, so the engine raises a review pair
    # and the sealed profile forbids exporting a candidate whose pairs are still open. That
    # keeps `export_eligible` false for the whole run; geography adds its own blocker for the
    # candidates without an observed point.
    assert {code for codes in blockers.values() for code in codes} >= {
        "PENDING_RESOLUTION_REVIEW",
        "GEOGRAPHY_NOT_ALLOWED",
    }
    assert {cid for cid, codes in blockers.items() if "GEOGRAPHY_NOT_ALLOWED" in codes} == set(
        unlocated
    )
    assert not {item["candidate_id"] for item in evaluations if item["export_eligible"]}


def test_the_resolution_policy_is_the_sealed_campaign_profile(pipeline: _Pipeline) -> None:
    source = _read_source(pipeline, pipeline.resolution_work_id)
    batch = decode_resolution_batch(source.resolution_batch_content or b"")
    resolution = _row(
        pipeline.engine,
        "SELECT quality_policy_digest, market_area_digest FROM candidates.resolution_runs "
        "WHERE run_id = :run_id",
        run_id=pipeline.run_id,
    )

    # The batch embeds the profile's own thresholds and quality rules, and the resolution row
    # names the sealed profile digest the run was authorized with.
    assert resolution["quality_policy_digest"].startswith("sha256:")
    assert resolution["market_area_digest"] == batch.market_area_digest
    assert len(batch.candidates) == len(_ACCEPTED_SOURCE_RECORDS)
    # The batch carries the sealed rules for the entity kinds it actually contains.
    assert {rule.entity_kind.value for rule in batch.quality_rules} == {
        item.entity_kind.value for item in batch.candidates
    }
    assert {rule.pending_review_blocks_export for rule in batch.quality_rules} == {True}
    assert batch.thresholds.candidate_limit == 200
    assert batch.thresholds.name_review_minimum_bps == 8500
    assert {item.candidate_id for item in batch.candidates} == {
        UUID(str(row["candidate_id"]))
        for row in _rows(pipeline.engine, "SELECT candidate_id FROM candidates.candidates")
    }


def test_replaying_the_resolution_output_adds_no_rows(pipeline: _Pipeline) -> None:
    before = _counts(pipeline.engine)
    resolution_before = _row(
        pipeline.engine,
        "SELECT resolution_id, snapshot_digest FROM candidates.resolution_runs "
        "WHERE run_id = :run_id",
        run_id=pipeline.run_id,
    )
    evaluations_before = _row(
        pipeline.engine,
        "SELECT count(*) AS total FROM quality.quality_evaluations",
    )

    source = _read_source(pipeline, pipeline.resolution_work_id)
    plan = PipelineTransitionRegistry().plan(source)
    status = pipeline.advancement.register(source, plan, correlation_id="resolution-replay")
    assert status.state.value == "applied"

    assert _counts(pipeline.engine) == before
    assert (
        _row(
            pipeline.engine,
            "SELECT resolution_id, snapshot_digest FROM candidates.resolution_runs "
            "WHERE run_id = :run_id",
            run_id=pipeline.run_id,
        )
        == resolution_before
    )
    assert (
        _row(pipeline.engine, "SELECT count(*) AS total FROM quality.quality_evaluations")
        == evaluations_before
    )
