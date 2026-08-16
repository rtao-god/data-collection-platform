from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa
import uvicorn
from fastapi import FastAPI
from sqlalchemy.engine import Engine

from collection_application import ArtifactTransferService, WorkEngineService
from collection_infrastructure import (
    PostgresArtifactTransfer,
    PostgresWorkEngine,
    S3ArtifactObjectStore,
)
from worker_gateway.app import GatewayDependencies, create_app
from worker_gateway.auth import WorkerAuthenticator

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080
_DEFAULT_EXPIRY_INTERVAL_SECONDS = 5.0
_DEFAULT_EXPIRY_BATCH_SIZE = 100
_DEFAULT_BIND_MODE = "local"
_CONTAINER_BIND_MODE = "container"
_CONTAINER_HOST = "0.0.0.0"  # noqa: S104 - validated container-only bind.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def build_runtime() -> tuple[FastAPI, Engine]:
    database_url = _required_environment("COLLECTOR_DATABASE_URL")
    credential_file = Path(_required_environment("WORKER_GATEWAY_TOKEN_FILE"))
    authenticator = WorkerAuthenticator.from_secret_file(credential_file)

    object_store = S3ArtifactObjectStore.create(
        endpoint_url=_required_environment("COLLECTOR_OBJECT_STORE_ENDPOINT"),
        bucket=_required_environment("COLLECTOR_OBJECT_STORE_BUCKET"),
        access_key_id=_read_secret_file(
            Path(_required_environment("COLLECTOR_OBJECT_STORE_ACCESS_KEY_FILE"))
        ),
        secret_access_key=_read_secret_file(
            Path(_required_environment("COLLECTOR_OBJECT_STORE_SECRET_KEY_FILE"))
        ),
        region_name=_required_environment("COLLECTOR_OBJECT_STORE_REGION"),
    )
    expiry_interval = _float_environment(
        "WORKER_GATEWAY_EXPIRY_INTERVAL_SECONDS",
        _DEFAULT_EXPIRY_INTERVAL_SECONDS,
        minimum=0,
        maximum=3_600,
    )
    expiry_batch_size = _integer_environment(
        "WORKER_GATEWAY_EXPIRY_BATCH_SIZE",
        _DEFAULT_EXPIRY_BATCH_SIZE,
        minimum=1,
        maximum=1_000,
    )
    engine = sa.create_engine(database_url, pool_pre_ping=True)
    work_engine = WorkEngineService(PostgresWorkEngine(engine))
    artifact_transfer = ArtifactTransferService(PostgresArtifactTransfer(engine, object_store))

    def readiness_probe() -> None:
        with engine.connect() as connection:
            if connection.execute(sa.text("SELECT 1")).scalar_one() != 1:
                raise RuntimeError("PostgreSQL readiness probe returned an unexpected result")
        object_store.assert_ready()

    application = create_app(
        GatewayDependencies(
            work_engine=work_engine,
            artifact_transfer=artifact_transfer,
            authenticator=authenticator,
            readiness_probe=readiness_probe,
            expiry_interval_seconds=expiry_interval,
            expiry_batch_size=expiry_batch_size,
        )
    )
    return application, engine


def main() -> None:
    host = _bind_host_from_environment()
    port = _integer_environment(
        "WORKER_GATEWAY_PORT",
        _DEFAULT_PORT,
        minimum=1,
        maximum=65_535,
    )
    application, engine = build_runtime()
    try:
        uvicorn.run(
            application,
            host=host,
            port=port,
            workers=1,
            proxy_headers=False,
            server_header=False,
            timeout_keep_alive=5,
        )
    finally:
        engine.dispose()


def _bind_host_from_environment() -> str:
    mode = os.environ.get("WORKER_GATEWAY_BIND_MODE", _DEFAULT_BIND_MODE).strip().casefold()
    if mode == _DEFAULT_BIND_MODE:
        host = os.environ.get("WORKER_GATEWAY_HOST", _DEFAULT_HOST).strip()
        if host not in _LOCAL_HOSTS:
            raise RuntimeError(
                "Worker Gateway refuses a non-local bind unless WORKER_GATEWAY_BIND_MODE=container"
            )
        return host
    if mode == _CONTAINER_BIND_MODE:
        host = os.environ.get("WORKER_GATEWAY_HOST", _CONTAINER_HOST).strip()
        if host != _CONTAINER_HOST:
            raise RuntimeError(
                "Worker Gateway container bind mode requires WORKER_GATEWAY_HOST=0.0.0.0"
            )
        return host
    raise RuntimeError("WORKER_GATEWAY_BIND_MODE must be local or container")


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _read_secret_file(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read required secret file: {path}") from exc
    if not value:
        raise RuntimeError(f"required secret file is empty: {path}")
    if len(value) > 4_096:
        raise RuntimeError(f"required secret file is too large: {path}")
    return value


def _integer_environment(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float_environment(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


if __name__ == "__main__":
    main()
