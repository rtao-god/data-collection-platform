from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from typing import Any

import httpx
import pytest
import uvicorn
import yaml
from dagster import DagsterInstance
from fastapi import FastAPI

from collection_application import WorkCapability
from collection_application.pipeline_supervision import PipelineSupervisorTick
from worker_gateway import (
    GatewayDependencies,
    WorkerAuthenticator,
    WorkerPrincipal,
    create_app,
)


class _RecordingSupervisor:
    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def run_once(self, **kwargs: Any) -> PipelineSupervisorTick:
        self.invocations.append(kwargs)
        return PipelineSupervisorTick(
            registered_count=0,
            claimed_advancement_id=None,
            terminal_status=None,
        )


def _free_local_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _kill_windows_process_tree(process_id: int) -> None:
    taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
    subprocess.run(  # noqa: S603 -- taskkill only terminates this test's Dagster process tree
        [str(taskkill), "/PID", str(process_id), "/T", "/F"],
        check=False,
        capture_output=True,
        text=True,
    )


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        _kill_windows_process_tree(process.pid)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            _kill_windows_process_tree(process.pid)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def _launch_log(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else "<log was not created>"


@pytest.mark.integration
def test_scheduled_and_webserver_manual_runs_execute_with_daemon_gateway_capability(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    config_root = repository_root / "apps" / "dagster_orchestrator" / "config"
    workspace_path = config_root / "workspace.yaml"
    dagster_home = tmp_path / "dagster-home"
    dagster_home.mkdir()

    dagster_config = yaml.safe_load((config_root / "dagster.yaml").read_text(encoding="utf-8"))
    dagster_config["storage"] = {"sqlite": {"base_dir": str(dagster_home / "sqlite")}}
    (dagster_home / "dagster.yaml").write_text(yaml.safe_dump(dagster_config), encoding="utf-8")

    gateway_token = "pipeline-runtime-token-00000000000000000001"
    worker_token = "gateway-test-worker-token-0000000000000001"
    token_path = tmp_path / "pipeline-token"
    token_path.write_text(gateway_token, encoding="utf-8")
    metadata_password_path = tmp_path / "dagster-metadata-password"
    metadata_password_path.write_text("local-runtime-test-password", encoding="utf-8")

    supervisor = _RecordingSupervisor()
    gateway_app: FastAPI = create_app(
        GatewayDependencies(
            work_engine=None,
            artifact_transfer=None,
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
            pipeline_supervisor=supervisor,
            pipeline_service_token=gateway_token,
        )
    )
    gateway_port = _free_local_port()
    gateway_server = uvicorn.Server(
        uvicorn.Config(
            gateway_app,
            host="127.0.0.1",
            port=gateway_port,
            log_level="error",
        )
    )
    gateway_thread = Thread(target=gateway_server.run, daemon=True)
    gateway_thread.start()

    processes: list[subprocess.Popen[str]] = []
    log_handles = []
    webserver_log_path = tmp_path / "dagster-webserver.log"
    daemon_log_path = tmp_path / "dagster-daemon.log"
    manual_run_id: str | None = None
    try:
        startup_deadline = monotonic() + 10
        while not gateway_server.started and monotonic() < startup_deadline:
            sleep(0.05)
        assert gateway_server.started, "Worker Gateway HTTP server did not start"

        webserver_environment = os.environ.copy()
        for key in (
            "PIPELINE_ADVANCEMENT_GATEWAY_URL",
            "PIPELINE_ADVANCEMENT_TOKEN_FILE",
        ):
            webserver_environment.pop(key, None)
        webserver_environment.update(
            {
                "DAGSTER_HOME": str(dagster_home),
                "DAGSTER_POSTGRES_PASSWORD_FILE": str(metadata_password_path),
                "DAGSTER_POSTGRES_PASSWORD": "local-runtime-test-password",
                "DAGSTER_DISABLE_TELEMETRY": "1",
                "COLLECTOR_BUILD_IDENTITY": "webserver-without-gateway-capability",
            }
        )
        assert "PIPELINE_ADVANCEMENT_GATEWAY_URL" not in webserver_environment
        assert "PIPELINE_ADVANCEMENT_TOKEN_FILE" not in webserver_environment

        daemon_environment = webserver_environment.copy()
        daemon_environment.update(
            {
                "COLLECTOR_BUILD_IDENTITY": "daemon-with-gateway-capability",
                "PIPELINE_ADVANCEMENT_GATEWAY_URL": f"http://127.0.0.1:{gateway_port}",
                "PIPELINE_ADVANCEMENT_TOKEN_FILE": str(token_path),
            }
        )
        webserver_port = _free_local_port()
        webserver_executable = Path(sys.executable).with_name(
            "dagster-webserver.exe" if os.name == "nt" else "dagster-webserver"
        )
        daemon_executable = Path(sys.executable).with_name(
            "dagster-daemon.exe" if os.name == "nt" else "dagster-daemon"
        )

        webserver_log = webserver_log_path.open("w", encoding="utf-8")
        daemon_log = daemon_log_path.open("w", encoding="utf-8")
        log_handles.extend((webserver_log, daemon_log))
        webserver = subprocess.Popen(  # noqa: S603 -- executable comes from the active environment
            [
                str(webserver_executable),
                "-h",
                "127.0.0.1",
                "-p",
                str(webserver_port),
                "-w",
                str(workspace_path),
            ],
            cwd=repository_root,
            env=webserver_environment,
            stdout=webserver_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=os.name != "nt",
        )
        processes.append(webserver)
        daemon = subprocess.Popen(  # noqa: S603 -- executable comes from the active environment
            [str(daemon_executable), "run", "-w", str(workspace_path)],
            cwd=repository_root,
            env=daemon_environment,
            stdout=daemon_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=os.name != "nt",
        )
        processes.append(daemon)

        graphql_url = f"http://127.0.0.1:{webserver_port}/graphql"
        repository_query = (
            "{ repositoriesOrError { ... on RepositoryConnection "
            "{ nodes { name location { name } } } } }"
        )
        repositories: list[dict[str, Any]] = []
        startup_deadline = monotonic() + 60
        while monotonic() < startup_deadline:
            if webserver.poll() is not None or daemon.poll() is not None:
                break
            try:
                response = httpx.post(
                    graphql_url,
                    json={"query": repository_query},
                    timeout=2,
                )
                if response.status_code == 200:
                    result = response.json()
                    if not result.get("errors"):
                        repositories = result["data"]["repositoriesOrError"]["nodes"]
                        if repositories:
                            break
            except httpx.HTTPError:
                pass
            sleep(0.25)

        assert webserver.poll() is None, _launch_log(webserver_log_path)
        assert daemon.poll() is None, _launch_log(daemon_log_path)
        assert repositories, (
            "Dagster webserver did not load its code location. "
            f"webserver={_launch_log(webserver_log_path)}; "
            f"daemon={_launch_log(daemon_log_path)}"
        )
        repository = repositories[0]
        launch_mutation = f'''mutation {{
          launchRun(executionParams: {{
            selector: {{
              repositoryLocationName: "{repository["location"]["name"]}"
              repositoryName: "{repository["name"]}"
              jobName: "pipeline_advancement_job"
            }}
            runConfigData: {{}}
            mode: "default"
            executionMetadata: {{
              tags: [{{key: "launch-origin", value: "webserver-manual"}}]
            }}
          }}) {{
            __typename
            ... on LaunchRunSuccess {{ run {{ runId status }} }}
            ... on PythonError {{ message }}
            ... on RunConfigValidationInvalid {{ errors {{ message }} }}
          }}
        }}'''
        launch_response = httpx.post(
            graphql_url,
            json={"query": launch_mutation},
            timeout=30,
        ).json()
        launch_result = launch_response.get("data", {}).get("launchRun", {})
        assert not launch_response.get("errors"), launch_response
        assert launch_result.get("__typename") == "LaunchRunSuccess", launch_response
        manual_run_id = launch_result["run"]["runId"]

        instance = DagsterInstance.from_config(str(dagster_home))
        try:
            execution_deadline = monotonic() + 100
            manual_run = None
            schedule_runs = []
            invocation_ids: set[str] = set()
            while monotonic() < execution_deadline:
                manual_run = instance.get_run_by_id(manual_run_id)
                schedule_runs = [
                    run
                    for run in instance.get_runs()
                    if run.tags.get("dagster/schedule_name") == "pipeline_advancement_schedule"
                ]
                invocation_ids = {
                    str(item.get("dagster_execution_id")) for item in supervisor.invocations
                }
                successful_schedule_ids = {
                    run.run_id for run in schedule_runs if run.status.value == "SUCCESS"
                }
                manual_succeeded = manual_run is not None and manual_run.status.value == "SUCCESS"
                if (
                    manual_succeeded
                    and successful_schedule_ids
                    and manual_run_id in invocation_ids
                    and successful_schedule_ids.intersection(invocation_ids)
                ):
                    break
                if manual_run is not None and manual_run.status.value == "FAILURE":
                    break
                if webserver.poll() is not None or daemon.poll() is not None:
                    break
                sleep(0.2)

            assert webserver.poll() is None, _launch_log(webserver_log_path)
            assert daemon.poll() is None, _launch_log(daemon_log_path)
            assert manual_run is not None and manual_run.status.value == "SUCCESS", (
                f"Webserver-manual run did not finish successfully: {manual_run}; "
                f"daemon={_launch_log(daemon_log_path)}"
            )
            successful_schedule_runs = [
                run for run in schedule_runs if run.status.value == "SUCCESS"
            ]
            assert successful_schedule_runs, (
                "The Dagster daemon did not finish a scheduled run successfully. "
                f"runs={schedule_runs}; daemon={_launch_log(daemon_log_path)}"
            )
            invocation_ids = {
                str(item.get("dagster_execution_id")) for item in supervisor.invocations
            }
            assert manual_run_id in invocation_ids, (
                "Webserver-queued manual run did not reach the authenticated Gateway. "
                f"invocations={supervisor.invocations}; daemon={_launch_log(daemon_log_path)}"
            )
            assert any(run.run_id in invocation_ids for run in successful_schedule_runs)
            assert all(
                item.get("dagster_build_id") == "daemon-with-gateway-capability"
                for item in supervisor.invocations
            ), supervisor.invocations
        finally:
            instance.dispose()
    finally:
        for process in reversed(processes):
            _stop_process_tree(process)
        for handle in log_handles:
            handle.close()
        gateway_server.should_exit = True
        gateway_thread.join(timeout=10)
