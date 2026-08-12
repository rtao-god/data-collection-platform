from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    path = ROOT / "apps/worker_gateway/tests/test_artifact_routes.py"
    path.write_text(
        '''from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID

from fastapi.testclient import TestClient

from collection_application import (
    ArtifactKind,
    ArtifactTransferService,
    PrepareArtifactRead,
    PrepareArtifactUpload,
    PreparedArtifactRead,
    PreparedArtifactUpload,
    VerifiedArtifactUpload,
    VerifyArtifactUpload,
    WorkEngineService,
)
from worker_gateway.app import GatewayDependencies, create_app
from worker_gateway.auth import WorkerAuthenticator, WorkerPrincipal

_UPLOAD_ID = UUID("019c0000-0000-7000-8000-000000000101")
_ARTIFACT_ID = UUID("019c0000-0000-7000-8000-000000000102")
_WORK_ID = UUID("019c0000-0000-7000-8000-000000000103")
_LEASE_ID = UUID("019c0000-0000-7000-8000-000000000104")
_LEASE_TOKEN = UUID("019c0000-0000-7000-8000-000000000105")
_DIGEST = "sha256:" + ("a" * 64)
_NOW = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)


class _Authenticator:
    def authenticate(self, authorization: str | None) -> WorkerPrincipal:
        assert authorization == "Bearer worker-secret"
        return cast(
            WorkerPrincipal,
            SimpleNamespace(
                worker_id="artifact-worker",
                capabilities=frozenset(),
            ),
        )


class _ArtifactPort:
    prepared: PrepareArtifactUpload | None = None
    verified: VerifyArtifactUpload | None = None
    read: PrepareArtifactRead | None = None

    def prepare_upload(self, command: PrepareArtifactUpload) -> PreparedArtifactUpload:
        self.prepared = command
        return PreparedArtifactUpload(
            upload_id=command.upload_id,
            method="PUT",
            url="https://objects.example.test/upload",
            required_headers={"content-type": command.content_type},
            expires_at_utc=_NOW,
        )

    def verify_upload(self, command: VerifyArtifactUpload) -> VerifiedArtifactUpload:
        self.verified = command
        return VerifiedArtifactUpload(
            upload_id=command.upload_id,
            work_id=command.work_id,
            artifact_kind=ArtifactKind.RAW_ARTIFACT,
            content_digest=_DIGEST,
            size_bytes=128,
            content_type="application/json",
            storage_reference="raw-artifacts/sha256/aa/object",
            verified_at_utc=_NOW,
        )

    def prepare_read(self, command: PrepareArtifactRead) -> PreparedArtifactRead:
        self.read = command
        return PreparedArtifactRead(
            artifact_id=command.artifact_id,
            method="GET",
            url="https://objects.example.test/read",
            expires_at_utc=_NOW,
        )


def _client(port: _ArtifactPort) -> TestClient:
    dependencies = GatewayDependencies(
        work_engine=cast(WorkEngineService, object()),
        authenticator=cast(WorkerAuthenticator, _Authenticator()),
        readiness_probe=lambda: None,
        artifact_transfer=ArtifactTransferService(port),
        expiry_interval_seconds=0,
    )
    return TestClient(create_app(dependencies), raise_server_exceptions=True)


def _lease_payload() -> dict[str, object]:
    return {
        "workId": str(_WORK_ID),
        "leaseId": str(_LEASE_ID),
        "leaseToken": str(_LEASE_TOKEN),
        "inputDigest": _DIGEST,
    }


def test_authenticated_artifact_routes_bind_principal_identity() -> None:
    port = _ArtifactPort()
    with _client(port) as client:
        prepare = client.post(
            f"/worker/artifacts/uploads/{_UPLOAD_ID}/prepare",
            headers={"Authorization": "Bearer worker-secret"},
            json={
                **_lease_payload(),
                "artifactKind": "raw_artifact",
                "expectedDigest": _DIGEST,
                "expectedSizeBytes": 128,
                "contentType": "application/json",
                "expiresInSeconds": 300,
            },
        )
        verify = client.post(
            f"/worker/artifacts/uploads/{_UPLOAD_ID}/verify",
            headers={"Authorization": "Bearer worker-secret"},
            json=_lease_payload(),
        )
        read = client.post(
            f"/worker/artifacts/{_ARTIFACT_ID}/reads/prepare",
            headers={"Authorization": "Bearer worker-secret"},
            json={**_lease_payload(), "expiresInSeconds": 300},
        )

    assert prepare.status_code == 200
    assert prepare.json()["uploadId"] == str(_UPLOAD_ID)
    assert verify.status_code == 200
    assert verify.json()["contentDigest"] == _DIGEST
    assert read.status_code == 200
    assert read.json()["artifactId"] == str(_ARTIFACT_ID)
    assert port.prepared is not None and port.prepared.worker_id == "artifact-worker"
    assert port.verified is not None and port.verified.worker_id == "artifact-worker"
    assert port.read is not None and port.read.worker_id == "artifact-worker"


def test_artifact_routes_require_worker_authentication() -> None:
    port = _ArtifactPort()
    with _client(port) as client:
        response = client.post(
            f"/worker/artifacts/uploads/{_UPLOAD_ID}/verify",
            json=_lease_payload(),
        )

    assert response.status_code == 401
    assert response.json()["owner"] == "WorkerGateway.Authentication"
''',
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
