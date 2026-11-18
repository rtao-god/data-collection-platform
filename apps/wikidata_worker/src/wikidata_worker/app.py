from __future__ import annotations

import os
from typing import NoReturn

from source_connector_sdk import SourceWorkerGateway
from wikidata_studios import WikidataEndpointPolicy, WikidataHttpClient
from wikidata_worker.gateway import SdkWikidataWorkerGateway
from wikidata_worker.worker import WikidataWorker, WikidataWorkerPolicy


def main() -> int:
    allowed_hosts = tuple(
        sorted(
            {
                item.strip()
                for item in _required("WIKIDATA_ALLOWED_HOSTS").split(",")
                if item.strip()
            }
        )
    )
    sdk = SourceWorkerGateway(
        base_url=_required("WORKER_GATEWAY_BASE_URL"),
        token=_required("WORKER_GATEWAY_TOKEN"),
        timeout_seconds=_float("WORKER_GATEWAY_TIMEOUT_SECONDS", 30.0),
    )
    gateway = SdkWikidataWorkerGateway(sdk)
    gateway.register(build_identity=_required("WIKIDATA_WORKER_BUILD_IDENTITY"))
    with (
        sdk,
        WikidataHttpClient(
            WikidataEndpointPolicy(
                allowed_hosts=allowed_hosts,
                user_agent=_required("WIKIDATA_USER_AGENT"),
                timeout_seconds=_float("WIKIDATA_TIMEOUT_SECONDS", 60.0),
                maximum_response_bytes=_integer(
                    "WIKIDATA_MAXIMUM_RESPONSE_BYTES",
                    64 * 1024 * 1024,
                ),
            )
        ) as fetcher,
    ):
        worker = WikidataWorker(
            gateway,
            fetcher,
            policy=WikidataWorkerPolicy(
                lease_duration_seconds=_integer("WIKIDATA_LEASE_SECONDS", 300),
                heartbeat_interval_seconds=_integer("WIKIDATA_HEARTBEAT_SECONDS", 60),
                poll_interval_seconds=_float("WIKIDATA_POLL_SECONDS", 5.0),
            ),
        )
        if _boolean("WIKIDATA_WORKER_RUN_ONCE", False):
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
    raise RuntimeError(f"required Wikidata worker setting {name} is missing or invalid")


if __name__ == "__main__":
    raise SystemExit(main())
