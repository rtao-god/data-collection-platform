from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from time import sleep

from entity_resolution_core import ResolutionError
from resolution_contracts import (
    canonical_resolution_snapshot_json,
    decode_resolution_batch,
)
from resolution_worker.contracts import ResolutionGateway, ResolutionWorkerSettings
from resolution_worker.snapshot import build_resolution_snapshot
from source_connector_sdk import WorkerGatewayFailure, WorkerLease, WorkFailureKind

_INPUT_ROLE = "resolution_batch"
_OUTPUT_ROLE = "resolution_snapshot"
_OUTPUT_CONTENT_TYPE = "application/vnd.collection.entity-resolution-snapshot+json"


@dataclass(frozen=True, slots=True)
class ResolutionRunResult:
    acquired: bool
    work_id: str | None
    output_digest: str | None


class ResolutionWorker:
    def __init__(
        self,
        gateway: ResolutionGateway,
        settings: ResolutionWorkerSettings,
    ) -> None:
        self._gateway = gateway
        self._settings = settings

    def register(self) -> None:
        self._gateway.register(self._settings)

    def run_once(self) -> ResolutionRunResult:
        lease = self._gateway.acquire(self._settings)
        if lease is None:
            return ResolutionRunResult(acquired=False, work_id=None, output_digest=None)
        heartbeat = _LeaseHeartbeat(self._gateway, self._settings, lease)
        try:
            _validate_lease(lease)
            heartbeat.start()
            content = self._gateway.read_input(
                heartbeat.current(),
                role=_INPUT_ROLE,
                maximum_bytes=self._settings.maximum_input_bytes,
            )
            heartbeat.raise_if_failed()
            batch = decode_resolution_batch(content)
            snapshot = build_resolution_snapshot(batch)
            payload = canonical_resolution_snapshot_json(snapshot).encode("utf-8")
            completion_lease = heartbeat.stop()
            self._gateway.publish_and_complete(
                completion_lease,
                content=payload,
                content_type=_OUTPUT_CONTENT_TYPE,
                output_role=_OUTPUT_ROLE,
                output_digest=snapshot.snapshot_digest,
            )
            return ResolutionRunResult(
                acquired=True,
                work_id=str(completion_lease.work_id),
                output_digest=snapshot.snapshot_digest,
            )
        except Exception as exc:
            failure_lease = heartbeat.stop(raise_failure=False)
            failure_kind, code, action = _classify_failure(exc)
            with contextlib.suppress(WorkerGatewayFailure):
                self._gateway.fail(
                    failure_lease,
                    failure_kind=failure_kind,
                    code=code,
                    message=_safe_failure_message(exc),
                    required_action=action,
                )
            raise

    def run_forever(self) -> None:
        self.register()
        while True:
            result = self.run_once()
            if not result.acquired:
                sleep(self._settings.poll_interval_seconds)


class ResolutionHeartbeatError(RuntimeError):
    pass


class _LeaseHeartbeat:
    def __init__(
        self,
        gateway: ResolutionGateway,
        settings: ResolutionWorkerSettings,
        lease: WorkerLease,
    ) -> None:
        self._gateway = gateway
        self._settings = settings
        self._lease = lease
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("resolution lease heartbeat was already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"resolution-heartbeat-{self._lease.work_id}",
            daemon=True,
        )
        self._thread.start()

    def current(self) -> WorkerLease:
        self.raise_if_failed()
        with self._lock:
            return self._lease

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise ResolutionHeartbeatError("resolution lease heartbeat failed") from self._failure

    def stop(self, *, raise_failure: bool = True) -> WorkerLease:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._settings.heartbeat_interval_seconds + 1)
            if self._thread.is_alive():
                raise RuntimeError("resolution lease heartbeat did not stop")
        if raise_failure:
            self.raise_if_failed()
        with self._lock:
            return self._lease

    def _run(self) -> None:
        while not self._stop.wait(self._settings.heartbeat_interval_seconds):
            try:
                with self._lock:
                    current = self._lease
                renewed = self._gateway.heartbeat(current, self._settings)
                with self._lock:
                    self._lease = renewed
            except BaseException as exc:
                self._failure = exc
                self._stop.set()
                return


def _validate_lease(lease: WorkerLease) -> None:
    if lease.stage != "entity_resolution" or lease.capability != "entity_resolution":
        raise ValueError("resolution lease stage and capability are incompatible")
    if lease.source_permit is not None:
        raise ValueError("entity-resolution work must not carry a source permit")
    if lease.expected_output_contract != "entity-resolution-snapshot@1":
        raise ValueError("resolution lease output contract is unsupported")
    roles = tuple(item.role for item in lease.input_artifacts)
    if roles != (_INPUT_ROLE,):
        raise ValueError("resolution lease requires exactly one canonical batch artifact")


def _classify_failure(exc: BaseException) -> tuple[WorkFailureKind, str, str]:
    if isinstance(exc, ResolutionError):
        return (
            "permanent",
            exc.code,
            "Correct the immutable resolution batch and enqueue replacement work.",
        )
    if isinstance(exc, (ValueError, TypeError)):
        return (
            "permanent",
            "RESOLUTION_CONTRACT_INVALID",
            "Correct the canonical resolution input and enqueue replacement work.",
        )
    if isinstance(exc, (WorkerGatewayFailure, ResolutionHeartbeatError)):
        return (
            "transient",
            "RESOLUTION_GATEWAY_FAILED",
            "Restore Worker Gateway or Object Store connectivity and retry the exact work unit.",
        )
    return (
        "permanent",
        "RESOLUTION_ENGINE_DEFECT",
        "Correct the deterministic resolution owner before retrying this semantic work unit.",
    )


def _safe_failure_message(exc: BaseException) -> str:
    if isinstance(exc, ResolutionError):
        return str(exc)[:500]
    return type(exc).__name__
