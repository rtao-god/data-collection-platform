from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import sleep
from typing import Protocol

from collection_contracts import (
    OwnerContextError,
    WikidataEntityQuerySpec,
    decode_wikidata_query_spec,
)
from source_connector_sdk import WorkerLease, WorkFailureKind
from wikidata_studios import (
    WikidataEntityRequest,
    WikidataFetchFailure,
    WikidataFetchResult,
    seal_entity_result,
)


class WikidataGateway(Protocol):
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
        result_envelope: bytes,
    ) -> None: ...

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: WorkFailureKind,
        error_code: str,
        message: str,
        retry_after_seconds: int | None = None,
    ) -> None: ...


class WikidataFetcher(Protocol):
    def fetch(self, request: WikidataEntityRequest) -> WikidataFetchResult: ...


@dataclass(frozen=True, slots=True)
class WikidataWorkerPolicy:
    lease_duration_seconds: int = 300
    heartbeat_interval_seconds: int = 60
    poll_interval_seconds: float = 5.0
    maximum_query_spec_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        if not 30 <= self.lease_duration_seconds <= 3_600:
            raise ValueError("Wikidata worker lease duration is outside the supported range")
        if not 5 <= self.heartbeat_interval_seconds < self.lease_duration_seconds:
            raise ValueError("Wikidata worker heartbeat interval is invalid")
        if not 0.1 <= self.poll_interval_seconds <= 300:
            raise ValueError("Wikidata worker poll interval is outside the supported range")
        if not 1_024 <= self.maximum_query_spec_bytes <= 16 * 1024 * 1024:
            raise ValueError("Wikidata query-spec byte limit is outside the supported range")


class WikidataWorker:
    """Performs exactly one bounded entity read per leased work unit."""

    def __init__(
        self,
        gateway: WikidataGateway,
        fetcher: WikidataFetcher,
        *,
        policy: WikidataWorkerPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway = gateway
        self._fetcher = fetcher
        self._policy = policy or WikidataWorkerPolicy()
        # The acquisition time is the only wall-clock value the worker contributes, and it
        # is published inside the sealed envelope so the record keeps its own read time.
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_once(self) -> bool:
        lease = self._gateway.acquire(
            lease_duration_seconds=self._policy.lease_duration_seconds,
            heartbeat_interval_seconds=self._policy.heartbeat_interval_seconds,
        )
        if lease is None:
            return False
        with _HeartbeatPump(self._gateway, lease, policy=self._policy) as heartbeat:
            try:
                spec = self._read_query_spec(heartbeat.lease)
                request = WikidataEntityRequest(
                    entity_ids=spec.entity_ids,
                    retrieved_at_utc=self._now_utc(),
                    run_id=spec.run_id,
                )
                fetched = self._fetcher.fetch(request)
                # Adaptation happens here, in the worker that owns the connector, so the
                # published envelope already carries the accepted records and the refusals.
                result = seal_entity_result(
                    fetched.body,
                    spec=spec,
                    retrieved_at_utc=request.retrieved_at_utc,
                )
                heartbeat.raise_if_failed()
                self._gateway.publish_result(
                    heartbeat.lease,
                    raw_response=result.raw_content,
                    result_envelope=result.envelope_content,
                )
            except WikidataFetchFailure as exc:
                heartbeat.raise_if_failed()
                self._gateway.fail(
                    heartbeat.lease,
                    failure_kind=exc.kind,
                    error_code=exc.code,
                    message=str(exc),
                    retry_after_seconds=exc.retry_after_seconds,
                )
            except (ValueError, OwnerContextError) as exc:
                # A result that answers another entity set, or a specification that is not
                # the bytes its producer published, is a contract failure of this work unit
                # and must be reported as one instead of crashing the worker.
                heartbeat.raise_if_failed()
                self._gateway.fail(
                    heartbeat.lease,
                    failure_kind="contract_invalid",
                    error_code="WIKIDATA_WORK_INPUT_INVALID",
                    message=f"The Wikidata work input does not satisfy its contract: {exc}",
                )
            return True

    def run_forever(self) -> None:
        while True:
            if not self.run_once():
                sleep(self._policy.poll_interval_seconds)

    def _read_query_spec(self, lease: WorkerLease) -> WikidataEntityQuerySpec:
        return decode_wikidata_query_spec(
            self._gateway.read_artifact(
                lease,
                role="wikidata_entity_query_spec",
                maximum_bytes=self._policy.maximum_query_spec_bytes,
            )
        )

    def _now_utc(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            raise ValueError("the acquisition clock returned no timezone-aware time")
        return moment.astimezone(UTC)


class _HeartbeatPump:
    def __init__(
        self,
        gateway: WikidataGateway,
        lease: WorkerLease,
        *,
        policy: WikidataWorkerPolicy,
    ) -> None:
        self._gateway = gateway
        self._lease = lease
        self._policy = policy
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"wikidata-heartbeat-{lease.work_id}",
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
            raise RuntimeError("Wikidata worker lost its lease heartbeat") from self._failure

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
