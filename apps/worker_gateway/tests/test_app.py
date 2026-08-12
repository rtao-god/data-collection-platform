from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient

from collection_application import (
    ArtifactKind,
    ArtifactTransferConflict,
    ArtifactTransferService,
    CollectionRunSpec,
    LeaseExpirySweep,
    LeaseExpirySweepResult,
    LeaseHeartbeat,
    LeaseRequest,
    PrepareArtifactRead,
    PrepareArtifactUpload,
    PreparedArtifactRead,
    PreparedArtifactUpload,
    SourceCapacitySpec,
    SourcePermit,
    StageRunSpec,
    VerifiedArtifactUpload,
    VerifyArtifactUpload,
    WorkCapability,
    WorkCompletion,
    WorkCompletionResult,
    WorkCompletionStatus,
    WorkEngineConflict,
    WorkEngineService,
    WorkerRegistration,
    WorkerRegistrationResult,
    WorkerRegistrationStatus,
    WorkFailure,
    WorkInputArtifact,
    WorkLease,
    WorkMutationResult,
    WorkRelease,
    WorkStage,
    WorkUnitSpec,
    WorkUnitState,
)
from worker_gateway import (
    GatewayDependencies,
    WorkerAuthenticator,
    WorkerPrincipal,
    create_app,
)

_TOKEN = "integration-worker-token-000000000001"
_WORKER_ID = "worker-http-1"
_LEASE_ID = UUID("019c0000-0000-7000-8000-000000000001")
_WORK_ID = UUID("019c0000-0000-7000-8000-000000000002")
_LEASE_TOKEN = UUID("019c0000-0000-7000-8000-000000000003")
_UPLOAD_ID = UUID("019c0000-0000-7000-8000-000000000004")
_ARTIFACT_ID = UUID("019c0000-0000-7000-8000-000000000005")
_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + ("a" * 64)
_OUTPUT_DIGEST = "sha256:" + ("b" * 64)


class FakePort:
    def __init__(self) -> None:
        self.registration: WorkerRegistration | None = None
        self.lease_request: LeaseRequest | None = None
        self.heartbeat_command: LeaseHeartbeat | None = None
        self.completion: WorkCompletion | None = None
        self.failure: WorkFailure | None = None
        self.release_command: WorkRelease | None = None
        self.lease: WorkLease | None = None
        self.conflict: WorkEngineConflict | None = None

    def register_worker(self, command: WorkerRegistration) -> WorkerRegistrationResult:
        self.registration = command
        return WorkerRegistrationResult(
            worker_id=command.worker_id,
            status=WorkerRegistrationStatus.REGISTERED,
        )

    def configure_source(self, command: SourceCapacitySpec) -> None:
        del command

    def create_run(self, command: CollectionRunSpec) -> None:
        del command

    def create_stage(self, command: StageRunSpec) -> None:
        del command

    def enqueue_work(self, command: WorkUnitSpec) -> None:
        del command

    def acquire_lease(self, command: LeaseRequest) -> WorkLease | None:
        self.lease_request = command
        if self.conflict is not None:
            raise self.conflict
        return self.lease

    def heartbeat(self, command: LeaseHeartbeat) -> WorkLease:
        self.heartbeat_command = command
        if self.lease is None:
            raise AssertionError("heartbeat requires a configured fake lease")
        return self.lease

    def complete(self, command: WorkCompletion) -> WorkCompletionResult:
        self.completion = command
        return WorkCompletionResult(
            work_id=command.work_id,
            status=WorkCompletionStatus.APPLIED,
            output_digest=command.output_digest,
            revision=3,
        )

    def fail(self, command: WorkFailure) -> WorkMutationResult:
        self.failure = command
        return WorkMutationResult(
            work_id=command.work_id,
            state=WorkUnitState.RETRY_WAIT,
            revision=4,
            available_at_utc=_NOW + timedelta(seconds=10),
        )

    def release(self, command: WorkRelease) -> WorkMutationResult:
        self.release_command = command
        return WorkMutationResult(
            work_id=command.work_id,
            state=WorkUnitState.PENDING,
            revision=5,
            available_at_utc=_NOW,
        )

    def expire_leases(self, command: LeaseExpirySweep) -> LeaseExpirySweepResult:
        del command
        return LeaseExpirySweepResult(
            expired_count=0,
            retry_wait_count=0,
            dead_letter_count=0,
        )


class FakeArtifactPort:
    def __init__(self) -> None:
        self.prepare_upload_command: PrepareArtifactUpload | None = None
        self.verify_upload_command: VerifyArtifactUpload | None = None
        self.prepare_read_command: PrepareArtifactRead | None = None
        self.conflict: ArtifactTransferConflict | None = None

    def prepare_upload(self, command: PrepareArtifactUpload) -> PreparedArtifactUpload:
        self.prepare_upload_command = command
        if self.conflict is not None:
            raise self.conflict
        return PreparedArtifactUpload(
            upload_id=command.upload_id,
            method="PUT",
            url="https://object-store.local/upload",
            required_headers={"content-type": command.content_type},
            expires_at_utc=_NOW + timedelta(minutes=5),
        )

    def verify_upload(self, command: VerifyArtifactUpload) -> VerifiedArtifactUpload:
        self.verify_upload_command = command
        if self.conflict is not None:
            raise self.conflict
        return VerifiedArtifactUpload(
            upload_id=command.upload_id,
            work_id=command.work_id,
            artifact_kind=ArtifactKind.RAW_ARTIFACT,
            content_digest=_OUTPUT_DIGEST,
            size_bytes=7,
            content_type="text/html",
            storage_reference="raw-artifacts/sha256/bb/bb/" + ("b" * 64),
            verified_at_utc=_NOW,
        )

    def prepare_read(self, command: PrepareArtifactRead) -> PreparedArtifactRead:
        self.prepare_read_command = command
        if self.conflict is not None:
            raise self.conflict
        return PreparedArtifactRead(
            artifact_id=command.artifact_id,
            method="GET",
            url="https://object-store.local/read",
            expires_at_utc=_NOW + timedelta(minutes=5),
        )


def _lease(*, source_permit: bool = False, input_artifact: bool = False) -> WorkLease:
    return WorkLease(
        lease_id=_LEASE_ID,
        work_id=_WORK_ID,
        lease_token=_LEASE_TOKEN,
        worker_id=_WORKER_ID,
        stage=WorkStage.ACQUISITION,
        capability=WorkCapability.HTTP_FETCH,
        input_digest=_DIGEST,
        expected_output_contract="fetch-observation",
        issued_at_utc=_NOW,
        expires_at_utc=_NOW + timedelta(minutes=5),
        heartbeat_deadline_utc=_NOW + timedelta(minutes=1),
        source_permit=(
            SourcePermit(
                source_key="official_website",
                policy_digest=_DIGEST,
                permit_not_before_utc=_NOW,
            )
            if source_permit
            else None
        ),
        correlation_id="lease-correlation",
        input_artifacts=(
            (WorkInputArtifact(artifact_id=_ARTIFACT_ID, role="raw_document"),)
            if input_artifact
            else ()
        ),
    )


def _client(
    port: FakePort,
    *,
    artifact_port: FakeArtifactPort | None = None,
    readiness_probe: Callable[[], None] | None = None,
) -> TestClient:
    probe = readiness_probe or (lambda: None)
    authenticator = WorkerAuthenticator.from_plaintext_credentials(
        {
            _TOKEN: WorkerPrincipal(
                worker_id=_WORKER_ID,
                capabilities=frozenset({WorkCapability.HTTP_FETCH}),
            )
        }
    )
    return TestClient(
        create_app(
            GatewayDependencies(
                work_engine=WorkEngineService(port),
                artifact_transfer=ArtifactTransferService(artifact_port or FakeArtifactPort()),
                authenticator=authenticator,
                readiness_probe=probe,
                expiry_interval_seconds=0,
            )
        ),
        raise_server_exceptions=False,
    )


def _headers(correlation_id: str = "request-correlation") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "X-Correlation-Id": correlation_id,
    }


def test_registration_uses_authenticated_identity_and_correlation() -> None:
    port = FakePort()
    with _client(port) as client:
        response = client.post(
            "/worker/registrations",
            headers=_headers(),
            json={
                "buildIdentity": "build-http-1",
                "capabilities": ["http_fetch"],
                "supportedOutputContracts": ["fetch-observation"],
                "maxConcurrency": 2,
                "resourceProfile": "http-small",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"workerId": _WORKER_ID, "status": "registered"}
    assert response.headers["X-Correlation-Id"] == "request-correlation"
    assert port.registration is not None
    assert port.registration.worker_id == _WORKER_ID
    assert port.registration.correlation_id == "request-correlation"


def test_missing_worker_credential_returns_typed_authentication_error() -> None:
    with _client(FakePort()) as client:
        response = client.get("/worker/capabilities")

    assert response.status_code == 401
    assert response.json()["owner"] == "WorkerGateway.Authentication"
    assert response.json()["code"] == "WORKER_AUTHENTICATION_REQUIRED"
    assert response.headers["X-Correlation-Id"]


def test_registration_cannot_expand_credential_capability_scope() -> None:
    port = FakePort()
    with _client(port) as client:
        response = client.post(
            "/worker/registrations",
            headers=_headers(),
            json={
                "buildIdentity": "build-http-1",
                "capabilities": ["http_fetch", "browser_fetch"],
                "supportedOutputContracts": ["fetch-observation"],
                "maxConcurrency": 1,
                "resourceProfile": "http-small",
            },
        )

    assert response.status_code == 403
    assert response.json()["code"] == "WORKER_REGISTRATION_SCOPE_FORBIDDEN"
    assert port.registration is None


def test_lease_acquisition_distinguishes_no_work_from_an_acquired_lease() -> None:
    port = FakePort()
    with _client(port) as client:
        no_work = client.post(
            "/worker/leases/acquire",
            headers=_headers(),
            json={
                "capability": "http_fetch",
                "leaseDurationSeconds": 300,
                "heartbeatIntervalSeconds": 60,
            },
        )
        port.lease = _lease(source_permit=True, input_artifact=True)
        acquired = client.post(
            "/worker/leases/acquire",
            headers=_headers("acquired-correlation"),
            json={
                "capability": "http_fetch",
                "leaseDurationSeconds": 300,
                "heartbeatIntervalSeconds": 60,
            },
        )

    assert no_work.status_code == 200
    assert no_work.json() == {"state": "no_eligible_work", "capability": "http_fetch"}
    assert acquired.status_code == 200
    body = acquired.json()
    assert body["state"] == "acquired"
    assert body["lease"]["workId"] == str(_WORK_ID)
    assert body["lease"]["sourcePermit"]["sourceKey"] == "official_website"
    assert body["lease"]["inputArtifacts"] == [
        {"artifactId": str(_ARTIFACT_ID), "role": "raw_document"}
    ]
    assert port.lease_request is not None
    assert port.lease_request.worker_id == _WORKER_ID
    assert port.lease_request.correlation_id == "acquired-correlation"


def test_heartbeat_and_completion_map_exact_worker_commands() -> None:
    port = FakePort()
    port.lease = _lease(source_permit=True)
    with _client(port) as client:
        heartbeat = client.post(
            f"/worker/leases/{_LEASE_ID}/heartbeat",
            headers=_headers(),
            json={
                "workId": str(_WORK_ID),
                "leaseToken": str(_LEASE_TOKEN),
                "inputDigest": _DIGEST,
                "leaseDurationSeconds": 300,
                "heartbeatIntervalSeconds": 60,
            },
        )
        completion = client.post(
            f"/worker/work/{_WORK_ID}/complete",
            headers=_headers("completion-correlation"),
            json={
                "leaseId": str(_LEASE_ID),
                "leaseToken": str(_LEASE_TOKEN),
                "inputDigest": _DIGEST,
                "outputContract": "fetch-observation",
                "outputDigest": _OUTPUT_DIGEST,
                "workerBuildIdentity": "build-http-1",
                "outputArtifacts": [{"uploadId": str(_UPLOAD_ID), "role": "raw_document"}],
            },
        )

    assert heartbeat.status_code == 200
    assert heartbeat.json()["leaseId"] == str(_LEASE_ID)
    assert port.heartbeat_command is not None
    assert port.heartbeat_command.worker_id == _WORKER_ID
    assert completion.status_code == 200
    assert completion.json()["status"] == "applied"
    assert port.completion is not None
    assert port.completion.worker_id == _WORKER_ID
    assert port.completion.correlation_id == "completion-correlation"
    assert port.completion.output_artifacts[0].upload_id == _UPLOAD_ID
    assert port.completion.output_artifacts[0].role == "raw_document"


def test_owner_conflict_preserves_owner_context_and_http_conflict() -> None:
    port = FakePort()
    port.conflict = WorkEngineConflict(
        code="WORK_LEASE_STALE",
        message="The worker no longer owns this lease.",
        context={"workId": str(_WORK_ID), "reason": "lease_token_mismatch"},
        required_action="Discard the result and acquire a new lease.",
    )
    with _client(port) as client:
        response = client.post(
            "/worker/leases/acquire",
            headers=_headers("stale-correlation"),
            json={
                "capability": "http_fetch",
                "leaseDurationSeconds": 300,
                "heartbeatIntervalSeconds": 60,
            },
        )

    assert response.status_code == 409
    assert response.json() == {
        "type": "collection/work-lease-stale",
        "owner": "WorkEngine",
        "code": "WORK_LEASE_STALE",
        "message": "The worker no longer owns this lease.",
        "context": {"workId": str(_WORK_ID), "reason": "lease_token_mismatch"},
        "requiredAction": "Discard the result and acquire a new lease.",
        "correlationId": "stale-correlation",
    }


def test_invalid_uuid_and_extra_field_fail_as_transport_errors() -> None:
    port = FakePort()
    with _client(port) as client:
        response = client.post(
            f"/worker/work/{_WORK_ID}/complete",
            headers=_headers(),
            json={
                "leaseId": "not-a-uuid",
                "leaseToken": str(_LEASE_TOKEN),
                "inputDigest": _DIGEST,
                "outputContract": "fetch-observation",
                "outputDigest": _OUTPUT_DIGEST,
                "workerBuildIdentity": "build-http-1",
                "unexpected": True,
            },
        )

    assert response.status_code == 422
    assert response.json()["owner"] == "WorkerGateway.Transport"
    assert response.json()["code"] == "WORKER_REQUEST_INVALID"
    assert port.completion is None


def test_protocol_metadata_is_authenticated_and_read_only() -> None:
    port = FakePort()
    with _client(port) as client:
        response = client.get("/worker/capabilities", headers=_headers())

    assert response.status_code == 200
    assert response.json()["contract"] == "collector-worker-protocol"
    assert response.json()["authorizedCapabilities"] == ["http_fetch"]
    assert "acquisition" in response.json()["supportedStages"]
    assert port.registration is None
    assert port.lease_request is None


def test_readiness_failure_is_typed_and_does_not_repair() -> None:
    calls = 0

    def failing_probe() -> None:
        nonlocal calls
        calls += 1
        raise ConnectionError("database unavailable")

    with _client(FakePort(), readiness_probe=failing_probe) as client:
        response = client.get("/health/ready")
        liveness = client.get("/health/live")

    assert response.status_code == 503
    assert response.json()["code"] == "WORKER_GATEWAY_DEPENDENCY_UNAVAILABLE"
    assert response.json()["context"] == {"causeType": "ConnectionError"}
    assert calls == 1
    assert liveness.status_code == 200
    assert liveness.json() == {"component": "worker-gateway", "status": "live"}


def test_artifact_routes_bind_authenticated_worker_and_exact_lease() -> None:
    work_port = FakePort()
    artifact_port = FakeArtifactPort()
    with _client(work_port, artifact_port=artifact_port) as client:
        prepared = client.post(
            "/worker/artifacts/prepare-upload",
            headers=_headers("artifact-prepare"),
            json={
                "uploadId": str(_UPLOAD_ID),
                "workId": str(_WORK_ID),
                "leaseId": str(_LEASE_ID),
                "leaseToken": str(_LEASE_TOKEN),
                "inputDigest": _DIGEST,
                "artifactKind": "raw_artifact",
                "expectedDigest": _OUTPUT_DIGEST,
                "expectedSizeBytes": 7,
                "contentType": "text/html",
                "expiresInSeconds": 300,
            },
        )
        verified = client.post(
            "/worker/artifacts/verify-upload",
            headers=_headers("artifact-verify"),
            json={
                "uploadId": str(_UPLOAD_ID),
                "workId": str(_WORK_ID),
                "leaseId": str(_LEASE_ID),
                "leaseToken": str(_LEASE_TOKEN),
                "inputDigest": _DIGEST,
            },
        )
        read = client.post(
            "/worker/artifacts/prepare-read",
            headers=_headers("artifact-read"),
            json={
                "artifactId": str(_ARTIFACT_ID),
                "workId": str(_WORK_ID),
                "leaseId": str(_LEASE_ID),
                "leaseToken": str(_LEASE_TOKEN),
                "inputDigest": _DIGEST,
                "expiresInSeconds": 300,
            },
        )

    assert prepared.status_code == 200
    assert prepared.json()["method"] == "PUT"
    assert prepared.json()["uploadId"] == str(_UPLOAD_ID)
    assert verified.status_code == 200
    assert verified.json()["contentDigest"] == _OUTPUT_DIGEST
    assert read.status_code == 200
    assert read.json()["artifactId"] == str(_ARTIFACT_ID)
    assert artifact_port.prepare_upload_command is not None
    assert artifact_port.prepare_upload_command.worker_id == _WORKER_ID
    assert artifact_port.prepare_upload_command.correlation_id == "artifact-prepare"
    assert artifact_port.verify_upload_command is not None
    assert artifact_port.verify_upload_command.worker_id == _WORKER_ID
    assert artifact_port.prepare_read_command is not None
    assert artifact_port.prepare_read_command.worker_id == _WORKER_ID


def test_completion_rejects_duplicate_output_artifact_roles() -> None:
    port = FakePort()
    with _client(port) as client:
        response = client.post(
            f"/worker/work/{_WORK_ID}/complete",
            headers=_headers(),
            json={
                "leaseId": str(_LEASE_ID),
                "leaseToken": str(_LEASE_TOKEN),
                "inputDigest": _DIGEST,
                "outputContract": "fetch-observation",
                "outputDigest": _OUTPUT_DIGEST,
                "workerBuildIdentity": "build-http-1",
                "outputArtifacts": [
                    {"uploadId": str(_UPLOAD_ID), "role": "body"},
                    {
                        "uploadId": "019c0000-0000-7000-8000-000000000006",
                        "role": "body",
                    },
                ],
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "WORKER_COMMAND_INVALID"
    assert port.completion is None
