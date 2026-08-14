from __future__ import annotations

import json
from hashlib import sha256
from uuid import UUID

from official_http import HttpAcquisitionManifest
from source_connector_sdk import SourceWorkerGateway, WorkerLease, WorkFailureKind

_REQUEST_ROLE = "http_request"
_PREVIOUS_RAW_ROLE = "previous_raw_artifact"
_ROBOTS_DECISION_ROLE = "robots_decision"
_RAW_ROLE = "http_raw_response"
_MANIFEST_ROLE = "http_acquisition_manifest"
_OUTPUT_CONTRACT = "official-http-acquisition@1"


class SdkHttpWorkerGateway:
    """Maps official HTTP acquisition to the canonical Worker Gateway protocol."""

    def __init__(self, client: SourceWorkerGateway) -> None:
        self._client = client
        self._build_identity: str | None = None

    def register(self, *, build_identity: str) -> None:
        self._client.register(
            build_identity=build_identity,
            capabilities={"http_fetch"},
            supported_output_contracts={_OUTPUT_CONTRACT},
            max_concurrency=1,
            resource_profile="official-http",
        )
        self._build_identity = build_identity

    def acquire(
        self,
        *,
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> WorkerLease | None:
        return self._client.acquire_lease(
            capability="http_fetch",
            lease_duration_seconds=lease_duration_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )

    def heartbeat(
        self,
        lease: WorkerLease,
        *,
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> WorkerLease:
        return self._client.heartbeat(
            lease,
            lease_duration_seconds=lease_duration_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )

    def read_request(self, lease: WorkerLease, *, maximum_bytes: int) -> bytes:
        artifact = lease.artifact(_REQUEST_ROLE)
        return self._client.read_artifact(
            lease,
            artifact_id=artifact.artifact_id,
            maximum_bytes=maximum_bytes,
        )

    def previous_artifact_id(self, lease: WorkerLease) -> UUID | None:
        return _optional_artifact_id(lease, _PREVIOUS_RAW_ROLE)

    def robots_artifact_id(self, lease: WorkerLease) -> UUID | None:
        return _optional_artifact_id(lease, _ROBOTS_DECISION_ROLE)

    def publish(
        self,
        lease: WorkerLease,
        *,
        manifest: HttpAcquisitionManifest,
        raw_body: bytes | None,
        raw_content_type: str,
    ) -> None:
        outputs: list[tuple[UUID, str]] = []
        raw_digest = manifest.raw_artifact_digest or manifest.reused_content_digest
        if raw_body is not None:
            raw_upload = self._client.upload_bytes(
                lease,
                content=raw_body,
                artifact_kind="raw_artifact",
                content_type=raw_content_type,
            )
            if raw_upload.content_digest != manifest.raw_artifact_digest:
                raise ValueError("HTTP manifest raw digest does not match uploaded content")
            raw_digest = raw_upload.content_digest
            outputs.append((raw_upload.upload_id, _RAW_ROLE))
        manifest_upload = self._client.upload_bytes(
            lease,
            content=manifest.to_bytes(),
            artifact_kind="diagnostic_artifact",
            content_type="application/vnd.collection.official-http-acquisition+json",
        )
        outputs.append((manifest_upload.upload_id, _MANIFEST_ROLE))
        output_digest = _result_digest(
            output_contract=lease.expected_output_contract,
            manifest_digest=manifest_upload.content_digest,
            raw_or_reused_digest=raw_digest,
        )
        self._client.complete(
            lease,
            output_contract=lease.expected_output_contract,
            output_digest=output_digest,
            worker_build_identity=self._required_build_identity(),
            output_artifacts=tuple(outputs),
        )

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: WorkFailureKind,
        error_code: str,
        message: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        self._client.fail(
            lease,
            failure_kind=failure_kind,
            code=error_code,
            owner="OfficialHttpWorker.Acquisition",
            message=message,
            required_action=_required_action(failure_kind, retry_after_seconds),
            worker_build_identity=self._required_build_identity(),
        )

    def _required_build_identity(self) -> str:
        if self._build_identity is None:
            raise RuntimeError("HTTP worker must register before processing work")
        return self._build_identity


def _result_digest(
    *,
    output_contract: str,
    manifest_digest: str,
    raw_or_reused_digest: str | None,
) -> str:
    payload = json.dumps(
        {
            "contract": output_contract,
            "manifestDigest": manifest_digest,
            "rawOrReusedDigest": raw_or_reused_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{sha256(payload).hexdigest()}"


def _required_action(kind: WorkFailureKind, retry_after_seconds: int | None) -> str:
    if kind == "transient":
        suffix = (
            f" Do not retry before {retry_after_seconds} seconds have elapsed."
            if retry_after_seconds is not None
            else ""
        )
        return "Retry the exact HTTP work unit after the source dependency recovers." + suffix
    if kind == "policy_blocked":
        return "Review the source policy or robots decision before scheduling replacement work."
    return (
        "Correct the official HTTP request or connector contract before scheduling "
        "replacement work."
    )


def _optional_artifact_id(lease: WorkerLease, role: str) -> UUID | None:
    matches = tuple(
        artifact.artifact_id for artifact in lease.input_artifacts if artifact.role == role
    )
    if len(matches) > 1:
        raise ValueError(f"HTTP lease contains duplicate artifacts for role {role}")
    return matches[0] if matches else None
