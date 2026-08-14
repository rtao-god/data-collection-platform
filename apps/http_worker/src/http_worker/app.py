from __future__ import annotations

import os
from typing import NoReturn

from http_worker.gateway import SdkHttpWorkerGateway
from http_worker.worker import HttpWorker, HttpWorkerPolicy
from official_http import ScrapyChildFetcher
from source_connector_sdk import SourceWorkerGateway


def main() -> int:
    sdk = SourceWorkerGateway(
        base_url=_required("WORKER_GATEWAY_BASE_URL"),
        token=_required("WORKER_GATEWAY_TOKEN"),
        timeout_seconds=_float("WORKER_GATEWAY_TIMEOUT_SECONDS", 30.0),
    )
    gateway = SdkHttpWorkerGateway(sdk)
    gateway.register(build_identity=_required("HTTP_WORKER_BUILD_IDENTITY"))
    with sdk:
        worker = HttpWorker(
            gateway,
            ScrapyChildFetcher(),
            policy=HttpWorkerPolicy(
                lease_duration_seconds=_integer("HTTP_LEASE_SECONDS", 300),
                heartbeat_interval_seconds=_integer("HTTP_HEARTBEAT_SECONDS", 60),
                poll_interval_seconds=_float("HTTP_POLL_SECONDS", 5.0),
                maximum_request_bytes=_integer("HTTP_REQUEST_MAXIMUM_BYTES", 1024 * 1024),
            ),
        )
        if _boolean("HTTP_WORKER_RUN_ONCE", False):
            worker.run_once()
        else:
            worker.run_forever()
    return 0


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        _configuration_error(name)
    return value.strip()


def _integer(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        _configuration_error(name)


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        _configuration_error(name)


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    _configuration_error(name)


def _configuration_error(name: str) -> NoReturn:
    raise RuntimeError(f"required HTTP worker setting {name} is missing or invalid")


if __name__ == "__main__":
    raise SystemExit(main())
