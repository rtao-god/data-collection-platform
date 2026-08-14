from __future__ import annotations

from resolution_worker.contracts import ResolutionWorkerSettings
from source_connector_sdk import (
    SourceWorkerGateway,
    WorkerLease,
    WorkFailureKind,
)


class SdkResolutionGateway:
    """Maps deterministic non-source resolution work to the Worker Gateway protocol."""

    def __init__(self, client: SourceWorkerGateway) -> None:
        self._client = client
        self._build_identity: str | None = None

    def register(self, settings: ResolutionWorkerSettings) -> None:
        self._client.register(
            build_identity=settings.build_identity,
            capabilities={"entity_resolution"},
            supported_output_contracts={settings.output_contract},
            max_concurrency=1,
            resource_profile="entity-resolution",
        )
        self._build_identity = settings.build_identity

    def acquire(self, settings: ResolutionWorkerSettings) -> WorkerLease | None:
        return self._client.acquire_lease(
            capability="entity_resolution",
            lease_duration_seconds=settings.lease_duration_seconds,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    def heartbeat(
        self,
        lease: WorkerLease,
        settings: ResolutionWorkerSettings,
    ) -> WorkerLease:
        return self._client.heartbeat(
            lease,
            lease_duration_seconds=settings.lease_duration_seconds,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    def read_input(
        self,
        lease: WorkerLease,
        *,
        role: str,
        maximum_bytes: int,
    ) -> bytes:
        artifact = lease.artifact(role)
        return self._client.read_artifact(
            lease,
            artifact_id=artifact.artifact_id,
            maximum_bytes=maximum_bytes,
        )

    def publish_and_complete(
        self,
        lease: WorkerLease,
        *,
        content: bytes,
        content_type: str,
        output_role: str,
        output_digest: str,
    ) -> None:
        upload = self._client.upload_bytes(
            lease,
            content=content,
            artifact_kind="derived_artifact",
            content_type=content_type,
        )
        self._client.complete(
            lease,
            output_contract=lease.expected_output_contract,
            output_digest=output_digest,
            worker_build_identity=self._required_build_identity(),
            output_artifacts=((upload.upload_id, output_role),),
        )

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: WorkFailureKind,
        code: str,
        message: str,
        required_action: str,
    ) -> None:
        self._client.fail(
            lease,
            failure_kind=failure_kind,
            code=code,
            owner="ResolutionWorker",
            message=message,
            required_action=required_action,
            worker_build_identity=self._required_build_identity(),
        )

    def _required_build_identity(self) -> str:
        if self._build_identity is None:
            raise RuntimeError("resolution worker must register before completing work")
        return self._build_identity
