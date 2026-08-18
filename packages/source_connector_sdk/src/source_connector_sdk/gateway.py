from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from types import TracebackType
from typing import Literal, Self, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from collection_contracts import ErrorEnvelope

type WorkCapability = Literal[
    "manual_import",
    "manual_record",
    "osm_query",
    "http_fetch",
    "browser_fetch",
    "extraction",
    "normalization",
    "geography",
    "entity_resolution",
    "quality",
    "export",
]
type WorkStage = Literal[
    "discovery",
    "acquisition",
    "extraction",
    "normalization",
    "geography",
    "entity_resolution",
    "quality",
    "export",
]
type ArtifactKind = Literal["raw_artifact", "diagnostic_artifact", "derived_artifact"]
type WorkFailureKind = Literal[
    "transient",
    "permanent",
    "policy_blocked",
    "contract_invalid",
]

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_ROLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$")
_CONTENT_TYPE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$"
)
_WORK_CAPABILITIES = frozenset(
    {
        "manual_import",
        "manual_record",
        "osm_query",
        "http_fetch",
        "browser_fetch",
        "extraction",
        "normalization",
        "geography",
        "entity_resolution",
        "quality",
        "export",
    }
)
_WORK_STAGES = frozenset(
    {
        "discovery",
        "acquisition",
        "extraction",
        "normalization",
        "geography",
        "entity_resolution",
        "quality",
        "export",
    }
)
_FAILURE_KINDS = frozenset({"transient", "permanent", "policy_blocked", "contract_invalid"})
_WORK_STATES = frozenset(
    {
        "pending",
        "leased",
        "retry_wait",
        "succeeded",
        "dead_letter",
        "blocked_by_policy",
        "cancelled",
        "superseded",
    }
)
_REGISTRATION_STATES = frozenset({"registered", "already_registered"})
_COMPLETION_STATES = frozenset({"applied", "already_applied"})
_MAX_OBJECT_BYTES = 5 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LeaseArtifact:
    artifact_id: UUID
    role: str

    def __post_init__(self) -> None:
        _require_role(self.role)


@dataclass(frozen=True, slots=True)
class SourcePermit:
    source_key: str
    policy_digest: str
    permit_not_before_utc: datetime

    def __post_init__(self) -> None:
        _require_token("source_key", self.source_key)
        _require_digest("policy_digest", self.policy_digest)
        _require_utc("permit_not_before_utc", self.permit_not_before_utc)


@dataclass(frozen=True, slots=True)
class WorkerLease:
    lease_id: UUID
    work_id: UUID
    lease_token: UUID
    worker_id: str
    stage: WorkStage
    capability: WorkCapability
    input_digest: str
    expected_output_contract: str
    issued_at_utc: datetime
    expires_at_utc: datetime
    heartbeat_deadline_utc: datetime
    source_permit: SourcePermit | None
    input_artifacts: tuple[LeaseArtifact, ...]
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("worker_id", self.worker_id)
        _require_digest("input_digest", self.input_digest)
        _require_token("expected_output_contract", self.expected_output_contract)
        _require_token("correlation_id", self.correlation_id)
        _require_utc("issued_at_utc", self.issued_at_utc)
        _require_utc("expires_at_utc", self.expires_at_utc)
        _require_utc("heartbeat_deadline_utc", self.heartbeat_deadline_utc)
        if not self.issued_at_utc < self.heartbeat_deadline_utc <= self.expires_at_utc:
            raise ValueError("worker lease deadlines are inconsistent")
        artifact_ids = tuple(item.artifact_id for item in self.input_artifacts)
        roles = tuple(item.role for item in self.input_artifacts)
        if len(set(artifact_ids)) != len(artifact_ids) or len(set(roles)) != len(roles):
            raise ValueError("worker lease artifact identities and roles must be unique")

    def artifact(self, role: str) -> LeaseArtifact:
        matches = tuple(item for item in self.input_artifacts if item.role == role)
        if len(matches) != 1:
            raise ValueError(f"worker lease requires exactly one artifact with role {role!r}")
        return matches[0]


@dataclass(frozen=True, slots=True)
class PreparedUpload:
    upload_id: UUID
    method: Literal["PUT"]
    url: str
    required_headers: Mapping[str, str]
    expires_at_utc: datetime

    def __post_init__(self) -> None:
        _require_http_url("prepared upload URL", self.url)
        _require_utc("expires_at_utc", self.expires_at_utc)
        object.__setattr__(self, "required_headers", dict(self.required_headers))


@dataclass(frozen=True, slots=True)
class VerifiedUpload:
    upload_id: UUID
    work_id: UUID
    artifact_kind: ArtifactKind
    content_digest: str
    size_bytes: int
    content_type: str
    storage_reference: str
    verified_at_utc: datetime

    def __post_init__(self) -> None:
        _require_digest("content_digest", self.content_digest)
        _require_size(self.size_bytes)
        _require_content_type(self.content_type)
        if not self.storage_reference or len(self.storage_reference) > 512:
            raise ValueError("storage_reference is invalid")
        _require_utc("verified_at_utc", self.verified_at_utc)


@dataclass(frozen=True, slots=True)
class PreparedRead:
    artifact_id: UUID
    method: Literal["GET"]
    url: str
    expires_at_utc: datetime

    def __post_init__(self) -> None:
        _require_http_url("prepared read URL", self.url)
        _require_utc("expires_at_utc", self.expires_at_utc)


@dataclass(frozen=True, slots=True)
class WorkerRegistrationResult:
    worker_id: str
    status: Literal["registered", "already_registered"]


@dataclass(frozen=True, slots=True)
class WorkCompletionResult:
    work_id: UUID
    status: Literal["applied", "already_applied"]
    output_digest: str
    revision: int

    def __post_init__(self) -> None:
        _require_digest("output_digest", self.output_digest)
        if self.revision < 0:
            raise ValueError("work completion revision cannot be negative")


@dataclass(frozen=True, slots=True)
class WorkMutationResult:
    work_id: UUID
    state: str
    revision: int
    available_at_utc: datetime | None

    def __post_init__(self) -> None:
        if self.state not in _WORK_STATES:
            raise ValueError("work mutation state is unsupported")
        if self.revision < 0:
            raise ValueError("work mutation revision cannot be negative")
        if self.available_at_utc is not None:
            _require_utc("available_at_utc", self.available_at_utc)


class WorkerGatewayFailure(RuntimeError):
    """Typed gateway or object-transfer failure without retaining worker credentials."""

    def __init__(self, *, status_code: int | None, envelope: ErrorEnvelope) -> None:
        self.status_code = status_code
        self.envelope = envelope
        super().__init__(envelope.message)


class SourceWorkerGateway:
    """Strict synchronous client for the authenticated Worker Gateway contract."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 30.0,
        gateway_transport: httpx.BaseTransport | None = None,
        object_transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized_base_url = _normalize_base_url(base_url)
        if not token or len(token) > 4_096:
            raise ValueError("worker gateway token is missing or too large")
        if not 0 < timeout_seconds <= 600:
            raise ValueError("worker gateway timeout must be between 0 and 600 seconds")
        timeout = httpx.Timeout(timeout_seconds)
        self._gateway = httpx.Client(
            base_url=normalized_base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            follow_redirects=False,
            transport=gateway_transport,
        )
        self._objects = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=object_transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        self._gateway.close()
        self._objects.close()

    def register(
        self,
        *,
        build_identity: str,
        capabilities: Collection[WorkCapability],
        supported_output_contracts: Collection[str],
        max_concurrency: int,
        resource_profile: str,
    ) -> WorkerRegistrationResult:
        _require_token("build_identity", build_identity)
        _require_token("resource_profile", resource_profile)
        if not capabilities:
            raise ValueError("worker registration requires at least one capability")
        if not supported_output_contracts:
            raise ValueError("worker registration requires at least one output contract")
        for capability in capabilities:
            _require_capability(capability)
        for contract in supported_output_contracts:
            _require_token("supported_output_contract", contract)
        if not 1 <= max_concurrency <= 10_000:
            raise ValueError("max_concurrency must be between 1 and 10000")
        payload = self._request_json(
            "POST",
            "/worker/registrations",
            {
                "buildIdentity": build_identity,
                "capabilities": sorted(capabilities),
                "supportedOutputContracts": sorted(supported_output_contracts),
                "maxConcurrency": max_concurrency,
                "resourceProfile": resource_profile,
            },
        )
        _require_exact_keys(payload, {"workerId", "status"})
        status = _require_enum(payload, "status", _REGISTRATION_STATES)
        return WorkerRegistrationResult(
            worker_id=_require_token_value(payload, "workerId"),
            status=cast(Literal["registered", "already_registered"], status),
        )

    def acquire_lease(
        self,
        *,
        capability: WorkCapability,
        lease_duration_seconds: int = 300,
        heartbeat_interval_seconds: int = 60,
    ) -> WorkerLease | None:
        _require_capability(capability)
        _require_lease_timing(lease_duration_seconds, heartbeat_interval_seconds)
        payload = self._request_json(
            "POST",
            "/worker/leases/acquire",
            {
                "capability": capability,
                "leaseDurationSeconds": lease_duration_seconds,
                "heartbeatIntervalSeconds": heartbeat_interval_seconds,
            },
        )
        state = _require_string(payload, "state")
        if state == "no_eligible_work":
            _require_exact_keys(payload, {"state", "capability"})
            returned_capability = _require_enum(payload, "capability", _WORK_CAPABILITIES)
            if returned_capability != capability:
                raise self._protocol_failure(
                    code="WORKER_GATEWAY_CAPABILITY_MISMATCH",
                    message="The no-work response names a different capability.",
                    context={
                        "requestedCapability": capability,
                        "actualCapability": returned_capability,
                    },
                )
            return None
        if state != "acquired":
            raise self._protocol_failure(
                code="WORKER_GATEWAY_LEASE_STATE_INVALID",
                message="The lease response has an unsupported state.",
                context={"actualState": state},
            )
        _require_exact_keys(payload, {"state", "lease"})
        lease_payload = _require_object(payload, "lease")
        lease = _parse_lease(lease_payload)
        if lease.capability != capability:
            raise self._protocol_failure(
                code="WORKER_GATEWAY_CAPABILITY_MISMATCH",
                message="The acquired lease names a different capability.",
                context={
                    "requestedCapability": capability,
                    "actualCapability": lease.capability,
                },
            )
        return lease

    def heartbeat(
        self,
        lease: WorkerLease,
        *,
        lease_duration_seconds: int = 300,
        heartbeat_interval_seconds: int = 60,
    ) -> WorkerLease:
        _require_lease_timing(lease_duration_seconds, heartbeat_interval_seconds)
        payload = self._request_json(
            "POST",
            f"/worker/leases/{lease.lease_id}/heartbeat",
            {
                "workId": str(lease.work_id),
                "leaseToken": str(lease.lease_token),
                "inputDigest": lease.input_digest,
                "leaseDurationSeconds": lease_duration_seconds,
                "heartbeatIntervalSeconds": heartbeat_interval_seconds,
            },
        )
        renewed = _parse_lease(payload)
        _require_same_lease_identity(lease, renewed)
        return renewed

    def prepare_upload(
        self,
        lease: WorkerLease,
        *,
        upload_id: UUID,
        artifact_kind: ArtifactKind,
        expected_digest: str,
        expected_size_bytes: int,
        content_type: str,
        expires_in_seconds: int = 900,
    ) -> PreparedUpload:
        _require_artifact_kind(artifact_kind)
        _require_digest("expected_digest", expected_digest)
        _require_size(expected_size_bytes)
        _require_content_type(content_type)
        _require_transfer_expiry(expires_in_seconds)
        payload = self._request_json(
            "POST",
            "/worker/artifacts/prepare-upload",
            {
                "uploadId": str(upload_id),
                "workId": str(lease.work_id),
                "leaseId": str(lease.lease_id),
                "leaseToken": str(lease.lease_token),
                "inputDigest": lease.input_digest,
                "artifactKind": artifact_kind,
                "expectedDigest": expected_digest,
                "expectedSizeBytes": expected_size_bytes,
                "contentType": content_type,
                "expiresInSeconds": expires_in_seconds,
            },
        )
        _require_exact_keys(
            payload,
            {"uploadId", "method", "url", "requiredHeaders", "expiresAtUtc"},
        )
        returned_upload_id = _require_uuid(payload, "uploadId")
        if returned_upload_id != upload_id:
            raise self._protocol_failure(
                code="WORKER_GATEWAY_UPLOAD_ID_MISMATCH",
                message="The prepared upload response names a different upload.",
                context={
                    "requestedUploadId": str(upload_id),
                    "actualUploadId": str(returned_upload_id),
                },
            )
        method = _require_string(payload, "method")
        if method != "PUT":
            raise self._protocol_failure(
                code="WORKER_GATEWAY_UPLOAD_METHOD_INVALID",
                message="The prepared artifact upload must use PUT.",
                context={"actualMethod": method},
            )
        return PreparedUpload(
            upload_id=returned_upload_id,
            method="PUT",
            url=_require_http_url_value(payload, "url"),
            required_headers=_require_string_mapping(payload, "requiredHeaders"),
            expires_at_utc=_require_datetime(payload, "expiresAtUtc"),
        )

    def put_prepared_upload(self, prepared: PreparedUpload, content: bytes) -> None:
        response = self._object_request(
            "PUT",
            prepared.url,
            headers=dict(prepared.required_headers),
            content=content,
        )
        if not 200 <= response.status_code < 300:
            raise self._object_failure(
                code="OBJECT_UPLOAD_REJECTED",
                message="The prepared object upload was rejected.",
                response=response,
            )

    def verify_upload(self, lease: WorkerLease, *, upload_id: UUID) -> VerifiedUpload:
        payload = self._request_json(
            "POST",
            "/worker/artifacts/verify-upload",
            {
                "uploadId": str(upload_id),
                "workId": str(lease.work_id),
                "leaseId": str(lease.lease_id),
                "leaseToken": str(lease.lease_token),
                "inputDigest": lease.input_digest,
            },
        )
        _require_exact_keys(
            payload,
            {
                "uploadId",
                "workId",
                "artifactKind",
                "contentDigest",
                "sizeBytes",
                "contentType",
                "storageReference",
                "verifiedAtUtc",
            },
        )
        verified = VerifiedUpload(
            upload_id=_require_uuid(payload, "uploadId"),
            work_id=_require_uuid(payload, "workId"),
            artifact_kind=cast(
                ArtifactKind,
                _require_enum(
                    payload,
                    "artifactKind",
                    {"raw_artifact", "diagnostic_artifact", "derived_artifact"},
                ),
            ),
            content_digest=_require_digest_value(payload, "contentDigest"),
            size_bytes=_require_int(payload, "sizeBytes", minimum=1, maximum=_MAX_OBJECT_BYTES),
            content_type=_require_content_type_value(payload, "contentType"),
            storage_reference=_require_string(payload, "storageReference"),
            verified_at_utc=_require_datetime(payload, "verifiedAtUtc"),
        )
        if verified.upload_id != upload_id or verified.work_id != lease.work_id:
            raise self._protocol_failure(
                code="WORKER_GATEWAY_VERIFIED_UPLOAD_IDENTITY_MISMATCH",
                message=(
                    "The verified upload response does not match the requested work and upload."
                ),
                context={
                    "requestedUploadId": str(upload_id),
                    "actualUploadId": str(verified.upload_id),
                    "requestedWorkId": str(lease.work_id),
                    "actualWorkId": str(verified.work_id),
                },
            )
        return verified

    def upload_bytes(
        self,
        lease: WorkerLease,
        *,
        content: bytes,
        artifact_kind: ArtifactKind,
        content_type: str,
        upload_id: UUID | None = None,
        expires_in_seconds: int = 900,
    ) -> VerifiedUpload:
        selected_upload_id = upload_id or uuid4()
        digest = f"sha256:{sha256(content).hexdigest()}"
        prepared = self.prepare_upload(
            lease,
            upload_id=selected_upload_id,
            artifact_kind=artifact_kind,
            expected_digest=digest,
            expected_size_bytes=len(content),
            content_type=content_type,
            expires_in_seconds=expires_in_seconds,
        )
        self.put_prepared_upload(prepared, content)
        verified = self.verify_upload(lease, upload_id=selected_upload_id)
        if (
            verified.content_digest != digest
            or verified.size_bytes != len(content)
            or verified.content_type != content_type
            or verified.artifact_kind != artifact_kind
        ):
            raise self._protocol_failure(
                code="WORKER_GATEWAY_VERIFIED_UPLOAD_CONTRACT_MISMATCH",
                message="The verified upload does not match the exact uploaded content contract.",
                context={"uploadId": str(selected_upload_id)},
            )
        return verified

    def prepare_read(
        self,
        lease: WorkerLease,
        *,
        artifact_id: UUID,
        expires_in_seconds: int = 900,
    ) -> PreparedRead:
        _require_transfer_expiry(expires_in_seconds)
        payload = self._request_json(
            "POST",
            "/worker/artifacts/prepare-read",
            {
                "artifactId": str(artifact_id),
                "workId": str(lease.work_id),
                "leaseId": str(lease.lease_id),
                "leaseToken": str(lease.lease_token),
                "inputDigest": lease.input_digest,
                "expiresInSeconds": expires_in_seconds,
            },
        )
        _require_exact_keys(payload, {"artifactId", "method", "url", "expiresAtUtc"})
        returned_artifact_id = _require_uuid(payload, "artifactId")
        if returned_artifact_id != artifact_id:
            raise self._protocol_failure(
                code="WORKER_GATEWAY_ARTIFACT_ID_MISMATCH",
                message="The prepared read response names a different artifact.",
                context={
                    "requestedArtifactId": str(artifact_id),
                    "actualArtifactId": str(returned_artifact_id),
                },
            )
        method = _require_string(payload, "method")
        if method != "GET":
            raise self._protocol_failure(
                code="WORKER_GATEWAY_READ_METHOD_INVALID",
                message="The prepared artifact read must use GET.",
                context={"actualMethod": method},
            )
        return PreparedRead(
            artifact_id=returned_artifact_id,
            method="GET",
            url=_require_http_url_value(payload, "url"),
            expires_at_utc=_require_datetime(payload, "expiresAtUtc"),
        )

    def get_prepared_read(
        self,
        prepared: PreparedRead,
        *,
        maximum_bytes: int = _MAX_OBJECT_BYTES,
    ) -> bytes:
        if not 1 <= maximum_bytes <= _MAX_OBJECT_BYTES:
            raise ValueError("maximum_bytes is outside the supported object range")
        response = self._object_request("GET", prepared.url)
        if not 200 <= response.status_code < 300:
            raise self._object_failure(
                code="OBJECT_READ_REJECTED",
                message="The prepared object read was rejected.",
                response=response,
            )
        content = response.content
        if len(content) > maximum_bytes:
            raise self._local_failure(
                owner="SourceConnectorSdk.ObjectTransfer",
                code="OBJECT_READ_TOO_LARGE",
                message="The downloaded object exceeds the caller byte limit.",
                context={
                    "artifactId": str(prepared.artifact_id),
                    "actualBytes": len(content),
                    "maximumBytes": maximum_bytes,
                },
                required_action="Reject the work input and report a permanent size-limit failure.",
            )
        return content

    def read_artifact(
        self,
        lease: WorkerLease,
        *,
        artifact_id: UUID,
        expires_in_seconds: int = 900,
        maximum_bytes: int = _MAX_OBJECT_BYTES,
    ) -> bytes:
        prepared = self.prepare_read(
            lease,
            artifact_id=artifact_id,
            expires_in_seconds=expires_in_seconds,
        )
        return self.get_prepared_read(prepared, maximum_bytes=maximum_bytes)

    def complete(
        self,
        lease: WorkerLease,
        *,
        output_contract: str,
        output_digest: str,
        worker_build_identity: str,
        output_artifacts: Sequence[tuple[UUID, str]] = (),
    ) -> WorkCompletionResult:
        _require_token("output_contract", output_contract)
        _require_digest("output_digest", output_digest)
        _require_token("worker_build_identity", worker_build_identity)
        _require_output_bindings(output_artifacts)
        payload = self._request_json(
            "POST",
            f"/worker/work/{lease.work_id}/complete",
            {
                "leaseId": str(lease.lease_id),
                "leaseToken": str(lease.lease_token),
                "inputDigest": lease.input_digest,
                "outputContract": output_contract,
                "outputDigest": output_digest,
                "workerBuildIdentity": worker_build_identity,
                "outputArtifacts": [
                    {"uploadId": str(upload_id), "role": role}
                    for upload_id, role in output_artifacts
                ],
            },
        )
        _require_exact_keys(payload, {"workId", "status", "outputDigest", "revision"})
        result = WorkCompletionResult(
            work_id=_require_uuid(payload, "workId"),
            status=cast(
                Literal["applied", "already_applied"],
                _require_enum(payload, "status", _COMPLETION_STATES),
            ),
            output_digest=_require_digest_value(payload, "outputDigest"),
            revision=_require_int(payload, "revision", minimum=0),
        )
        if result.work_id != lease.work_id or result.output_digest != output_digest:
            raise self._protocol_failure(
                code="WORKER_GATEWAY_COMPLETION_IDENTITY_MISMATCH",
                message="The completion response does not match the submitted work result.",
                context={
                    "requestedWorkId": str(lease.work_id),
                    "actualWorkId": str(result.work_id),
                    "requestedOutputDigest": output_digest,
                    "actualOutputDigest": result.output_digest,
                },
            )
        return result

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: WorkFailureKind,
        code: str,
        owner: str,
        message: str,
        required_action: str,
        worker_build_identity: str,
    ) -> WorkMutationResult:
        if failure_kind not in _FAILURE_KINDS:
            raise ValueError("failure_kind is unsupported")
        _require_code(code)
        _require_bounded_text("owner", owner, 100)
        _require_bounded_text("message", message, 500)
        _require_bounded_text("required_action", required_action, 500)
        _require_token("worker_build_identity", worker_build_identity)
        payload = self._request_json(
            "POST",
            f"/worker/work/{lease.work_id}/fail",
            {
                "leaseId": str(lease.lease_id),
                "leaseToken": str(lease.lease_token),
                "inputDigest": lease.input_digest,
                "failureKind": failure_kind,
                "code": code,
                "owner": owner,
                "message": message,
                "requiredAction": required_action,
                "workerBuildIdentity": worker_build_identity,
            },
        )
        return _parse_mutation(payload, expected_work_id=lease.work_id, gateway=self)

    def release(
        self,
        lease: WorkerLease,
        *,
        reason_code: str,
        worker_build_identity: str,
    ) -> WorkMutationResult:
        _require_code(reason_code)
        _require_token("worker_build_identity", worker_build_identity)
        payload = self._request_json(
            "POST",
            f"/worker/work/{lease.work_id}/release",
            {
                "leaseId": str(lease.lease_id),
                "leaseToken": str(lease.lease_token),
                "inputDigest": lease.input_digest,
                "reasonCode": reason_code,
                "workerBuildIdentity": worker_build_identity,
            },
        )
        return _parse_mutation(payload, expected_work_id=lease.work_id, gateway=self)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            response = self._gateway.request(method, path, json=dict(payload))
        except httpx.HTTPError as exc:
            raise self._local_failure(
                owner="SourceConnectorSdk.WorkerGateway",
                code="WORKER_GATEWAY_UNREACHABLE",
                message="The source connector could not reach Worker Gateway.",
                context={"causeType": type(exc).__name__},
                required_action=(
                    "Restore Worker Gateway connectivity and retry the same work action."
                ),
            ) from exc
        if not 200 <= response.status_code < 300:
            raise self._gateway_failure(response)
        return _response_object(response, self)

    def _object_request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        try:
            return self._objects.request(
                method,
                url,
                headers=dict(headers or {}),
                content=content,
            )
        except httpx.HTTPError as exc:
            raise self._local_failure(
                owner="SourceConnectorSdk.ObjectTransfer",
                code="OBJECT_TRANSFER_UNREACHABLE",
                message="The source connector could not reach the prepared object URL.",
                context={"causeType": type(exc).__name__},
                required_action="Restore object-store connectivity and retry the exact transfer.",
            ) from exc

    def _gateway_failure(self, response: httpx.Response) -> WorkerGatewayFailure:
        try:
            envelope = ErrorEnvelope.model_validate(response.json())
        except Exception as exc:
            raise self._protocol_failure(
                code="WORKER_GATEWAY_ERROR_ENVELOPE_INVALID",
                message=(
                    "Worker Gateway returned a non-success response without its error contract."
                ),
                context={
                    "statusCode": response.status_code,
                    "causeType": type(exc).__name__,
                },
                status_code=response.status_code,
            ) from exc
        return WorkerGatewayFailure(status_code=response.status_code, envelope=envelope)

    def _object_failure(
        self,
        *,
        code: str,
        message: str,
        response: httpx.Response,
    ) -> WorkerGatewayFailure:
        return self._local_failure(
            owner="SourceConnectorSdk.ObjectTransfer",
            code=code,
            message=message,
            context={"statusCode": response.status_code},
            required_action=(
                "Inspect the scoped object transfer and retry only while the URL is valid."
            ),
            status_code=response.status_code,
            correlation_id=response.headers.get("X-Correlation-Id"),
        )

    def _protocol_failure(
        self,
        *,
        code: str,
        message: str,
        context: Mapping[str, object],
        status_code: int | None = None,
    ) -> WorkerGatewayFailure:
        return self._local_failure(
            owner="SourceConnectorSdk.WorkerGatewayContract",
            code=code,
            message=message,
            context=context,
            required_action=(
                "Stop the connector, restore compatibility with the committed Worker Gateway "
                "OpenAPI contract, and retry with the same immutable input."
            ),
            status_code=status_code,
        )

    @staticmethod
    def _local_failure(
        *,
        owner: str,
        code: str,
        message: str,
        context: Mapping[str, object],
        required_action: str,
        status_code: int | None = None,
        correlation_id: str | None = None,
    ) -> WorkerGatewayFailure:
        return WorkerGatewayFailure(
            status_code=status_code,
            envelope=ErrorEnvelope(
                type=f"collection/{code.lower().replace('_', '-')}",
                owner=owner,
                code=code,
                message=message,
                context=dict(context),
                required_action=required_action,
                correlation_id=correlation_id or f"connector-sdk-{uuid4()}",
            ),
        )


def _parse_lease(payload: Mapping[str, object]) -> WorkerLease:
    _require_exact_keys(
        payload,
        {
            "leaseId",
            "workId",
            "leaseToken",
            "workerId",
            "stage",
            "capability",
            "inputDigest",
            "expectedOutputContract",
            "issuedAtUtc",
            "expiresAtUtc",
            "heartbeatDeadlineUtc",
            "sourcePermit",
            "inputArtifacts",
            "correlationId",
        },
    )
    permit_value = payload["sourcePermit"]
    source_permit: SourcePermit | None = None
    if permit_value is not None:
        permit_payload = _as_object(permit_value, "sourcePermit")
        _require_exact_keys(
            permit_payload,
            {"sourceKey", "policyDigest", "permitNotBeforeUtc"},
        )
        source_permit = SourcePermit(
            source_key=_require_token_value(permit_payload, "sourceKey"),
            policy_digest=_require_digest_value(permit_payload, "policyDigest"),
            permit_not_before_utc=_require_datetime(permit_payload, "permitNotBeforeUtc"),
        )
    artifact_values = _require_array(payload, "inputArtifacts")
    artifacts: list[LeaseArtifact] = []
    for value in artifact_values:
        item = _as_object(value, "inputArtifacts[]")
        _require_exact_keys(item, {"artifactId", "role"})
        artifacts.append(
            LeaseArtifact(
                artifact_id=_require_uuid(item, "artifactId"),
                role=_require_role_value(item, "role"),
            )
        )
    return WorkerLease(
        lease_id=_require_uuid(payload, "leaseId"),
        work_id=_require_uuid(payload, "workId"),
        lease_token=_require_uuid(payload, "leaseToken"),
        worker_id=_require_token_value(payload, "workerId"),
        stage=cast(WorkStage, _require_enum(payload, "stage", _WORK_STAGES)),
        capability=cast(
            WorkCapability,
            _require_enum(payload, "capability", _WORK_CAPABILITIES),
        ),
        input_digest=_require_digest_value(payload, "inputDigest"),
        expected_output_contract=_require_token_value(payload, "expectedOutputContract"),
        issued_at_utc=_require_datetime(payload, "issuedAtUtc"),
        expires_at_utc=_require_datetime(payload, "expiresAtUtc"),
        heartbeat_deadline_utc=_require_datetime(payload, "heartbeatDeadlineUtc"),
        source_permit=source_permit,
        input_artifacts=tuple(artifacts),
        correlation_id=_require_token_value(payload, "correlationId"),
    )


def _parse_mutation(
    payload: Mapping[str, object],
    *,
    expected_work_id: UUID,
    gateway: SourceWorkerGateway,
) -> WorkMutationResult:
    _require_exact_keys(payload, {"workId", "state", "revision", "availableAtUtc"})
    work_id = _require_uuid(payload, "workId")
    if work_id != expected_work_id:
        raise gateway._protocol_failure(
            code="WORKER_GATEWAY_MUTATION_IDENTITY_MISMATCH",
            message="The work mutation response names a different work unit.",
            context={
                "requestedWorkId": str(expected_work_id),
                "actualWorkId": str(work_id),
            },
        )
    available_value = payload["availableAtUtc"]
    available_at_utc = (
        None if available_value is None else _parse_datetime(available_value, "availableAtUtc")
    )
    return WorkMutationResult(
        work_id=work_id,
        state=_require_enum(payload, "state", _WORK_STATES),
        revision=_require_int(payload, "revision", minimum=0),
        available_at_utc=available_at_utc,
    )


def _response_object(
    response: httpx.Response,
    gateway: SourceWorkerGateway,
) -> dict[str, object]:
    try:
        value = response.json()
    except ValueError as exc:
        raise gateway._protocol_failure(
            code="WORKER_GATEWAY_RESPONSE_JSON_INVALID",
            message="Worker Gateway returned a successful response that is not JSON.",
            context={"statusCode": response.status_code},
        ) from exc
    return _as_object(value, "response")


def _as_object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a JSON object with string keys")
    return {cast(str, key): item for key, item in value.items()}


def _require_object(payload: Mapping[str, object], key: str) -> dict[str, object]:
    if key not in payload:
        raise ValueError(f"response field {key} is missing")
    return _as_object(payload[key], key)


def _require_array(payload: Mapping[str, object], key: str) -> tuple[object, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"response field {key} must be an array")
    return tuple(value)


def _require_exact_keys(payload: Mapping[str, object], expected: set[str]) -> None:
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"response field set mismatch; missing={sorted(expected - actual)!r}; "
            f"unknown={sorted(actual - expected)!r}"
        )


def _require_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"response field {key} must be a string")
    return value


def _require_token_value(payload: Mapping[str, object], key: str) -> str:
    value = _require_string(payload, key)
    _require_token(key, value)
    return value


def _require_digest_value(payload: Mapping[str, object], key: str) -> str:
    value = _require_string(payload, key)
    _require_digest(key, value)
    return value


def _require_content_type_value(payload: Mapping[str, object], key: str) -> str:
    value = _require_string(payload, key)
    _require_content_type(value)
    return value


def _require_role_value(payload: Mapping[str, object], key: str) -> str:
    value = _require_string(payload, key)
    _require_role(value)
    return value


def _require_http_url_value(payload: Mapping[str, object], key: str) -> str:
    value = _require_string(payload, key)
    _require_http_url(key, value)
    return value


def _require_uuid(payload: Mapping[str, object], key: str) -> UUID:
    value = _require_string(payload, key)
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValueError(f"response field {key} must be a UUID") from exc


def _require_int(
    payload: Mapping[str, object],
    key: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"response field {key} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"response field {key} is outside the supported range")
    return value


def _require_datetime(payload: Mapping[str, object], key: str) -> datetime:
    if key not in payload:
        raise ValueError(f"response field {key} is missing")
    return _parse_datetime(payload[key], key)


def _parse_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"response field {name} must be an RFC 3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"response field {name} must be an RFC 3339 timestamp") from exc
    _require_utc(name, parsed)
    return parsed


def _require_string_mapping(payload: Mapping[str, object], key: str) -> dict[str, str]:
    value = _require_object(payload, key)
    result: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(item, str) or not name or not item:
            raise ValueError(f"response field {key} must contain non-empty string headers")
        if name.lower() != name:
            raise ValueError(f"response field {key} header names must be lower-case")
        result[name] = item
    return result


def _require_enum(payload: Mapping[str, object], key: str, allowed: Collection[str]) -> str:
    value = _require_string(payload, key)
    if value not in allowed:
        raise ValueError(f"response field {key} has an unsupported value")
    return value


def _require_same_lease_identity(previous: WorkerLease, renewed: WorkerLease) -> None:
    if (
        previous.lease_id != renewed.lease_id
        or previous.work_id != renewed.work_id
        or previous.lease_token != renewed.lease_token
        or previous.worker_id != renewed.worker_id
        or previous.stage != renewed.stage
        or previous.capability != renewed.capability
        or previous.input_digest != renewed.input_digest
        or previous.expected_output_contract != renewed.expected_output_contract
        or previous.issued_at_utc != renewed.issued_at_utc
        or previous.source_permit != renewed.source_permit
        or previous.input_artifacts != renewed.input_artifacts
    ):
        raise ValueError("heartbeat response changed immutable lease identity")


def _require_output_bindings(bindings: Sequence[tuple[UUID, str]]) -> None:
    if len(bindings) > 32:
        raise ValueError("work completion cannot contain more than 32 output artifacts")
    upload_ids = tuple(upload_id for upload_id, _role in bindings)
    roles = tuple(role for _upload_id, role in bindings)
    if len(set(upload_ids)) != len(upload_ids) or len(set(roles)) != len(roles):
        raise ValueError("work completion output identities and roles must be unique")
    for role in roles:
        _require_role(role)


def _normalize_base_url(value: str) -> str:
    _require_http_url("worker gateway base URL", value)
    parts = urlsplit(value)
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("worker gateway base URL cannot contain credentials, query, or fragment")
    return value.rstrip("/") + "/"


def _require_http_url(name: str, value: str) -> None:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    if len(value) > 8_192:
        raise ValueError(f"{name} is too large")


def _require_token(name: str, value: str) -> None:
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid token format")


def _require_code(value: str) -> None:
    if _CODE_PATTERN.fullmatch(value) is None:
        raise ValueError("failure code has an invalid format")


def _require_role(value: str) -> None:
    if _ROLE_PATTERN.fullmatch(value) is None:
        raise ValueError("artifact role has an invalid format")


def _require_digest(name: str, value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical SHA-256 digest")


def _require_content_type(value: str) -> None:
    if _CONTENT_TYPE_PATTERN.fullmatch(value) is None:
        raise ValueError("content type has an invalid format")


def _require_size(value: int) -> None:
    if not 1 <= value <= _MAX_OBJECT_BYTES:
        raise ValueError("artifact size is outside the supported range")


def _require_transfer_expiry(value: int) -> None:
    if not 60 <= value <= 3_600:
        raise ValueError("artifact transfer expiry must be between 60 and 3600 seconds")


def _require_lease_timing(lease_duration_seconds: int, heartbeat_interval_seconds: int) -> None:
    if not 5 <= lease_duration_seconds <= 86_400:
        raise ValueError("lease duration must be between 5 and 86400 seconds")
    if not 1 <= heartbeat_interval_seconds < lease_duration_seconds:
        raise ValueError("heartbeat interval must be positive and below lease duration")


def _require_capability(value: str) -> None:
    if value not in _WORK_CAPABILITIES:
        raise ValueError("worker capability is unsupported")


def _require_artifact_kind(value: str) -> None:
    if value not in {"raw_artifact", "diagnostic_artifact", "derived_artifact"}:
        raise ValueError("artifact kind is unsupported")


def _require_bounded_text(name: str, value: str, maximum: int) -> None:
    if not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-empty and at most {maximum} characters")


def _require_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{name} must be timezone-aware UTC")
