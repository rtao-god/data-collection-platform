from __future__ import annotations

import threading
from dataclasses import dataclass
from time import sleep
from typing import Literal, Protocol

from osm_overpass import (
    OverpassFetchFailure,
    OverpassFetchResult,
    OverpassQuerySpec,
    OverpassResponseError,
    decode_query_spec,
    parse_overpass_response,
)
from source_connector_sdk import WorkerLease

type WorkerFailureKind = Literal[
    "transient",
    "permanent",
    "policy_blocked",
    "contract_invalid",
]


class OSMGateway(Protocol):
    def acquire(
        self,
        *,
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> WorkerLease | None: ...

    def heartbeat(
        self,
        lease: WorkerLease,
        *,
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> WorkerLease: ...

    def read_artifact(
        self,
        lease: WorkerLease,
        *,
        role: str,
        maximum_bytes: int,
    ) -> bytes: ...

    def publish_result(
        self,
        lease: WorkerLease,
        *,
        raw_response: bytes,
        observations: bytes,
    ) -> None: ...

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: WorkerFailureKind,
        error_code: str,
        message: str,
        retry_after_seconds: int | None = None,
    ) -> None: ...


class OverpassFetcher(Protocol):
    def fetch(self, spec: OverpassQuerySpec) -> OverpassFetchResult: ...


@dataclass(frozen=True, slots=True)
class OSMWorkerPolicy:
    lease_duration_seconds: int = 300
    heartbeat_interval_seconds: int = 60
    poll_interval_seconds: float = 5.0
    maximum_query_spec_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if not 30 <= self.lease_duration_seconds <= 3_600:
            raise ValueError("OSM worker lease duration is outside the supported range")
        if not 5 <= self.heartbeat_interval_seconds < self.lease_duration_seconds:
            raise ValueError("OSM worker heartbeat interval is invalid")
        if not 0.1 <= self.poll_interval_seconds <= 300:
            raise ValueError("OSM worker poll interval is outside the supported range")
        if not 1_024 <= self.maximum_query_spec_bytes <= 16 * 1024 * 1024:
            raise ValueError("OSM query-spec byte limit is outside the supported range")


class OSMWorker:
    def __init__(
        self,
        gateway: OSMGateway,
        fetcher: OverpassFetcher,
        *,
        policy: OSMWorkerPolicy | None = None,
    ) -> None:
        self._gateway = gateway
        self._fetcher = fetcher
        self._policy = policy or OSMWorkerPolicy()

    def run_once(self) -> bool:
        lease = self._gateway.acquire(
            lease_duration_seconds=self._policy.lease_duration_seconds,
            heartbeat_interval_seconds=self._policy.heartbeat_interval_seconds,
        )
        if lease is None:
            return False
        with _HeartbeatPump(self._gateway, lease, policy=self._policy) as heartbeat:
            try:
                query_spec_body = self._gateway.read_artifact(
                    heartbeat.lease,
                    role="overpass_query_spec",
                    maximum_bytes=self._policy.maximum_query_spec_bytes,
                )
                query_spec = decode_query_spec(query_spec_body)
                fetched = self._fetcher.fetch(query_spec)
                batch = parse_overpass_response(fetched.body, spec=query_spec)
                heartbeat.raise_if_failed()
                self._gateway.publish_result(
                    heartbeat.lease,
                    raw_response=fetched.body,
                    observations=batch.to_bytes(),
                )
            except OverpassFetchFailure as exc:
                heartbeat.raise_if_failed()
                self._gateway.fail(
                    heartbeat.lease,
                    failure_kind=exc.kind,
                    error_code=exc.code,
                    message=str(exc),
                    retry_after_seconds=exc.retry_after_seconds,
                )
            except OverpassResponseError as exc:
                heartbeat.raise_if_failed()
                self._gateway.fail(
                    heartbeat.lease,
                    failure_kind="contract_invalid",
                    error_code=exc.code,
                    message=str(exc),
                )
            except ValueError:
                heartbeat.raise_if_failed()
                self._gateway.fail(
                    heartbeat.lease,
                    failure_kind="contract_invalid",
                    error_code="OSM_WORK_INPUT_INVALID",
                    message="The OSM work input does not satisfy its contract.",
                )
            return True

    def run_forever(self) -> None:
        while True:
            if not self.run_once():
                sleep(self._policy.poll_interval_seconds)


class _HeartbeatPump:
    def __init__(
        self,
        gateway: OSMGateway,
        lease: WorkerLease,
        *,
        policy: OSMWorkerPolicy,
    ) -> None:
        self._gateway = gateway
        self._lease = lease
        self._policy = policy
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"osm-heartbeat-{lease.work_id}",
            daemon=True,
        )

    @property
    def lease(self) -> WorkerLease:
        with self._lock:
            return self._lease

    def __enter__(self) -> _HeartbeatPump:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        self._thread.join(timeout=self._policy.heartbeat_interval_seconds + 1)
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("OSM worker lost its lease heartbeat") from self._failure

    def _run(self) -> None:
        while not self._stop.wait(self._policy.heartbeat_interval_seconds):
            try:
                renewed = self._gateway.heartbeat(
                    self.lease,
                    lease_duration_seconds=self._policy.lease_duration_seconds,
                    heartbeat_interval_seconds=self._policy.heartbeat_interval_seconds,
                )
            except BaseException as exc:
                self._failure = exc
                self._stop.set()
                return
            with self._lock:
                self._lease = renewed
