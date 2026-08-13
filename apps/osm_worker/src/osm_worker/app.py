from __future__ import annotations

import os
from typing import NoReturn

from osm_overpass import OverpassEndpointPolicy, OverpassHttpClient
from source_connector_sdk import SourceWorkerGateway

from osm_worker.gateway import SdkOsmWorkerGateway
from osm_worker.worker import OSMWorker, OSMWorkerPolicy


def main() -> int:
    endpoint = _required("OVERPASS_ENDPOINT_URL")
    allowed_hosts = tuple(
        sorted(
            {
                item.strip()
                for item in _required("OVERPASS_ALLOWED_HOSTS").split(",")
                if item.strip()
            }
        )
    )
    sdk = SourceWorkerGateway(
        base_url=_required("WORKER_GATEWAY_BASE_URL"),
        token=_required("WORKER_GATEWAY_TOKEN"),
        timeout_seconds=_float("WORKER_GATEWAY_TIMEOUT_SECONDS", 30.0),
    )
    gateway = SdkOsmWorkerGateway(sdk)
    gateway.register(build_identity=_required("OSM_WORKER_BUILD_IDENTITY"))
    with (
        sdk,
        OverpassHttpClient(
            OverpassEndpointPolicy(
                endpoint_url=endpoint,
                allowed_hosts=allowed_hosts,
                user_agent=_required("OVERPASS_USER_AGENT"),
                timeout_seconds=_float("OVERPASS_TIMEOUT_SECONDS", 90.0),
                maximum_response_bytes=_integer(
                    "OVERPASS_MAXIMUM_RESPONSE_BYTES",
                    64 * 1024 * 1024,
                ),
            )
        ) as fetcher,
    ):
        worker = OSMWorker(
            gateway,
            fetcher,
            policy=OSMWorkerPolicy(
                lease_duration_seconds=_integer("OSM_LEASE_SECONDS", 300),
                heartbeat_interval_seconds=_integer("OSM_HEARTBEAT_SECONDS", 60),
                poll_interval_seconds=_float("OSM_POLL_SECONDS", 5.0),
            ),
        )
        if _boolean("OSM_WORKER_RUN_ONCE", False):
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
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    _configuration_error(name)


def _configuration_error(name: str) -> NoReturn:
    raise RuntimeError(f"required OSM worker setting {name} is missing or invalid")


if __name__ == "__main__":
    raise SystemExit(main())
