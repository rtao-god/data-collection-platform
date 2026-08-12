from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from uuid import UUID

import httpx
import pytest

from source_connector_sdk import SourceWorkerGateway, WorkerGatewayFailure

_WORK_ID = UUID("00000000-0000-0000-0000-000000000101")
_LEASE_ID = UUID("00000000-0000-0000-0000-000000000102")
_LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000103")
_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000104")
_UPLOAD_ID = UUID("00000000-0000-0000-0000-000000000105")
_DIGEST = "sha256:" + "1" * 64
_POLICY_DIGEST = "sha256:" + "2" * 64
_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _lease_payload() -> dict[str, object]:
    return {
        "leaseId": str(_LEASE_ID),
        "workId": str(_WORK_ID),
        "leaseToken": str(_LEASE_TOKEN),
        "workerId": "manual-worker-1",
        "stage": "discovery",
        "capability": "manual_import",
        "inputDigest": _DIGEST,
        "expectedOutputContract": "collector-manual-import-plan",
        "issuedAtUtc": _NOW.isoformat(),
        "expiresAtUtc": (_NOW + timedelta(minutes=5)).isoformat(),
        "heartbeatDeadlineUtc": (_NOW + timedelta(minutes=1)).isoformat(),
        "sourcePermit": {
            "sourceKey": "manual_seed",
            "policyDigest": _POLICY_DIGEST,
            "permitNotBeforeUtc": _NOW.isoformat(),
        },
        "inputArtifacts": [
            {
                "artifactId": str(_ARTIFACT_ID),
                "role": "manual_source",
            }
        ],
        "correlationId": "lease-correlation",
    }


def _gateway(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    object_handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> SourceWorkerGateway:
    return SourceWorkerGateway(
        base_url="http://gateway.test",
        token="worker-secret",
        gateway_transport=httpx.MockTransport(handler),
        object_transport=httpx.MockTransport(
            object_handler or (lambda request: httpx.Response(500, request=request))
        ),
    )


def test_registration_and_empty_lease_are_strictly_typed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/worker/registrations":
            return httpx.Response(
                200,
                json={"workerId": "manual-worker-1", "status": "registered"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"state": "no_eligible_work", "capability": "manual_import"},
            request=request,
        )

    with _gateway(handler) as gateway:
        registration = gateway.register(
            build_identity="manual-worker-build",
            capabilities={"manual_import"},
            supported_output_contracts={"collector-manual-import-plan"},
            max_concurrency=1,
            resource_profile="manual-local",
        )
        lease = gateway.acquire_lease(capability="manual_import")

    assert registration.worker_id == "manual-worker-1"
    assert registration.status == "registered"
    assert lease is None
    assert all(request.headers["authorization"] == "Bearer worker-secret" for request in requests)


def test_lease_and_heartbeat_preserve_immutable_identity() -> None:
    heartbeat_payload = _lease_payload()
    heartbeat_payload["expiresAtUtc"] = (_NOW + timedelta(minutes=10)).isoformat()
    heartbeat_payload["heartbeatDeadlineUtc"] = (_NOW + timedelta(minutes=2)).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/worker/leases/acquire":
            return httpx.Response(
                200,
                json={"state": "acquired", "lease": _lease_payload()},
                request=request,
            )
        return httpx.Response(200, json=heartbeat_payload, request=request)

    with _gateway(handler) as gateway:
        lease = gateway.acquire_lease(capability="manual_import")
        assert lease is not None
        renewed = gateway.heartbeat(lease)

    assert lease.artifact("manual_source").artifact_id == _ARTIFACT_ID
    assert lease.source_permit is not None
    assert lease.source_permit.policy_digest == _POLICY_DIGEST
    assert renewed.work_id == lease.work_id
    assert renewed.expires_at_utc > lease.expires_at_utc


def test_gateway_error_preserves_owner_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "type": "collection/work-lease-stale",
                "owner": "WorkEngine",
                "code": "WORK_LEASE_STALE",
                "message": "The lease is stale.",
                "context": {"workId": str(_WORK_ID)},
                "requiredAction": "Acquire another lease.",
                "correlationId": "gateway-correlation",
            },
            request=request,
        )

    with _gateway(handler) as gateway, pytest.raises(WorkerGatewayFailure) as captured:
        gateway.acquire_lease(capability="manual_import")

    assert captured.value.status_code == 409
    assert captured.value.envelope.owner == "WorkEngine"
    assert captured.value.envelope.code == "WORK_LEASE_STALE"
    assert "worker-secret" not in repr(captured.value.__dict__)


def test_upload_bytes_never_sends_gateway_token_to_object_store() -> None:
    body = b"manual import source"
    digest = f"sha256:{sha256(body).hexdigest()}"
    gateway_requests: list[httpx.Request] = []
    object_requests: list[httpx.Request] = []

    def gateway_handler(request: httpx.Request) -> httpx.Response:
        gateway_requests.append(request)
        if request.url.path == "/worker/leases/acquire":
            return httpx.Response(
                200,
                json={"state": "acquired", "lease": _lease_payload()},
                request=request,
            )
        if request.url.path == "/worker/artifacts/prepare-upload":
            return httpx.Response(
                200,
                json={
                    "uploadId": str(_UPLOAD_ID),
                    "method": "PUT",
                    "url": "http://objects.test/upload",
                    "requiredHeaders": {
                        "content-length": str(len(body)),
                        "content-type": "application/json",
                        "x-amz-meta-sha256": digest.removeprefix("sha256:"),
                    },
                    "expiresAtUtc": (_NOW + timedelta(minutes=15)).isoformat(),
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "uploadId": str(_UPLOAD_ID),
                "workId": str(_WORK_ID),
                "artifactKind": "raw_artifact",
                "contentDigest": digest,
                "sizeBytes": len(body),
                "contentType": "application/json",
                "storageReference": "raw-artifacts/sha256/aa/bb/value",
                "verifiedAtUtc": _NOW.isoformat(),
            },
            request=request,
        )

    def object_handler(request: httpx.Request) -> httpx.Response:
        object_requests.append(request)
        return httpx.Response(200, request=request)

    with _gateway(gateway_handler, object_handler=object_handler) as gateway:
        lease = gateway.acquire_lease(capability="manual_import")
        assert lease is not None
        verified = gateway.upload_bytes(
            lease,
            content=body,
            artifact_kind="raw_artifact",
            content_type="application/json",
            upload_id=_UPLOAD_ID,
        )

    assert verified.content_digest == digest
    assert all(
        request.headers["authorization"] == "Bearer worker-secret" for request in gateway_requests
    )
    assert len(object_requests) == 1
    assert "authorization" not in object_requests[0].headers
    assert object_requests[0].content == body


def test_complete_sends_ordered_output_bindings() -> None:
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/worker/leases/acquire":
            return httpx.Response(
                200,
                json={"state": "acquired", "lease": _lease_payload()},
                request=request,
            )
        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "workId": str(_WORK_ID),
                "status": "applied",
                "outputDigest": _DIGEST,
                "revision": 3,
            },
            request=request,
        )

    with _gateway(handler) as gateway:
        lease = gateway.acquire_lease(capability="manual_import")
        assert lease is not None
        result = gateway.complete(
            lease,
            output_contract="collector-manual-import-plan",
            output_digest=_DIGEST,
            worker_build_identity="manual-worker-build",
            output_artifacts=((_UPLOAD_ID, "manual_plan"),),
        )

    assert result.status == "applied"
    assert captured_payload["outputArtifacts"] == [
        {"uploadId": str(_UPLOAD_ID), "role": "manual_plan"}
    ]


def test_success_response_with_unknown_field_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "workerId": "manual-worker-1",
                "status": "registered",
                "unexpected": True,
            },
            request=request,
        )

    with _gateway(handler) as gateway, pytest.raises(ValueError, match="field set mismatch"):
        gateway.register(
            build_identity="manual-worker-build",
            capabilities={"manual_import"},
            supported_output_contracts={"collector-manual-import-plan"},
            max_concurrency=1,
            resource_profile="manual-local",
        )


def test_read_limit_fails_with_typed_local_owner_error() -> None:
    def gateway_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/worker/leases/acquire":
            return httpx.Response(
                200,
                json={"state": "acquired", "lease": _lease_payload()},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "artifactId": str(_ARTIFACT_ID),
                "method": "GET",
                "url": "http://objects.test/read",
                "expiresAtUtc": (_NOW + timedelta(minutes=15)).isoformat(),
            },
            request=request,
        )

    def object_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"too-large", request=request)

    with _gateway(gateway_handler, object_handler=object_handler) as gateway:
        lease = gateway.acquire_lease(capability="manual_import")
        assert lease is not None
        with pytest.raises(WorkerGatewayFailure) as captured:
            gateway.read_artifact(lease, artifact_id=_ARTIFACT_ID, maximum_bytes=3)

    assert captured.value.envelope.owner == "SourceConnectorSdk.ObjectTransfer"
    assert captured.value.envelope.code == "OBJECT_READ_TOO_LARGE"
