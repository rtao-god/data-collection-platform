"""The manual-import plan admission as the pipeline actually performs it.

A campaign snapshot is published, a run is bootstrapped with its manual-import discovery
unit, the real manual worker leases that unit through the real Worker Gateway and publishes
the canonical plan, and the real pipeline advancement then admits that plan into child
manual-record work inside its own transaction.

The happy path, the replay, an immutable-input conflict and a rejected plan are covered here
because they are the four states the admission transition can end in. The rollback case drives
a mismatched completion through the same repository and proves the admission was never
persisted.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from db_support import disposable_database
from fastapi.testclient import TestClient
from s3_support import FakeS3Client, S3Transport, object_store
from sqlalchemy.engine import Engine

import manual_import_worker.gateway
from collection_application import (
    ArtifactTransferService,
    CampaignRunService,
    CampaignSnapshotPublicationService,
    CampaignSnapshotService,
    CreateCampaignRun,
    OwnedArtifactPublisherService,
    WorkEngineService,
)
from collection_application.manual_import_admission import (
    MANUAL_RECORD_CAPABILITY,
    MANUAL_RECORD_OUTPUT_CONTRACT,
    MANUAL_RECORD_STAGE,
)
from collection_application.pipeline_advancement import (
    ApplyPipelineAdvancement,
    ClaimPipelineAdvancement,
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionRegistry,
    manual_import_admission_result_digest,
)
from collection_infrastructure import (
    PostgresCampaignRunStore,
    PostgresCampaignSnapshotStore,
    PostgresOwnedArtifactPublisher,
    PostgresWorkEngine,
)
from collection_infrastructure.filesystem_campaign_source import FilesystemCampaignBundleSource
from collection_infrastructure.postgres.artifact_transfer import PostgresArtifactTransfer
from collection_infrastructure.postgres.manual_import_admission import (
    PostgresManualImportAdmissionStore,
)
from collection_infrastructure.postgres.manual_import_admission_applier import (
    manual_import_admission_transition_appliers,
)
from collection_infrastructure.postgres.manual_import_child_writer import (
    PostgresManualImportChildWorkWriter,
)
from collection_infrastructure.postgres.pipeline_advancement import (
    PostgresPipelineAdvancementRepository,
)
from collection_infrastructure.postgres.succeeded_work_catalog import (
    PostgresSucceededWorkCatalog,
)
from manual_import_worker.contracts import ManualWorkerSettings
from manual_import_worker.gateway import SourceWorkerGatewayAdapter
from manual_import_worker.worker import ManualWorker
from source_connector_sdk import SourceWorkerGateway
from worker_gateway import GatewayDependencies, WorkerAuthenticator, WorkerPrincipal, create_app

pytestmark = pytest.mark.integration

_CAMPAIGN_KEY = "berlin_recording_services"
_CAMPAIGN_ROOT = Path(__file__).resolve().parents[2] / "campaigns"
_TRANSITION_KEY = "manual-import-plan-admission"
_NOW = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
_WORKER = "manual-worker@integration"
_TOKEN = "manual-worker-token-" + secrets.token_hex(8)


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
class _DiscoveredRun:
    engine: Engine
    run_id: UUID
    admission_id: UUID
    plan_id: UUID
    catalog: PostgresSucceededWorkCatalog
    advancement: PostgresPipelineAdvancementRepository
    appliers: Mapping[str, Any]


def _rows(engine: Engine, sql: str, **params: object) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.text(sql), params).mappings()]


def _row(engine: Engine, sql: str, **params: object) -> dict[str, Any]:
    rows = _rows(engine, sql, **params)
    assert len(rows) == 1, f"expected one row, got {len(rows)}"
    return rows[0]


def _publish_and_bootstrap(engine: Engine, store: object, campaigns_root: Path) -> UUID:
    artifacts = PostgresOwnedArtifactPublisher(engine, store, clock=lambda: _NOW)  # type: ignore[arg-type]
    publication = CampaignSnapshotPublicationService(
        CampaignSnapshotService(FilesystemCampaignBundleSource(campaigns_root)),
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
            correlation_id="manual-import-admission",
        )
    )
    return run_id


def _gateway(engine: Engine, store: object) -> TestClient:
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
                    _TOKEN: WorkerPrincipal(
                        worker_id=_WORKER,
                        capabilities=frozenset({"manual_import"}),
                    )
                }
            ),
            readiness_probe=readiness_probe,
            expiry_interval_seconds=0,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


class _ObjectStoreHTTPX:
    """The manual worker streams leased inputs straight from the object store.

    Nothing else about the worker changes: the module-level `httpx` it uses is replaced by
    this shim so the presigned URL resolves against the in-process stand-in instead of a real
    object server.
    """

    def __init__(self, transport: httpx.BaseTransport) -> None:
        self._transport = transport

    def Client(self, *args: Any, **kwargs: Any) -> httpx.Client:
        kwargs.setdefault("transport", self._transport)
        return httpx.Client(*args, **kwargs)


def _publish_plan(
    engine: Engine,
    store: object,
    client: FakeS3Client,
    *,
    campaigns_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> UUID:
    """Bootstrap one run and let the real manual worker publish its canonical plan."""

    run_id = _publish_and_bootstrap(engine, store, campaigns_root)
    api = _gateway(engine, store)

    def transport(client: TestClient) -> httpx.BaseTransport:
        return _FastApiTransport(client)

    monkeypatch.setattr(
        manual_import_worker.gateway,
        "httpx",
        _ObjectStoreHTTPX(S3Transport(client)),
    )
    with api:
        sdk = SourceWorkerGateway(
            base_url="http://worker-gateway.test",
            token=_TOKEN,
            gateway_transport=transport(api),
            object_transport=S3Transport(client),
        )
        worker = ManualWorker(
            SourceWorkerGatewayAdapter(sdk),
            ManualWorkerSettings(
                gateway_url="http://worker-gateway.test",
                gateway_token=_TOKEN,
                capability="manual_import",
                build_identity=_WORKER,
                resource_profile="integration",
                poll_interval_seconds=1.0,
                lease_duration_seconds=120,
                heartbeat_interval_seconds=30,
                transfer_timeout_seconds=30.0,
                max_source_bytes=16 * 1024 * 1024,
                max_plan_bytes=64 * 1024 * 1024,
            ),
        )
        worker.register()
        outcome = worker.run_once()
    assert outcome.acquired is True, outcome
    return run_id


def _advancement(
    engine: Engine, store: object
) -> tuple[PostgresPipelineAdvancementRepository, Mapping[str, Any]]:
    catalog = PostgresSucceededWorkCatalog(engine, object_store=store)  # type: ignore[arg-type]
    admission_store = PostgresManualImportAdmissionStore(
        engine,
        PostgresManualImportChildWorkWriter(engine, clock=lambda: _NOW),
    )
    appliers: dict[str, Any] = manual_import_admission_transition_appliers(admission_store)
    repository = PostgresPipelineAdvancementRepository(
        engine,
        source_reader=catalog,
        transition_appliers=appliers,
        clock=lambda: _NOW,
    )
    return repository, appliers


def _register_and_claim(
    advancement: PostgresPipelineAdvancementRepository,
    catalog: PostgresSucceededWorkCatalog,
    engine: Engine,
    run_id: UUID,
) -> tuple[Any, Any]:
    manual_work = _row(
        engine,
        "SELECT work_id FROM work.work_units WHERE run_id = :run_id "
        "AND capability = 'manual_import'",
        run_id=run_id,
    )
    with engine.connect() as connection:
        source = catalog.read(connection, UUID(str(manual_work["work_id"])))
    plan = PipelineTransitionRegistry().plan(source)
    assert plan.transition_key == _TRANSITION_KEY
    assert plan.disposition is PipelineTransitionDisposition.APPLY, plan.blocker
    assert plan.manual_import_admission is not None
    advancement.register(source, plan, correlation_id="manual-import-admission")
    lease = advancement.claim(
        ClaimPipelineAdvancement(
            worker_id="pipeline-supervisor",
            dagster_execution_id="dagster-run",
            dagster_build_id="dagster-build",
            lease_duration=timedelta(minutes=5),
            correlation_id="manual-import-admission",
        )
    )
    assert lease is not None, "the manual-import plan admission was registered as blocked"
    return source, lease


def _apply(advancement: PostgresPipelineAdvancementRepository, lease: Any, digest: str) -> Any:
    return advancement.apply(
        ApplyPipelineAdvancement(
            advancement_id=lease.advancement_id,
            expected_revision=lease.revision,
            lease_id=lease.lease_id,
            lease_token=lease.lease_token,
            dagster_execution_id=lease.dagster_execution_id,
            transition_plan_digest=lease.transition_plan_digest,
            result_digest=digest,
            correlation_id="manual-import-admission",
        )
    )


def _admission_rows(engine: Engine, run_id: UUID) -> list[dict[str, Any]]:
    return _rows(
        engine,
        "SELECT admission_id, parent_work_id, plan_digest, result_digest, child_work_count, "
        "target_stage, target_capability, target_output_contract, source_artifact_role "
        "FROM manual_import.plan_admissions WHERE run_id = :run_id ORDER BY admitted_at_utc",
        run_id=run_id,
    )


def _child_work(engine: Engine, run_id: UUID) -> list[dict[str, Any]]:
    return _rows(
        engine,
        "SELECT work_id, stage, capability, state, expected_output_contract, "
        "source_binding_key, semantic_key FROM work.work_units "
        "WHERE run_id = :run_id AND capability = 'manual_record' ORDER BY semantic_key",
        run_id=run_id,
    )


def test_the_plan_transition_admits_child_manual_record_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.environ.get("COLLECTOR_DATABASE_URL", "").strip():
        pytest.fail("COLLECTOR_DATABASE_URL is required for the manual import admission.")

    for url in disposable_database("manual import admission"):
        engine = sa.create_engine(url, poolclass=sa.NullPool)
        try:
            client = FakeS3Client()
            store = object_store(client)
            run_id = _publish_plan(
                engine,
                store,
                client,
                monkeypatch=monkeypatch,
                campaigns_root=_CAMPAIGN_ROOT,
            )
            advancement, appliers = _advancement(engine, store)
            assert set(appliers) == {_TRANSITION_KEY}
            catalog = PostgresSucceededWorkCatalog(engine, object_store=store)
            source, lease = _register_and_claim(advancement, catalog, engine, run_id)

            plan = PipelineTransitionRegistry().plan(source)
            status = _apply(advancement, lease, manual_import_admission_result_digest(plan))

            assert status.state.value == "applied"
            assert status.result_digest == manual_import_admission_result_digest(plan)
            admissions = _admission_rows(engine, run_id)
            assert len(admissions) == 1
            admission = admissions[0]
            children = _child_work(engine, run_id)
            # The shipped campaign seeds exactly three studios, and every schedulable record
            # becomes its own manual-record unit.
            assert int(admission["child_work_count"]) == 3
            assert len(children) == 3
            assert {row["stage"] for row in children} == {MANUAL_RECORD_STAGE.value}
            assert {row["capability"] for row in children} == {MANUAL_RECORD_CAPABILITY.value}
            assert {row["expected_output_contract"] for row in children} == {
                MANUAL_RECORD_OUTPUT_CONTRACT
            }
            assert {row["state"] for row in children} == {"pending"}
            assert {row["source_binding_key"] for row in children} == {"manual_seed_import"}
            assert len({row["semantic_key"] for row in children}) == 3
            assert admission["target_stage"] == MANUAL_RECORD_STAGE.value
            assert admission["target_capability"] == MANUAL_RECORD_CAPABILITY.value
            assert admission["target_output_contract"] == MANUAL_RECORD_OUTPUT_CONTRACT
            assert str(admission["result_digest"]) == status.result_digest
            assert admission["source_artifact_role"] == "manual_source:csv:atomic"
            items = _rows(
                engine,
                "SELECT position, child_work_id, record_digest "
                "FROM manual_import.plan_admission_items "
                "WHERE admission_id = :admission_id ORDER BY position",
                admission_id=admission["admission_id"],
            )
            assert len(items) == 3
            assert [int(item["position"]) for item in items] == [0, 1, 2]
            assert {str(item["child_work_id"]) for item in items} == {
                str(row["work_id"]) for row in children
            }
        finally:
            engine.dispose()


def test_a_mismatched_completion_rolls_the_admission_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.environ.get("COLLECTOR_DATABASE_URL", "").strip():
        pytest.fail("COLLECTOR_DATABASE_URL is required for the manual import admission.")

    for url in disposable_database("manual import admission rollback"):
        engine = sa.create_engine(url, poolclass=sa.NullPool)
        try:
            client = FakeS3Client()
            store = object_store(client)
            run_id = _publish_plan(
                engine,
                store,
                client,
                monkeypatch=monkeypatch,
                campaigns_root=_CAMPAIGN_ROOT,
            )
            advancement, _ = _advancement(engine, store)
            catalog = PostgresSucceededWorkCatalog(engine, object_store=store)
            _, lease = _register_and_claim(advancement, catalog, engine, run_id)

            with pytest.raises(PipelineAdvancementConflict) as captured:
                _apply(advancement, lease, "sha256:" + "0" * 64)

            assert captured.value.code == "PIPELINE_RESULT_DIGEST_CONFLICT"
            # The applier ran inside the same transaction, so nothing it wrote survives.
            assert _admission_rows(engine, run_id) == []
            assert _child_work(engine, run_id) == []
            state = _row(
                engine,
                "SELECT state FROM work.pipeline_advancements WHERE transition_key = :key",
                key=_TRANSITION_KEY,
            )
            assert state["state"] != "applied"
        finally:
            engine.dispose()


def test_replaying_the_same_plan_never_creates_second_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.environ.get("COLLECTOR_DATABASE_URL", "").strip():
        pytest.fail("COLLECTOR_DATABASE_URL is required for the manual import admission.")

    for url in disposable_database("manual import admission replay"):
        engine = sa.create_engine(url, poolclass=sa.NullPool)
        try:
            client = FakeS3Client()
            store = object_store(client)
            run_id = _publish_plan(
                engine,
                store,
                client,
                monkeypatch=monkeypatch,
                campaigns_root=_CAMPAIGN_ROOT,
            )
            advancement, appliers = _advancement(engine, store)
            catalog = PostgresSucceededWorkCatalog(engine, object_store=store)
            source, lease = _register_and_claim(advancement, catalog, engine, run_id)
            plan = PipelineTransitionRegistry().plan(source)
            _apply(advancement, lease, manual_import_admission_result_digest(plan))

            applier = appliers[_TRANSITION_KEY]
            with engine.begin() as connection:
                replayed = applier(
                    connection,
                    source,
                    plan,
                    correlation_id="manual-import-admission-replay",
                )

            assert replayed == manual_import_admission_result_digest(plan)
            assert len(_admission_rows(engine, run_id)) == 1
            assert len(_child_work(engine, run_id)) == 3
        finally:
            engine.dispose()


def test_an_identity_change_is_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.environ.get("COLLECTOR_DATABASE_URL", "").strip():
        pytest.fail("COLLECTOR_DATABASE_URL is required for the manual import admission.")

    for url in disposable_database("manual import admission identity"):
        engine = sa.create_engine(url, poolclass=sa.NullPool)
        try:
            client = FakeS3Client()
            store = object_store(client)
            run_id = _publish_plan(
                engine,
                store,
                client,
                monkeypatch=monkeypatch,
                campaigns_root=_CAMPAIGN_ROOT,
            )
            advancement, appliers = _advancement(engine, store)
            catalog = PostgresSucceededWorkCatalog(engine, object_store=store)
            source, _ = _register_and_claim(advancement, catalog, engine, run_id)
            plan = PipelineTransitionRegistry().plan(source)
            forged_summary = plan.manual_import_admission.__class__(
                admission_id=uuid4(),
                document_plan_digest=plan.manual_import_admission.document_plan_digest,
                child_work_ids=plan.manual_import_admission.child_work_ids,
            )
            forged = plan.__class__(
                source_work_unit_id=plan.source_work_unit_id,
                transition_key=plan.transition_key,
                disposition=plan.disposition,
                plan_digest=plan.plan_digest,
                blocker=plan.blocker,
                manual_import_admission=forged_summary,
            )

            with (
                pytest.raises(PipelineAdvancementConflict) as captured,
                engine.begin() as connection,
            ):
                appliers[_TRANSITION_KEY](
                    connection,
                    source,
                    forged,
                    correlation_id="manual-import-admission-identity",
                )

            assert captured.value.code == "PIPELINE_MANUAL_IMPORT_ADMISSION_IDENTITY_CHANGED"
            assert _admission_rows(engine, run_id) == []
            assert _child_work(engine, run_id) == []
        finally:
            engine.dispose()
