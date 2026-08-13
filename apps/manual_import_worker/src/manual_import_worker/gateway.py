from __future__ import annotations

from typing import cast

import httpx

from manual_import_worker.contracts import ManualImportWorkerSettings
from source_connector_sdk import (
    LeaseArtifact,
    SourceWorkerGateway,
    VerifiedUpload,
    WorkerLease,
    WorkFailureKind,
)

_PLAN_CONTENT_TYPE = "application/vnd.collection.manual-import-plan+json"
_PLAN_OUTPUT_ROLE = "manual_import_plan"
_PLAN_OUTPUT_CONTRACTS = frozenset({"manual-import-plan", "manual-import-plan@1"})
_FAILURE_KINDS = frozenset({"transient", "permanent", "policy_blocked", "contract_invalid"})


class SourceWorkerGatewayAdapter:
    """Maps manual-import behavior to the canonical source-worker SDK."""

    def __init__(self, client: SourceWorkerGateway) -> None:
        self._client = client
        self._build_identity: str | None = None

    def register(self, settings: ManualImportWorkerSettings) -> None:
        self._client.register(
            build_identity=settings.build_identity,
            capabilities={"manual_import"},
            supported_output_contracts=_PLAN_OUTPUT_CONTRACTS,
            max_concurrency=1,
            resource_profile=settings.resource_profile,
        )
        self._build_identity = settings.build_identity

    def acquire(self, settings: ManualImportWorkerSettings) -> WorkerLease | None:
        return self._client.acquire_lease(
            capability="manual_import",
            lease_duration_seconds=settings.lease_duration_seconds,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    def heartbeat(self, lease: WorkerLease, settings: ManualImportWorkerSettings) -> WorkerLease:
        return self._client.heartbeat(
            lease,
            lease_duration_seconds=settings.lease_duration_seconds,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    def read_source(
        self,
        lease: WorkerLease,
        source: LeaseArtifact,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        prepared = self._client.prepare_read(
            lease,
            artifact_id=source.artifact_id,
        )
        body = bytearray()
        with (
            httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client,
            client.stream(prepared.method, prepared.url) as response,
        ):
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
        del timeout_seconds
        upload = self._client.upload_bytes(
            lease,
            content=payload,
            artifact_kind="diagnostic_artifact",
            content_type=_PLAN_CONTENT_TYPE,
        )
        if upload.content_digest != content_digest:
            raise RuntimeError("verified manual import plan digest changed during transfer")
        return upload

    def complete(self, lease: WorkerLease, *, plan_digest: str, upload: object) -> None:
        verified = cast(VerifiedUpload, upload)
        self._client.complete(
            lease,
            output_contract=lease.expected_output_contract,
            output_digest=plan_digest,
            worker_build_identity=self._required_build_identity(),
            output_artifacts=((verified.upload_id, _PLAN_OUTPUT_ROLE),),
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
        if failure_kind not in _FAILURE_KINDS:
            raise ValueError("manual import failure kind is unsupported")
        self._client.fail(
            lease,
            failure_kind=cast(WorkFailureKind, failure_kind),
            code=code,
            owner="ManualImportWorker",
            message=message,
            required_action=required_action,
            worker_build_identity=self._required_build_identity(),
        )

    def _required_build_identity(self) -> str:
        if self._build_identity is None:
            raise RuntimeError("manual import worker must register before processing work")
        return self._build_identity
