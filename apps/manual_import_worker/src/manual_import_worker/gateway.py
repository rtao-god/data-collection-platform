from __future__ import annotations

from typing import cast

import httpx

from manual_import_worker.contracts import (
    ManualWorkerOutputContract,
    ManualWorkerSettings,
)
from source_connector_sdk import (
    LeaseArtifact,
    SourceWorkerGateway,
    VerifiedUpload,
    WorkerLease,
    WorkFailureKind,
)

_FAILURE_KINDS = frozenset({"transient", "permanent", "policy_blocked", "contract_invalid"})


class SourceWorkerGatewayAdapter:
    """Maps one configured manual capability to the canonical Worker Gateway SDK."""

    def __init__(self, client: SourceWorkerGateway) -> None:
        self._client = client
        self._build_identity: str | None = None
        self._output: ManualWorkerOutputContract | None = None

    def register(self, settings: ManualWorkerSettings) -> None:
        output = settings.output
        self._client.register(
            build_identity=settings.build_identity,
            capabilities={settings.capability},
            supported_output_contracts={output.output_contract},
            max_concurrency=1,
            resource_profile=settings.resource_profile,
        )
        self._build_identity = settings.build_identity
        self._output = output

    def acquire(self, settings: ManualWorkerSettings) -> WorkerLease | None:
        return self._client.acquire_lease(
            capability=settings.capability,
            lease_duration_seconds=settings.lease_duration_seconds,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    def heartbeat(self, lease: WorkerLease, settings: ManualWorkerSettings) -> WorkerLease:
        return self._client.heartbeat(
            lease,
            lease_duration_seconds=settings.lease_duration_seconds,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    def read_artifact(
        self,
        lease: WorkerLease,
        artifact: LeaseArtifact,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        prepared = self._client.prepare_read(
            lease,
            artifact_id=artifact.artifact_id,
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
                    raise ValueError("manual worker input exceeds the configured byte limit")
        return bytes(body)

    def publish_output(
        self,
        lease: WorkerLease,
        payload: bytes,
        *,
        content_digest: str,
    ) -> VerifiedUpload:
        output = self._required_output(lease)
        upload = self._client.upload_bytes(
            lease,
            content=payload,
            artifact_kind=output.artifact_kind,
            content_type=output.content_type,
        )
        if upload.content_digest != content_digest:
            raise RuntimeError("verified manual output digest changed during transfer")
        return upload

    def complete(self, lease: WorkerLease, *, output_digest: str, upload: object) -> None:
        output = self._required_output(lease)
        verified = cast(VerifiedUpload, upload)
        self._client.complete(
            lease,
            output_contract=output.output_contract,
            output_digest=output_digest,
            worker_build_identity=self._required_build_identity(),
            output_artifacts=((verified.upload_id, output.output_role),),
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
            raise ValueError("manual worker failure kind is unsupported")
        output = self._required_output(lease)
        owner = "ManualWorker" if output.capability == "manual_import" else "ManualRecordWorker"
        self._client.fail(
            lease,
            failure_kind=cast(WorkFailureKind, failure_kind),
            code=code,
            owner=owner,
            message=message,
            required_action=required_action,
            worker_build_identity=self._required_build_identity(),
        )

    def _required_output(self, lease: WorkerLease) -> ManualWorkerOutputContract:
        output = self._output
        if output is None:
            raise RuntimeError("manual worker must register before processing work")
        if lease.capability != output.capability:
            raise ValueError("manual worker lease capability differs from its registration")
        if lease.expected_output_contract != output.output_contract:
            raise ValueError("manual worker lease output contract differs from its registration")
        return output

    def _required_build_identity(self) -> str:
        if self._build_identity is None:
            raise RuntimeError("manual worker must register before processing work")
        return self._build_identity
