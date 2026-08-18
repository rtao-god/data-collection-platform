from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest

from manual_import_worker import ManualWorkerSettings, SourceWorkerGatewayAdapter
from source_connector_sdk import VerifiedUpload, WorkerLease

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
_UPLOAD_ID = UUID("00000000-0000-0000-0000-000000000010")


class _Client:
    def __init__(self) -> None:
        self.registration: dict[str, object] | None = None
        self.acquire_call: dict[str, object] | None = None
        self.completion: dict[str, object] | None = None
        self.failure: dict[str, object] | None = None

    def register(self, **kwargs: object) -> None:
        self.registration = dict(kwargs)

    def acquire_lease(self, **kwargs: object) -> None:
        self.acquire_call = dict(kwargs)
        return None

    def heartbeat(self, lease: WorkerLease, **kwargs: object) -> WorkerLease:
        del kwargs
        return lease

    def upload_bytes(
        self,
        lease: WorkerLease,
        *,
        content: bytes,
        artifact_kind: str,
        content_type: str,
    ) -> VerifiedUpload:
        return VerifiedUpload(
            upload_id=_UPLOAD_ID,
            work_id=lease.work_id,
            artifact_kind=artifact_kind,  # type: ignore[arg-type]
            content_digest=_digest(content),
            size_bytes=len(content),
            content_type=content_type,
            storage_reference=f"derived/{_UPLOAD_ID}",
            verified_at_utc=_NOW,
        )

    def complete(self, lease: WorkerLease, **kwargs: object) -> None:
        del lease
        self.completion = dict(kwargs)

    def fail(self, lease: WorkerLease, **kwargs: object) -> None:
        del lease
        self.failure = dict(kwargs)


def _settings(capability: str) -> ManualWorkerSettings:
    return ManualWorkerSettings(
        gateway_url="https://gateway.example.test",
        gateway_token="secret",
        capability=capability,  # type: ignore[arg-type]
        build_identity=f"{capability}-build",
        resource_profile=capability,
        poll_interval_seconds=1,
        lease_duration_seconds=60,
        heartbeat_interval_seconds=10,
        transfer_timeout_seconds=5,
        max_source_bytes=1024,
        max_plan_bytes=2048,
    )


def _lease(capability: str) -> WorkerLease:
    return WorkerLease(
        lease_id=UUID("00000000-0000-0000-0000-000000000001"),
        work_id=UUID("00000000-0000-0000-0000-000000000002"),
        lease_token=UUID("00000000-0000-0000-0000-000000000003"),
        worker_id=f"{capability}-worker-local",
        stage="discovery",
        capability=capability,  # type: ignore[arg-type]
        input_digest="sha256:" + "1" * 64,
        expected_output_contract=(
            "manual-import-plan@1" if capability == "manual_import" else "manual-import-record@1"
        ),
        issued_at_utc=_NOW,
        expires_at_utc=_NOW + timedelta(seconds=60),
        heartbeat_deadline_utc=_NOW + timedelta(seconds=20),
        source_permit=None,
        input_artifacts=(),
        correlation_id="gateway-adapter-test",
    )


@pytest.mark.parametrize(
    ("capability", "contract", "role", "artifact_kind", "content_type"),
    (
        (
            "manual_import",
            "manual-import-plan@1",
            "manual_import_plan",
            "diagnostic_artifact",
            "application/vnd.collection.manual-import-plan+json",
        ),
        (
            "manual_record",
            "manual-import-record@1",
            "manual_import_record",
            "derived_artifact",
            "application/vnd.collection.manual-import-record+json",
        ),
    ),
)
def test_adapter_registers_and_completes_only_configured_capability(
    capability: str,
    contract: str,
    role: str,
    artifact_kind: str,
    content_type: str,
) -> None:
    client = _Client()
    adapter = SourceWorkerGatewayAdapter(client)  # type: ignore[arg-type]
    settings = _settings(capability)
    lease = _lease(capability)
    payload = b"{}\n"

    adapter.register(settings)
    assert client.registration == {
        "build_identity": settings.build_identity,
        "capabilities": {capability},
        "supported_output_contracts": {contract},
        "max_concurrency": 1,
        "resource_profile": capability,
    }

    adapter.acquire(settings)
    assert client.acquire_call is not None
    assert client.acquire_call["capability"] == capability

    upload = adapter.publish_output(lease, payload, content_digest=_digest(payload))
    assert upload.artifact_kind == artifact_kind
    assert upload.content_type == content_type

    adapter.complete(lease, output_digest="sha256:" + "2" * 64, upload=upload)
    assert client.completion is not None
    assert client.completion["output_contract"] == contract
    assert client.completion["output_artifacts"] == ((_UPLOAD_ID, role),)


def test_adapter_rejects_lease_from_another_manual_capability() -> None:
    client = _Client()
    adapter = SourceWorkerGatewayAdapter(client)  # type: ignore[arg-type]
    adapter.register(_settings("manual_import"))

    with pytest.raises(ValueError, match="capability"):
        adapter.publish_output(
            _lease("manual_record"),
            b"{}\n",
            content_digest=_digest(b"{}\n"),
        )


def _digest(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"
