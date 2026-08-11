from __future__ import annotations

import os
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from collection_application import (
    CollectionRunSpec,
    CollectionRunState,
    RetryPolicy,
    StageRunSpec,
    StageRunState,
    WorkCapability,
    WorkEngineService,
    WorkStage,
    WorkUnitSpec,
)
from collection_infrastructure import PostgresWorkEngine
from worker_gateway import (
    GatewayDependencies,
    WorkerAuthenticator,
    WorkerPrincipal,
    create_app,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
_TOKEN = "gateway-runtime-token-00000000000000000001"


def _database_url() -> str:
    value = os.environ.get("COLLECTOR_DATABASE_URL", "").strip()
    if not value:
        pytest.fail("COLLECTOR_DATABASE_URL is required for Worker Gateway integration tests.")
    return value


def _digest(*parts: str) -> str:
    return f"sha256:{sha256(':'.join(parts).encode('utf-8')).hexdigest()}"


def _insert_ready_snapshot(engine: Engine, label: str) -> tuple[str, str]:
    campaign_key = f"campaign_{label}"
    bundle_digest = _digest(label, "bundle")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO config.config_bundle_components (
                    bundle_digest,
                    position,
                    path,
                    component_digest
                ) VALUES (
                    :bundle_digest,
                    0,
                    'campaign.yaml',
                    :component_digest
                )
                """
            ),
            {
                "bundle_digest": bundle_digest,
                "component_digest": _digest(label, "component"),
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO config.config_bundles (
                    bundle_digest,
                    campaign_key,
                    contract,
                    contract_revision,
                    readiness,
                    recorded_at_utc
                ) VALUES (
                    :bundle_digest,
                    :campaign_key,
                    'collector-campaign-snapshot',
                    'campaign-snapshot-v1',
                    'ready',
                    :recorded_at_utc
                )
                """
            ),
            {
                "bundle_digest": bundle_digest,
                "campaign_key": campaign_key,
                "recorded_at_utc": _NOW,
            },
        )
    return campaign_key, bundle_digest


def test_authenticated_gateway_completes_durable_work(engine: Engine) -> None:
    label = f"gateway_{uuid4().hex}"
    campaign_key, bundle_digest = _insert_ready_snapshot(engine, label)
    adapter = PostgresWorkEngine(engine, clock=lambda: _NOW)
    service = WorkEngineService(adapter)
    run_id = uuid4()
    stage_run_id = uuid4()
    work_id = uuid4()
    output_contract = f"output-{label[-16:]}"
    input_digest = _digest(label, "input")
    output_digest = _digest(label, "output")
    worker_id = f"worker-{label[-16:]}"
    build_identity = f"build-{label[-16:]}"

    service.create_run(
        CollectionRunSpec(
            run_id=run_id,
            campaign_key=campaign_key,
            config_bundle_digest=bundle_digest,
            initial_state=CollectionRunState.RUNNING,
            correlation_id=f"setup-{label}",
        )
    )
    service.create_stage(
        StageRunSpec(
            stage_run_id=stage_run_id,
            run_id=run_id,
            stage=WorkStage.EXTRACTION,
            initial_state=StageRunState.RUNNING,
            correlation_id=f"setup-{label}",
        )
    )
    service.enqueue_work(
        WorkUnitSpec(
            work_id=work_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage=WorkStage.EXTRACTION,
            capability=WorkCapability.EXTRACTION,
            source_key=None,
            semantic_key=_digest(label, "semantic"),
            input_digest=input_digest,
            expected_output_contract=output_contract,
            priority=0,
            retry_policy=RetryPolicy(3, 10, 2, 60),
            available_at_utc=_NOW,
            correlation_id=f"setup-{label}",
        )
    )

    authenticator = WorkerAuthenticator.from_plaintext_credentials(
        {
            _TOKEN: WorkerPrincipal(
                worker_id=worker_id,
                capabilities=frozenset({WorkCapability.EXTRACTION}),
            )
        }
    )

    def readiness_probe() -> None:
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT 1")).scalar_one() == 1

    application = create_app(
        GatewayDependencies(
            work_engine=service,
            authenticator=authenticator,
            readiness_probe=readiness_probe,
            expiry_interval_seconds=0,
        )
    )
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "X-Correlation-Id": f"gateway-{label}",
    }
    with TestClient(application) as client:
        registration = client.post(
            "/worker/registrations",
            headers=headers,
            json={
                "buildIdentity": build_identity,
                "capabilities": ["extraction"],
                "supportedOutputContracts": [output_contract],
                "maxConcurrency": 1,
                "resourceProfile": "processing-small",
            },
        )
        acquired = client.post(
            "/worker/leases/acquire",
            headers=headers,
            json={
                "capability": "extraction",
                "leaseDurationSeconds": 300,
                "heartbeatIntervalSeconds": 60,
            },
        )
        lease = acquired.json()["lease"]
        completed = client.post(
            f"/worker/work/{work_id}/complete",
            headers=headers,
            json={
                "leaseId": lease["leaseId"],
                "leaseToken": lease["leaseToken"],
                "inputDigest": input_digest,
                "outputContract": output_contract,
                "outputDigest": output_digest,
                "workerBuildIdentity": build_identity,
            },
        )

    assert registration.status_code == 200
    assert registration.json() == {"workerId": worker_id, "status": "registered"}
    assert acquired.status_code == 200
    assert acquired.json()["state"] == "acquired"
    assert UUID(lease["workId"]) == work_id
    assert completed.status_code == 200
    assert completed.json()["status"] == "applied"
    assert completed.json()["outputDigest"] == output_digest

    with engine.connect() as connection:
        work = connection.execute(
            sa.text(
                """
                SELECT state, output_contract, output_digest, active_lease_id
                FROM work.work_units
                WHERE work_id = :work_id
                """
            ),
            {"work_id": work_id},
        ).mappings().one()
        active_lease_count = connection.execute(
            sa.text(
                """
                SELECT active_lease_count
                FROM work.worker_heartbeats
                WHERE worker_id = :worker_id
                """
            ),
            {"worker_id": worker_id},
        ).scalar_one()

    assert work == {
        "state": "succeeded",
        "output_contract": output_contract,
        "output_digest": output_digest,
        "active_lease_id": None,
    }
    assert active_lease_count == 0
