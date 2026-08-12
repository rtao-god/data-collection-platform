from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import cast
from uuid import UUID, uuid4

import httpx

from source_connector_sdk import (
    ArtifactKind,
    LeaseArtifact,
    PreparedRead,
    PreparedUpload,
    SourceWorkerGateway,
    VerifiedUpload,
    WorkerLease,
)

from manual_import_worker.contracts import ManualImportWorkerSettings

_PLAN_CONTENT_TYPE = "application/vnd.collection.manual-import-plan+json"
_PLAN_OUTPUT_ROLE = "manual_import_plan"
_PLAN_OUTPUT_CONTRACTS = frozenset({"manual-import-plan", "manual-import-plan@1"})


class SourceWorkerGatewayAdapter:
    """Keeps Gateway authentication separate from pre-signed object transfer."""

    def __init__(self, client: SourceWorkerGateway) -> None:
        self._client = client

    def register(self, settings: ManualImportWorkerSettings) -> None:
        self._invoke(
            "register",
            {
                "build_identity": settings.build_identity,
                "capabilities": {"manual_import"},
                "supported_output_contracts": _PLAN_OUTPUT_CONTRACTS,
                "max_concurrency": 1,
                "resource_profile": settings.resource_profile,
            },
        )

    def acquire(self, settings: ManualImportWorkerSettings) -> WorkerLease | None:
        result = self._invoke(
            "acquire_lease",
            {
                "capability": "manual_import",
                "lease_duration_seconds": settings.lease_duration_seconds,
                "heartbeat_interval_seconds": settings.heartbeat_interval_seconds,
            },
        )
        return cast(WorkerLease | None, result)

    def heartbeat(
        self, lease: WorkerLease, settings: ManualImportWorkerSettings
    ) -> WorkerLease:
        result = self._invoke(
            "heartbeat",
            self._lease_values(
                lease,
                lease_duration_seconds=settings.lease_duration_seconds,
                heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
            ),
        )
        return cast(WorkerLease, result)

    def read_source(
        self,
        lease: WorkerLease,
        source: LeaseArtifact,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        prepared = cast(
            PreparedRead,
            self._invoke(
                "prepare_read",
                self._lease_values(lease, artifact_id=source.artifact_id),
            ),
        )
        body = bytearray()
        with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
            with client.stream(prepared.method, prepared.url) as response:
                if not 200 <= response.status_code < 300:
                    raise RuntimeError(
                        f"scoped artifact read failed with status {response.status_code}"
                    )
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError("manual import source exceeds the configured byte limit")
        return bytes(body)

    def publish_plan(
        self,
        lease: WorkerLease,
        payload: bytes,
        *,
        content_digest: str,
        timeout_seconds: float,
    ) -> VerifiedUpload:
        upload_id = uuid4()
        prepared = cast(
            PreparedUpload,
            self._invoke(
                "prepare_upload",
                self._lease_values(
                    lease,
                    upload_id=upload_id,
                    artifact_kind=cast(ArtifactKind, "derived_artifact"),
                    expected_digest=content_digest,
                    expected_size_bytes=len(payload),
                    content_type=_PLAN_CONTENT_TYPE,
                    expires_in_seconds=900,
                ),
            ),
        )
        with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
            response = client.request(
                prepared.method,
                prepared.url,
                headers=dict(prepared.required_headers),
                content=payload,
            )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"scoped artifact upload failed with status {response.status_code}"
            )
        return cast(
            VerifiedUpload,
            self._invoke(
                "verify_upload",
                self._lease_values(lease, upload_id=upload_id),
            ),
        )

    def complete(
        self, lease: WorkerLease, *, plan_digest: str, upload: object
    ) -> None:
        verified = cast(VerifiedUpload, upload)
        mapping = {_PLAN_OUTPUT_ROLE: verified.upload_id}
        bindings = (
            {
                "uploadId": str(verified.upload_id),
                "role": _PLAN_OUTPUT_ROLE,
                "position": 0,
            },
        )
        self._invoke(
            "complete",
            self._lease_values(
                lease,
                output_digest=plan_digest,
                output_contract=lease.expected_output_contract,
                expected_output_contract=lease.expected_output_contract,
                output_artifacts=mapping,
                artifacts=mapping,
                output_bindings=bindings,
            ),
        )

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: str,
        code: str,
        message: str,
        required_action: str,
    ) -> None:
        self._invoke(
            "fail",
            self._lease_values(
                lease,
                failure_kind=failure_kind,
                code=code,
                failure_code=code,
                message=message,
                context={},
                required_action=required_action,
            ),
        )

    def _invoke(self, method_name: str, values: Mapping[str, object]) -> object:
        method = cast(Callable[..., object], getattr(self._client, method_name, None))
        if not callable(method):
            raise RuntimeError(f"Worker Gateway SDK does not expose {method_name}")
        signature = inspect.signature(method)
        arguments: dict[str, object] = {}
        missing: list[str] = []
        for name, parameter in signature.parameters.items():
            if name in values:
                arguments[name] = values[name]
            elif parameter.default is inspect.Parameter.empty:
                missing.append(name)
        if missing:
            joined = ", ".join(sorted(missing))
            raise RuntimeError(
                f"Worker Gateway SDK method {method_name} has unsupported required fields: {joined}"
            )
        return method(**arguments)

    @staticmethod
    def _lease_values(lease: WorkerLease, **extra: object) -> dict[str, object]:
        values: dict[str, object] = {
            "lease": lease,
            "work_lease": lease,
            "lease_id": lease.lease_id,
            "work_id": lease.work_id,
            "lease_token": lease.lease_token,
            "worker_id": lease.worker_id,
            "input_digest": lease.input_digest,
            "correlation_id": lease.correlation_id,
        }
        values.update(extra)
        return values
