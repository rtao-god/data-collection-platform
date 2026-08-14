from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from time import sleep

from pydantic import ValidationError

from collection_contracts import (
    ExtractionRequest,
    NormalizationProfile,
    canonical_extracted_record_json,
    canonical_observation_batch_json,
    decode_extracted_record,
)
from extraction_core import EmbeddedMetadataExtractor, ExtractionError, extract_html_document
from normalization_core import NormalizationError, normalize_extracted_record
from processing_worker.contracts import ProcessingGateway, ProcessingWorkerSettings
from source_connector_sdk import WorkerGatewayFailure, WorkerLease, WorkFailureKind

_EXTRACTION_INPUT_ROLES = frozenset({"raw_source_document", "extraction_request"})
_NORMALIZATION_INPUT_ROLES = frozenset({"extracted_record", "normalization_profile"})


@dataclass(frozen=True, slots=True)
class ProcessingRunResult:
    acquired: bool
    work_id: str | None
    output_digest: str | None


class ProcessingWorker:
    def __init__(
        self,
        gateway: ProcessingGateway,
        settings: ProcessingWorkerSettings,
        *,
        metadata_extractor: EmbeddedMetadataExtractor | None = None,
    ) -> None:
        self._gateway = gateway
        self._settings = settings
        self._metadata_extractor = metadata_extractor

    def register(self) -> None:
        self._gateway.register(self._settings)

    def run_once(self) -> ProcessingRunResult:
        lease = self._gateway.acquire(self._settings)
        if lease is None:
            return ProcessingRunResult(acquired=False, work_id=None, output_digest=None)
        heartbeat = _LeaseHeartbeat(self._gateway, self._settings, lease)
        try:
            _validate_lease(self._settings, lease)
            heartbeat.start()
            if self._settings.capability == "extraction":
                payload, content_type, role, output_digest = self._extract(heartbeat)
            else:
                payload, content_type, role, output_digest = self._normalize(heartbeat)
            heartbeat.raise_if_failed()
            completion_lease = heartbeat.stop()
            self._gateway.publish_and_complete(
                completion_lease,
                content=payload,
                content_type=content_type,
                output_role=role,
                output_digest=output_digest,
            )
            return ProcessingRunResult(
                acquired=True,
                work_id=str(completion_lease.work_id),
                output_digest=output_digest,
            )
        except Exception as exc:
            failure_lease = heartbeat.stop(raise_failure=False)
            failure_kind, code, action = _classify_failure(exc)
            with contextlib.suppress(WorkerGatewayFailure):
                self._gateway.fail(
                    failure_lease,
                    failure_kind=failure_kind,
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

    def _extract(
        self,
        heartbeat: _LeaseHeartbeat,
    ) -> tuple[bytes, str, str, str]:
        lease = heartbeat.current()
        request_content = self._gateway.read_input(
            lease,
            role="extraction_request",
            maximum_bytes=min(self._settings.maximum_input_bytes, 1024 * 1024),
        )
        raw_content = self._gateway.read_input(
            heartbeat.current(),
            role="raw_source_document",
            maximum_bytes=self._settings.maximum_input_bytes,
        )
        heartbeat.raise_if_failed()
        request = ExtractionRequest.model_validate_json(request_content)
        record = extract_html_document(
            raw_content,
            request,
            metadata_extractor=self._metadata_extractor,
        )
        return (
            canonical_extracted_record_json(record).encode("utf-8"),
            "application/vnd.collection.extracted-record+json",
            "extracted_record",
            record.content_digest,
        )

    def _normalize(
        self,
        heartbeat: _LeaseHeartbeat,
    ) -> tuple[bytes, str, str, str]:
        lease = heartbeat.current()
        record_content = self._gateway.read_input(
            lease,
            role="extracted_record",
            maximum_bytes=self._settings.maximum_input_bytes,
        )
        profile_content = self._gateway.read_input(
            heartbeat.current(),
            role="normalization_profile",
            maximum_bytes=min(self._settings.maximum_input_bytes, 1024 * 1024),
        )
        heartbeat.raise_if_failed()
        record = decode_extracted_record(record_content)
        profile = NormalizationProfile.model_validate_json(profile_content)
        batch = normalize_extracted_record(record, profile)
        return (
            canonical_observation_batch_json(batch).encode("utf-8"),
            "application/vnd.collection.field-observation-batch+json",
            "field_observation_batch",
            batch.content_digest,
        )


class _LeaseHeartbeat:
    def __init__(
        self,
        gateway: ProcessingGateway,
        settings: ProcessingWorkerSettings,
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
            raise RuntimeError("processing lease heartbeat was already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"processing-heartbeat-{self._lease.work_id}",
            daemon=True,
        )
        self._thread.start()

    def current(self) -> WorkerLease:
        self.raise_if_failed()
        with self._lock:
            return self._lease

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("processing lease heartbeat failed") from self._failure

    def stop(self, *, raise_failure: bool = True) -> WorkerLease:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._settings.heartbeat_interval_seconds + 1)
            if self._thread.is_alive():
                raise RuntimeError("processing lease heartbeat did not stop")
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


def _validate_lease(settings: ProcessingWorkerSettings, lease: WorkerLease) -> None:
    if lease.capability != settings.capability or lease.stage != settings.capability:
        raise ValueError("processing lease stage and capability do not match worker identity")
    if lease.expected_output_contract != settings.output_contract:
        raise ValueError("processing lease output contract is unsupported")
    expected_roles = (
        _EXTRACTION_INPUT_ROLES
        if settings.capability == "extraction"
        else _NORMALIZATION_INPUT_ROLES
    )
    actual_roles = frozenset(item.role for item in lease.input_artifacts)
    if actual_roles != expected_roles or len(lease.input_artifacts) != len(expected_roles):
        raise ValueError("processing lease input artifact roles are incomplete or unexpected")


def _classify_failure(exc: BaseException) -> tuple[WorkFailureKind, str, str]:
    if isinstance(exc, ExtractionError):
        return (
            "contract_invalid",
            exc.code,
            "Correct the raw artifact or extraction request and enqueue replacement work.",
        )
    if isinstance(exc, NormalizationError):
        return (
            "contract_invalid",
            exc.code,
            "Correct the extracted record or normalization profile and enqueue replacement work.",
        )
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return (
            "contract_invalid",
            "PROCESSING_CONTRACT_INVALID",
            "Correct the typed processing input and enqueue replacement work.",
        )
    if isinstance(exc, WorkerGatewayFailure):
        return (
            "transient",
            "PROCESSING_GATEWAY_FAILED",
            "Restore Worker Gateway or Object Store connectivity and retry the exact work unit.",
        )
    return (
        "transient",
        "PROCESSING_RUNTIME_FAILED",
        "Restore the failing runtime dependency and retry the exact semantic work unit.",
    )
