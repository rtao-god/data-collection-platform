from __future__ import annotations

import threading
from dataclasses import dataclass
from hashlib import sha256
from time import sleep

from manual_import_core import (
    ManualImportPlanDocumentError,
    ManualImportRecordDocumentError,
    build_manual_import_plan,
    canonical_manual_import_plan_json,
    canonical_manual_import_record_json,
    materialize_manual_import_record,
)
from manual_import_worker.contracts import (
    ManualWorkerCapability,
    ManualWorkerGateway,
    ManualWorkerSettings,
    parse_manual_import_source,
    parse_manual_record_source,
)
from source_connector_sdk import WorkerLease


@dataclass(frozen=True, slots=True)
class ManualWorkRunResult:
    acquired: bool
    work_id: str | None
    capability: ManualWorkerCapability
    output_contract: str
    output_digest: str | None


@dataclass(frozen=True, slots=True)
class _MaterializedOutput:
    payload: bytes
    output_digest: str
    artifact_digest: str


class ManualWorker:
    def __init__(
        self,
        gateway: ManualWorkerGateway,
        settings: ManualWorkerSettings,
    ) -> None:
        self._gateway = gateway
        self._settings = settings

    def register(self) -> None:
        self._gateway.register(self._settings)

    def run_once(self) -> ManualWorkRunResult:
        lease = self._gateway.acquire(self._settings)
        if lease is None:
            return ManualWorkRunResult(
                acquired=False,
                work_id=None,
                capability=self._settings.capability,
                output_contract=self._settings.output.output_contract,
                output_digest=None,
            )
        heartbeat = _LeaseHeartbeat(self._gateway, self._settings, lease)
        try:
            heartbeat.start()
            output = self._materialize(heartbeat)
            upload = self._gateway.publish_output(
                heartbeat.current(),
                output.payload,
                content_digest=output.artifact_digest,
            )
            heartbeat.raise_if_failed()
            completion_lease = heartbeat.stop()
            self._gateway.complete(
                completion_lease,
                output_digest=output.output_digest,
                upload=upload,
            )
            return ManualWorkRunResult(
                acquired=True,
                work_id=str(completion_lease.work_id),
                capability=self._settings.capability,
                output_contract=self._settings.output.output_contract,
                output_digest=output.output_digest,
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
        while True:
            result = self.run_once()
            if not result.acquired:
                sleep(self._settings.poll_interval_seconds)

    def _materialize(self, heartbeat: _LeaseHeartbeat) -> _MaterializedOutput:
        lease = heartbeat.current()
        if self._settings.capability == "manual_import":
            import_source = parse_manual_import_source(lease)
            body = self._gateway.read_artifact(
                lease,
                import_source.artifact,
                max_bytes=self._settings.max_source_bytes,
                timeout_seconds=self._settings.transfer_timeout_seconds,
            )
            heartbeat.raise_if_failed()
            plan = build_manual_import_plan(
                body,
                format=import_source.format,
                mode=import_source.mode,
                max_file_bytes=self._settings.max_source_bytes,
            )
            payload = canonical_manual_import_plan_json(plan).encode("utf-8")
            return _MaterializedOutput(
                payload=payload,
                output_digest=plan.plan_digest,
                artifact_digest=_sha256_identity(payload),
            )
        if self._settings.capability == "manual_record":
            record_source = parse_manual_record_source(lease)
            plan_payload = self._gateway.read_artifact(
                lease,
                record_source.plan_artifact,
                max_bytes=self._settings.max_plan_bytes,
                timeout_seconds=self._settings.transfer_timeout_seconds,
            )
            heartbeat.raise_if_failed()
            document = materialize_manual_import_record(
                plan_payload,
                source_artifact_role=record_source.source_artifact.role,
                plan_record_position=record_source.plan_record_position,
            )
            payload = canonical_manual_import_record_json(document).encode("utf-8")
            return _MaterializedOutput(
                payload=payload,
                output_digest=document.content_digest,
                artifact_digest=_sha256_identity(payload),
            )
        raise RuntimeError("manual worker capability has no materializer owner")


class _LeaseHeartbeat:
    def __init__(
        self,
        gateway: ManualWorkerGateway,
        settings: ManualWorkerSettings,
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
            name=f"manual-work-heartbeat-{self._lease.work_id}",
            daemon=True,
        )
        self._thread.start()

    def current(self) -> WorkerLease:
        self.raise_if_failed()
        with self._lock:
            return self._lease

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("manual work lease heartbeat failed") from self._failure

    def stop(self, *, raise_failure: bool = True) -> WorkerLease:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._settings.heartbeat_interval_seconds + 1)
            if self._thread.is_alive():
                raise RuntimeError("manual work lease heartbeat did not stop")
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


def _sha256_identity(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"


def _classify_failure(exc: BaseException) -> tuple[str, str, str]:
    if isinstance(exc, ManualImportPlanDocumentError):
        return (
            "contract_invalid",
            exc.code,
            "Publish the exact canonical plan artifact and retry the same manual-record work.",
        )
    if isinstance(exc, ManualImportRecordDocumentError):
        return (
            "contract_invalid",
            exc.code,
            "Repair the exact plan-record binding and enqueue a new manual-record work unit.",
        )
    if isinstance(exc, (ValueError, TypeError)):
        return (
            "contract_invalid",
            "MANUAL_WORK_CONTRACT_INVALID",
            "Correct the exact source, plan, or typed work input and enqueue new work.",
        )
    return (
        "transient",
        "MANUAL_WORK_RUNTIME_FAILED",
        "Restore the failing runtime dependency and retry the same semantic work unit.",
    )
