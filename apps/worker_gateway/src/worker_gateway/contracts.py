from __future__ import annotations

from datetime import datetime
from typing import Annotated, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from collection_application import (
    ArtifactKind,
    PreparedArtifactRead,
    PreparedArtifactUpload,
    VerifiedArtifactUpload,
    WorkCapability,
    WorkCompletionResult,
    WorkCompletionStatus,
    WorkerRegistrationResult,
    WorkerRegistrationStatus,
    WorkFailureKind,
    WorkLease,
    WorkMutationResult,
    WorkOutputArtifact,
    WorkStage,
    WorkUnitState,
)

_WIRE_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_CODE_PATTERN = r"^[A-Z][A-Z0-9_]{0,99}$"
_ROLE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$"


class WorkerWireModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


class WorkerRegistrationRequest(WorkerWireModel):
    build_identity: str = Field(
        alias="buildIdentity",
        serialization_alias="buildIdentity",
        pattern=_WIRE_IDENTITY_PATTERN,
    )
    capabilities: frozenset[WorkCapability] = Field(min_length=1)
    supported_output_contracts: frozenset[str] = Field(
        alias="supportedOutputContracts",
        serialization_alias="supportedOutputContracts",
        min_length=1,
    )
    max_concurrency: int = Field(
        alias="maxConcurrency",
        serialization_alias="maxConcurrency",
        ge=1,
        le=10_000,
    )
    resource_profile: str = Field(
        alias="resourceProfile",
        serialization_alias="resourceProfile",
        pattern=_WIRE_IDENTITY_PATTERN,
    )


class WorkerRegistrationResponse(WorkerWireModel):
    worker_id: str = Field(alias="workerId", serialization_alias="workerId")
    status: WorkerRegistrationStatus

    @classmethod
    def from_result(cls, result: WorkerRegistrationResult) -> WorkerRegistrationResponse:
        return cls(worker_id=result.worker_id, status=result.status)


class LeaseAcquireRequest(WorkerWireModel):
    capability: WorkCapability
    lease_duration_seconds: int = Field(
        alias="leaseDurationSeconds",
        serialization_alias="leaseDurationSeconds",
        ge=5,
        le=86_400,
    )
    heartbeat_interval_seconds: int = Field(
        alias="heartbeatIntervalSeconds",
        serialization_alias="heartbeatIntervalSeconds",
        ge=1,
        le=86_399,
    )


class LeaseHeartbeatRequest(WorkerWireModel):
    work_id: UUID = Field(alias="workId", serialization_alias="workId")
    lease_token: UUID = Field(alias="leaseToken", serialization_alias="leaseToken")
    input_digest: str = Field(
        alias="inputDigest",
        serialization_alias="inputDigest",
        pattern=_DIGEST_PATTERN,
    )
    lease_duration_seconds: int = Field(
        alias="leaseDurationSeconds",
        serialization_alias="leaseDurationSeconds",
        ge=5,
        le=86_400,
    )
    heartbeat_interval_seconds: int = Field(
        alias="heartbeatIntervalSeconds",
        serialization_alias="heartbeatIntervalSeconds",
        ge=1,
        le=86_399,
    )


class ArtifactPrepareUploadRequest(WorkerWireModel):
    upload_id: UUID = Field(alias="uploadId", serialization_alias="uploadId")
    work_id: UUID = Field(alias="workId", serialization_alias="workId")
    lease_id: UUID = Field(alias="leaseId", serialization_alias="leaseId")
    lease_token: UUID = Field(alias="leaseToken", serialization_alias="leaseToken")
    input_digest: str = Field(
        alias="inputDigest",
        serialization_alias="inputDigest",
        pattern=_DIGEST_PATTERN,
    )
    artifact_kind: ArtifactKind = Field(
        alias="artifactKind",
        serialization_alias="artifactKind",
    )
    expected_digest: str = Field(
        alias="expectedDigest",
        serialization_alias="expectedDigest",
        pattern=_DIGEST_PATTERN,
    )
    expected_size_bytes: int = Field(
        alias="expectedSizeBytes",
        serialization_alias="expectedSizeBytes",
        ge=1,
        le=5 * 1024 * 1024 * 1024,
    )
    content_type: str = Field(
        alias="contentType",
        serialization_alias="contentType",
        min_length=3,
        max_length=129,
    )
    expires_in_seconds: int = Field(
        alias="expiresInSeconds",
        serialization_alias="expiresInSeconds",
        ge=60,
        le=3_600,
    )


class ArtifactVerifyUploadRequest(WorkerWireModel):
    upload_id: UUID = Field(alias="uploadId", serialization_alias="uploadId")
    work_id: UUID = Field(alias="workId", serialization_alias="workId")
    lease_id: UUID = Field(alias="leaseId", serialization_alias="leaseId")
    lease_token: UUID = Field(alias="leaseToken", serialization_alias="leaseToken")
    input_digest: str = Field(
        alias="inputDigest",
        serialization_alias="inputDigest",
        pattern=_DIGEST_PATTERN,
    )


class ArtifactPrepareReadRequest(WorkerWireModel):
    artifact_id: UUID = Field(alias="artifactId", serialization_alias="artifactId")
    work_id: UUID = Field(alias="workId", serialization_alias="workId")
    lease_id: UUID = Field(alias="leaseId", serialization_alias="leaseId")
    lease_token: UUID = Field(alias="leaseToken", serialization_alias="leaseToken")
    input_digest: str = Field(
        alias="inputDigest",
        serialization_alias="inputDigest",
        pattern=_DIGEST_PATTERN,
    )
    expires_in_seconds: int = Field(
        alias="expiresInSeconds",
        serialization_alias="expiresInSeconds",
        ge=60,
        le=3_600,
    )


class PreparedArtifactUploadResponse(WorkerWireModel):
    upload_id: UUID = Field(alias="uploadId", serialization_alias="uploadId")
    method: Literal["PUT"]
    url: str
    required_headers: dict[str, str] = Field(
        alias="requiredHeaders",
        serialization_alias="requiredHeaders",
    )
    expires_at_utc: datetime = Field(alias="expiresAtUtc", serialization_alias="expiresAtUtc")

    @classmethod
    def from_result(cls, result: PreparedArtifactUpload) -> PreparedArtifactUploadResponse:
        return cls(
            upload_id=result.upload_id,
            method="PUT",
            url=result.url,
            required_headers=dict(result.required_headers),
            expires_at_utc=result.expires_at_utc,
        )


class VerifiedArtifactUploadResponse(WorkerWireModel):
    upload_id: UUID = Field(alias="uploadId", serialization_alias="uploadId")
    work_id: UUID = Field(alias="workId", serialization_alias="workId")
    artifact_kind: ArtifactKind = Field(
        alias="artifactKind",
        serialization_alias="artifactKind",
    )
    content_digest: str = Field(
        alias="contentDigest",
        serialization_alias="contentDigest",
        pattern=_DIGEST_PATTERN,
    )
    size_bytes: int = Field(alias="sizeBytes", serialization_alias="sizeBytes", ge=1)
    content_type: str = Field(alias="contentType", serialization_alias="contentType")
    storage_reference: str = Field(
        alias="storageReference",
        serialization_alias="storageReference",
    )
    verified_at_utc: datetime = Field(
        alias="verifiedAtUtc",
        serialization_alias="verifiedAtUtc",
    )

    @classmethod
    def from_result(cls, result: VerifiedArtifactUpload) -> VerifiedArtifactUploadResponse:
        return cls(
            upload_id=result.upload_id,
            work_id=result.work_id,
            artifact_kind=result.artifact_kind,
            content_digest=result.content_digest,
            size_bytes=result.size_bytes,
            content_type=result.content_type,
            storage_reference=result.storage_reference,
            verified_at_utc=result.verified_at_utc,
        )


class PreparedArtifactReadResponse(WorkerWireModel):
    artifact_id: UUID = Field(alias="artifactId", serialization_alias="artifactId")
    method: Literal["GET"]
    url: str
    expires_at_utc: datetime = Field(alias="expiresAtUtc", serialization_alias="expiresAtUtc")

    @classmethod
    def from_result(cls, result: PreparedArtifactRead) -> PreparedArtifactReadResponse:
        return cls(
            artifact_id=result.artifact_id,
            method="GET",
            url=result.url,
            expires_at_utc=result.expires_at_utc,
        )


class WorkOutputArtifactRequest(WorkerWireModel):
    upload_id: UUID = Field(alias="uploadId", serialization_alias="uploadId")
    role: str = Field(pattern=_ROLE_PATTERN)

    def to_application(self) -> WorkOutputArtifact:
        return WorkOutputArtifact(upload_id=self.upload_id, role=self.role)


class WorkCompletionRequest(WorkerWireModel):
    lease_id: UUID = Field(alias="leaseId", serialization_alias="leaseId")
    lease_token: UUID = Field(alias="leaseToken", serialization_alias="leaseToken")
    input_digest: str = Field(
        alias="inputDigest",
        serialization_alias="inputDigest",
        pattern=_DIGEST_PATTERN,
    )
    output_contract: str = Field(
        alias="outputContract",
        serialization_alias="outputContract",
        pattern=_WIRE_IDENTITY_PATTERN,
    )
    output_digest: str = Field(
        alias="outputDigest",
        serialization_alias="outputDigest",
        pattern=_DIGEST_PATTERN,
    )
    worker_build_identity: str = Field(
        alias="workerBuildIdentity",
        serialization_alias="workerBuildIdentity",
        pattern=_WIRE_IDENTITY_PATTERN,
    )
    output_artifacts: tuple[WorkOutputArtifactRequest, ...] = Field(
        default=(),
        alias="outputArtifacts",
        serialization_alias="outputArtifacts",
        max_length=32,
    )


class WorkFailureRequest(WorkerWireModel):
    lease_id: UUID = Field(alias="leaseId", serialization_alias="leaseId")
    lease_token: UUID = Field(alias="leaseToken", serialization_alias="leaseToken")
    input_digest: str = Field(
        alias="inputDigest",
        serialization_alias="inputDigest",
        pattern=_DIGEST_PATTERN,
    )
    failure_kind: WorkFailureKind = Field(
        alias="failureKind",
        serialization_alias="failureKind",
    )
    code: str = Field(pattern=_CODE_PATTERN)
    owner: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    required_action: str = Field(
        alias="requiredAction",
        serialization_alias="requiredAction",
        min_length=1,
        max_length=500,
    )
    worker_build_identity: str = Field(
        alias="workerBuildIdentity",
        serialization_alias="workerBuildIdentity",
        pattern=_WIRE_IDENTITY_PATTERN,
    )


class WorkReleaseRequest(WorkerWireModel):
    lease_id: UUID = Field(alias="leaseId", serialization_alias="leaseId")
    lease_token: UUID = Field(alias="leaseToken", serialization_alias="leaseToken")
    input_digest: str = Field(
        alias="inputDigest",
        serialization_alias="inputDigest",
        pattern=_DIGEST_PATTERN,
    )
    reason_code: str = Field(
        alias="reasonCode",
        serialization_alias="reasonCode",
        pattern=_CODE_PATTERN,
    )
    worker_build_identity: str = Field(
        alias="workerBuildIdentity",
        serialization_alias="workerBuildIdentity",
        pattern=_WIRE_IDENTITY_PATTERN,
    )


class SourcePermitResponse(WorkerWireModel):
    source_key: str = Field(alias="sourceKey", serialization_alias="sourceKey")
    policy_digest: str = Field(
        alias="policyDigest",
        serialization_alias="policyDigest",
        pattern=_DIGEST_PATTERN,
    )
    permit_not_before_utc: datetime = Field(
        alias="permitNotBeforeUtc",
        serialization_alias="permitNotBeforeUtc",
    )


class WorkInputArtifactResponse(WorkerWireModel):
    artifact_id: UUID = Field(alias="artifactId", serialization_alias="artifactId")
    role: str = Field(pattern=_ROLE_PATTERN)


class WorkLeaseResponse(WorkerWireModel):
    lease_id: UUID = Field(alias="leaseId", serialization_alias="leaseId")
    work_id: UUID = Field(alias="workId", serialization_alias="workId")
    lease_token: UUID = Field(alias="leaseToken", serialization_alias="leaseToken")
    worker_id: str = Field(alias="workerId", serialization_alias="workerId")
    stage: WorkStage
    capability: WorkCapability
    input_digest: str = Field(
        alias="inputDigest",
        serialization_alias="inputDigest",
        pattern=_DIGEST_PATTERN,
    )
    expected_output_contract: str = Field(
        alias="expectedOutputContract",
        serialization_alias="expectedOutputContract",
    )
    issued_at_utc: datetime = Field(alias="issuedAtUtc", serialization_alias="issuedAtUtc")
    expires_at_utc: datetime = Field(alias="expiresAtUtc", serialization_alias="expiresAtUtc")
    heartbeat_deadline_utc: datetime = Field(
        alias="heartbeatDeadlineUtc",
        serialization_alias="heartbeatDeadlineUtc",
    )
    source_permit: SourcePermitResponse | None = Field(
        alias="sourcePermit",
        serialization_alias="sourcePermit",
    )
    input_artifacts: tuple[WorkInputArtifactResponse, ...] = Field(
        alias="inputArtifacts",
        serialization_alias="inputArtifacts",
    )
    correlation_id: str = Field(alias="correlationId", serialization_alias="correlationId")

    @classmethod
    def from_domain(cls, lease: WorkLease) -> WorkLeaseResponse:
        source_permit = (
            SourcePermitResponse(
                source_key=lease.source_permit.source_key,
                policy_digest=lease.source_permit.policy_digest,
                permit_not_before_utc=lease.source_permit.permit_not_before_utc,
            )
            if lease.source_permit is not None
            else None
        )
        return cls(
            lease_id=lease.lease_id,
            work_id=lease.work_id,
            lease_token=lease.lease_token,
            worker_id=lease.worker_id,
            stage=lease.stage,
            capability=lease.capability,
            input_digest=lease.input_digest,
            expected_output_contract=lease.expected_output_contract,
            issued_at_utc=lease.issued_at_utc,
            expires_at_utc=lease.expires_at_utc,
            heartbeat_deadline_utc=lease.heartbeat_deadline_utc,
            source_permit=source_permit,
            input_artifacts=tuple(
                WorkInputArtifactResponse(artifact_id=binding.artifact_id, role=binding.role)
                for binding in lease.input_artifacts
            ),
            correlation_id=lease.correlation_id,
        )


class LeaseAcquiredResponse(WorkerWireModel):
    state: Literal["acquired"] = "acquired"
    lease: WorkLeaseResponse


class NoEligibleWorkResponse(WorkerWireModel):
    state: Literal["no_eligible_work"] = "no_eligible_work"
    capability: WorkCapability


LeaseAcquireResponse = Annotated[
    LeaseAcquiredResponse | NoEligibleWorkResponse,
    Field(discriminator="state"),
]


class WorkCompletionResponse(WorkerWireModel):
    work_id: UUID = Field(alias="workId", serialization_alias="workId")
    status: WorkCompletionStatus
    output_digest: str = Field(
        alias="outputDigest",
        serialization_alias="outputDigest",
        pattern=_DIGEST_PATTERN,
    )
    revision: int = Field(ge=0)

    @classmethod
    def from_result(cls, result: WorkCompletionResult) -> WorkCompletionResponse:
        return cls(
            work_id=result.work_id,
            status=result.status,
            output_digest=result.output_digest,
            revision=result.revision,
        )


class WorkMutationResponse(WorkerWireModel):
    work_id: UUID = Field(alias="workId", serialization_alias="workId")
    state: WorkUnitState
    revision: int = Field(ge=0)
    available_at_utc: datetime | None = Field(
        alias="availableAtUtc",
        serialization_alias="availableAtUtc",
    )

    @classmethod
    def from_result(cls, result: WorkMutationResult) -> WorkMutationResponse:
        return cls(
            work_id=result.work_id,
            state=result.state,
            revision=result.revision,
            available_at_utc=result.available_at_utc,
        )


class WorkerProtocolMetadataResponse(WorkerWireModel):
    contract: Literal["collector-worker-protocol"] = "collector-worker-protocol"
    contract_revision: Literal["worker-protocol-v1"] = Field(
        default="worker-protocol-v1",
        alias="contractRevision",
        serialization_alias="contractRevision",
    )
    worker_id: str = Field(alias="workerId", serialization_alias="workerId")
    authorized_capabilities: tuple[WorkCapability, ...] = Field(
        alias="authorizedCapabilities",
        serialization_alias="authorizedCapabilities",
    )
    supported_stages: tuple[WorkStage, ...] = Field(
        alias="supportedStages",
        serialization_alias="supportedStages",
    )


class HealthResponse(WorkerWireModel):
    component: Literal["worker-gateway"] = "worker-gateway"
    status: Literal["live", "ready"]
