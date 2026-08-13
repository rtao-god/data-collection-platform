from __future__ import annotations

import threading
from dataclasses import dataclass
from hashlib import sha256
from time import sleep

from collection_contracts import ManualImportPlan
from manual_import_core import (
    build_manual_import_plan,
    canonical_manual_import_plan_json,
)
from manual_import_worker.contracts import (
    ManualImportGateway,
    ManualImportSource,
    ManualImportWorkerSettings,
    parse_manual_import_source,
)
from source_connector_sdk import WorkerLease


@dataclass(frozen=True, slots=True)
class ManualImportRunResult:
    acquired: bool
    work_id: str | None
    plan_digest: str | None


class ManualImportWorker:
    def __init__(
        self,
        gateway: ManualImportGateway,
        settings: ManualImportWorkerSettings,
    ) -> None:
        self._gateway = gateway
        self._settings = settings

    def register(self) -> None:
        self._gateway.register(self._settings)

    def run_once(self) -> ManualImportRunResult:
        lease = self._gateway.acquire(self._settings)
        if lease is None:
            return ManualImportRunResult(acquired=False, work_id=None, plan_digest=None)
        heartbeat = _LeaseHeartbeat(self._gateway, self._settings, lease)
        try:
            source = parse_manual_import_source(lease)
            heartbeat.start()
            body = self._gateway.read_source(
                heartbeat.current(),
                source.artifact,
                max_bytes=self._settings.max_source_bytes,
                timeout_seconds=self._settings.transfer_timeout_seconds,
            )
            heartbeat.raise_if_failed()
            plan = _build_plan(body, source)
            payload = canonical_manual_import_plan_json(plan).encode("utf-8")
            artifact_digest = _sha256_identity(payload)
            plan_digest = plan.plan_digest
            upload = self._gateway.publish_plan(
                heartbeat.current(),
                payload,
                content_digest=artifact_digest,
                timeout_seconds=self._settings.transfer_timeout_seconds,
            )
            heartbeat.raise_if_failed()
            completion_lease = heartbeat.stop()
            self._gateway.complete(
                completion_lease,
                plan_digest=plan_digest,
                upload=upload,
            )
            return ManualImportRunResult(
                acquired=True,
                work_id=str(completion_lease.work_id),
                plan_digest=plan_digest,
            )
        except Exception as exc:
            failure_lease = heartbeat.stop(raise_failure=False)
            kind, code, action = _classify_failure(exc)
            self._gateway.fail(
                failure_lease,
                failure_kind=kind,
                code=code,
                message=str(exc) or type(exc).__name__,
                required_action=action,
            )
            raise

    def run_forever(self) -> None:
        self.register()
        while True:
            result = self.run_once()
            if not result.acquired:
                sleep(self._settings.poll_interval_seconds)


class _LeaseHeartbeat:
    def __init__(
        self,
        gateway: ManualImportGateway,
        settings: ManualImportWorkerSettings,
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
            raise RuntimeError("lease heartbeat was already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"manual-import-heartbeat-{self._lease.work_id}",
            daemon=True,
        )
        self._thread.start()

    def current(self) -> WorkerLease:
        self.raise_if_failed()
        with self._lock:
            return self._lease

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("manual import lease heartbeat failed") from self._failure

    def stop(self, *, raise_failure: bool = True) -> WorkerLease:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._settings.heartbeat_interval_seconds + 1)
            if self._thread.is_alive():
                raise RuntimeError("manual import lease heartbeat did not stop")
        if raise_failure:
            self.raise_if_failed()
        with self._lock:
            return self._lease

    def _run(self) -> None:
        interval = self._settings.heartbeat_interval_seconds
        while not self._stop.wait(interval):
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


def _build_plan(body: bytes, source: ManualImportSource) -> ManualImportPlan:
    return build_manual_import_plan(
        body,
        format=source.format,
        mode=source.mode,
    )


def _sha256_identity(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"


def _classify_failure(exc: BaseException) -> tuple[str, str, str]:
    if isinstance(exc, (ValueError, TypeError)):
        return (
            "contract_invalid",
            "MANUAL_IMPORT_CONTRACT_INVALID",
            "Correct the source artifact or typed work input and enqueue a new work unit.",
        )
    return (
        "transient",
        "MANUAL_IMPORT_RUNTIME_FAILED",
        "Restore the failing runtime dependency and retry the same semantic work unit.",
    )
