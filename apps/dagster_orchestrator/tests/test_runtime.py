from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
import yaml
from dagster import DagsterInstance, Definitions, build_schedule_context
from dagster._core.launcher import DefaultRunLauncher
from dagster._core.run_coordinator import QueuedRunCoordinator
from fastapi.testclient import TestClient

from collection_application import WorkCapability
from collection_application.pipeline_supervision import PipelineSupervisorTick
from collector_orchestrator import gateway_client
from collector_orchestrator.definitions import definitions
from collector_orchestrator.gateway_client import GatewayClientError, run_pipeline_advancement
from worker_gateway import (
    GatewayDependencies,
    WorkerAuthenticator,
    WorkerPrincipal,
    create_app,
)

_TOKEN = "dagster-service-token-012345678901234567890123"
_ADVANCEMENT_ID = "019c0000-0000-7000-8000-000000000007"
_WORK_ID = "019c0000-0000-7000-8000-000000000002"
_RUN_ID = "019c0000-0000-7000-8000-000000000008"
_DIGEST = "sha256:" + ("a" * 64)


def _response_payload() -> dict[str, Any]:
    return {
        "contract": "worker-gateway-pipeline-advancement-tick",
        "contractRevision": "pipeline-advancement-tick-v1",
        "registeredCount": 1,
        "claimedAdvancementId": _ADVANCEMENT_ID,
        "terminalStatus": {
            "advancementId": _ADVANCEMENT_ID,
            "sourceWorkUnitId": _WORK_ID,
            "runId": _RUN_ID,
            "state": "blocked",
            "transitionKey": "official_http_robots_route_v1",
            "transitionPlanDigest": _DIGEST,
            "revision": 4,
            "attemptCount": 1,
            "resultDigest": None,
            "blocker": {
                "owner": "PipelineAdvancement",
                "code": "PIPELINE_WEBSITE_ROBOTS_DISALLOWED",
                "message": "The exact seed URL is disallowed by robots policy.",
                "requiredAction": "Do not enqueue page work.",
                "context": {"seedUrl": "https://studio.example/contact"},
            },
            "createdAtUtc": "2026-08-11T12:00:00Z",
            "updatedAtUtc": "2026-08-11T12:00:01Z",
        },
        "discoveryFailures": [
            {
                "sourceWorkUnitId": "019c0000-0000-7000-8000-000000000009",
                "code": "SUCCEEDED_WORK_ARTIFACT_MISSING",
                "message": "The successful work output has no canonical artifact metadata.",
                "requiredAction": "Repair the owning completion record.",
                "context": {"artifactRole": "http_request"},
            }
        ],
    }


def test_gateway_client_sends_typed_request_and_parses_diagnostics(tmp_path: Path) -> None:
    token_file = tmp_path / "pipeline-token"
    token_file.write_text(_TOKEN, encoding="utf-8")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_response_payload())

    result = run_pipeline_advancement(
        dagster_execution_id="dagster-run-123",
        dagster_build_id="build-2026-08-11",
        environment={
            "PIPELINE_ADVANCEMENT_GATEWAY_URL": "http://worker-gateway:8080",
            "PIPELINE_ADVANCEMENT_TOKEN_FILE": str(token_file),
        },
        transport=httpx.MockTransport(respond),
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == ("http://worker-gateway:8080/internal/pipeline-advancement/run-once")
    assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
    payload = json.loads(request.content)
    assert payload == {
        "contract": "worker-gateway-pipeline-advancement-run",
        "contractRevision": "pipeline-advancement-run-v1",
        "dagsterExecutionId": "dagster-run-123",
        "dagsterBuildId": "build-2026-08-11",
    }
    assert "COLLECTOR_DATABASE_URL" not in payload
    assert result.claimed_advancement_id == UUID(_ADVANCEMENT_ID)
    assert result.terminal_status is not None
    assert result.terminal_status.state == "blocked"
    assert result.terminal_status.blocker is not None
    assert result.terminal_status.blocker.code == "PIPELINE_WEBSITE_ROBOTS_DISALLOWED"
    assert result.discovery_failures[0].code == "SUCCEEDED_WORK_ARTIFACT_MISSING"


def test_gateway_client_rejects_gateway_error_without_exposing_token(tmp_path: Path) -> None:
    token_file = tmp_path / "pipeline-token"
    token_file.write_text(_TOKEN, encoding="utf-8")

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    with pytest.raises(GatewayClientError) as error:
        run_pipeline_advancement(
            dagster_execution_id="dagster-run-123",
            dagster_build_id="build-2026-08-11",
            environment={
                "PIPELINE_ADVANCEMENT_GATEWAY_URL": "http://worker-gateway:8080",
                "PIPELINE_ADVANCEMENT_TOKEN_FILE": str(token_file),
            },
            transport=httpx.MockTransport(respond),
        )

    assert error.value.code == "PIPELINE_ADVANCEMENT_GATEWAY_REJECTED"
    assert error.value.context == {"statusCode": 401}
    assert _TOKEN not in str(error.value)


def test_scheduled_dagster_run_calls_authenticated_gateway_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeSupervisor:
        invocation: dict[str, object] | None = None

        def run_once(self, **kwargs: object) -> PipelineSupervisorTick:
            self.invocation = kwargs
            return PipelineSupervisorTick(
                registered_count=0,
                claimed_advancement_id=None,
                terminal_status=None,
            )

    service_token_path = tmp_path / "pipeline-token"
    service_token_path.write_text(_TOKEN, encoding="utf-8")
    supervisor = FakeSupervisor()
    worker_token = "gateway-test-worker-token-0000000000000001"
    gateway_app = create_app(
        GatewayDependencies(
            work_engine=cast(Any, None),
            artifact_transfer=cast(Any, None),
            authenticator=WorkerAuthenticator.from_plaintext_credentials(
                {
                    worker_token: WorkerPrincipal(
                        worker_id="gateway-test-worker",
                        capabilities=frozenset({WorkCapability.HTTP_FETCH}),
                    )
                }
            ),
            readiness_probe=lambda: None,
            expiry_interval_seconds=0,
            pipeline_supervisor=cast(Any, supervisor),
            pipeline_service_token=_TOKEN,
        )
    )
    gateway_requests: list[httpx.Request] = []
    upstream = httpx.Client

    with TestClient(gateway_app) as gateway_server:

        def forward(request: httpx.Request) -> httpx.Response:
            gateway_requests.append(request)
            response = gateway_server.request(
                request.method,
                request.url.path,
                content=request.content,
                headers=request.headers,
            )
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=response.content,
            )

        def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
            kwargs["transport"] = httpx.MockTransport(forward)
            return upstream(*args, **kwargs)

        monkeypatch.setattr(gateway_client.httpx, "Client", client_factory)
        monkeypatch.setenv("COLLECTOR_BUILD_IDENTITY", "compose-test-build")
        monkeypatch.setenv("PIPELINE_ADVANCEMENT_GATEWAY_URL", "http://worker-gateway:8080")
        monkeypatch.setenv("PIPELINE_ADVANCEMENT_TOKEN_FILE", str(service_token_path))

        schedule_tick = definitions.get_schedule_def("pipeline_advancement_schedule").evaluate_tick(
            build_schedule_context()
        )
        assert len(schedule_tick.run_requests) == 1
        run_request = schedule_tick.run_requests[0]
        run = definitions.get_job_def("pipeline_advancement_job").execute_in_process(
            run_config=run_request.run_config or {},
            tags=run_request.tags or {},
        )

    assert run.success
    assert len(gateway_requests) == 1
    request = gateway_requests[0]
    assert request.url.path == "/internal/pipeline-advancement/run-once"
    assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert supervisor.invocation is not None
    assert supervisor.invocation["dagster_build_id"] == "compose-test-build"
    assert supervisor.invocation["registration_limit"] == 100
    assert supervisor.invocation["worker_id"] == "dagster-pipeline-advancement"


def test_instance_config_queues_runs_for_the_daemon(tmp_path: Path) -> None:
    config_path = Path(__file__).parents[1] / "config" / "dagster.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["storage"] = {"sqlite": {"base_dir": str(tmp_path)}}
    (tmp_path / "dagster.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    instance = DagsterInstance.from_config(str(tmp_path))
    try:
        assert isinstance(instance.run_coordinator, QueuedRunCoordinator)
        assert isinstance(instance.run_launcher, DefaultRunLauncher)
    finally:
        instance.dispose()


def test_dagster_definitions_are_loadable_without_collection_database_configuration() -> None:
    assert isinstance(definitions, Definitions)
    assert definitions.get_job_def("pipeline_advancement_job").name == ("pipeline_advancement_job")
    assert definitions.get_schedule_def("pipeline_advancement_schedule").name == (
        "pipeline_advancement_schedule"
    )
    Definitions.validate_loadable(definitions)
