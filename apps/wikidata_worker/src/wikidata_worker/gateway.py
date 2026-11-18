from __future__ import annotations

import json
from hashlib import sha256

from source_connector_sdk import (
    SourceWorkerGateway,
    WorkerLease,
    WorkFailureKind,
)

# One acquisition result is two immutable artifacts: the exact response body and the
# sealed envelope that describes which entities it answered. The catalog re-derives the
# result digest from both, so the roles and the digest shape are fixed here.
_RAW_RESPONSE_CONTENT_TYPE = "application/json"
_ENVELOPE_CONTENT_TYPE = "application/vnd.collection.wikidata-entity-result+json"
_RAW_RESPONSE_ROLE = "wikidata_raw_response"
_ENVELOPE_ROLE = "wikidata_entity_result"
_OUTPUT_CONTRACTS = frozenset({"wikidata-entity-result@2"})


class SdkWikidataWorkerGateway:
    """Maps Wikidata entity acquisition to the canonical Worker Gateway protocol."""

    def __init__(self, client: SourceWorkerGateway) -> None:
        self._client = client
        self._build_identity: str | None = None

    def register(self, *, build_identity: str) -> None:
        self._client.register(
            build_identity=build_identity,
            capabilities={"wikidata_entity_query"},
            supported_output_contracts=_OUTPUT_CONTRACTS,
            max_concurrency=1,
            resource_profile="wikidata-entity-query",
        )
        self._build_identity = build_identity

    def acquire(
        self,
        *,
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> WorkerLease | None:
        return self._client.acquire_lease(
            capability="wikidata_entity_query",
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

    def read_artifact(
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

    def publish_result(
        self,
        lease: WorkerLease,
        *,
        raw_response: bytes,
        result_envelope: bytes,
    ) -> None:
        raw_upload = self._client.upload_bytes(
            lease,
            content=raw_response,
            artifact_kind="raw_artifact",
            content_type=_RAW_RESPONSE_CONTENT_TYPE,
        )
        envelope_upload = self._client.upload_bytes(
            lease,
            content=result_envelope,
            artifact_kind="diagnostic_artifact",
            content_type=_ENVELOPE_CONTENT_TYPE,
        )
        output_digest = _result_digest(
            output_contract=lease.expected_output_contract,
            raw_digest=raw_upload.content_digest,
            envelope_digest=envelope_upload.content_digest,
        )
        self._client.complete(
            lease,
            output_contract=lease.expected_output_contract,
            output_digest=output_digest,
            worker_build_identity=self._required_build_identity(),
            output_artifacts=(
                (raw_upload.upload_id, _RAW_RESPONSE_ROLE),
                (envelope_upload.upload_id, _ENVELOPE_ROLE),
            ),
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
        required_action = _required_action(failure_kind, retry_after_seconds)
        self._client.fail(
            lease,
            failure_kind=failure_kind,
            code=error_code,
            owner="WikidataWorker.EntityQuery",
            message=message,
            required_action=required_action,
            worker_build_identity=self._required_build_identity(),
        )

    def _required_build_identity(self) -> str:
        if self._build_identity is None:
            raise RuntimeError("Wikidata worker must register before processing work")
        return self._build_identity


def _result_digest(
    *,
    output_contract: str,
    raw_digest: str,
    envelope_digest: str,
) -> str:
    payload = json.dumps(
        {
            "contract": output_contract,
            "envelopeDigest": envelope_digest,
            "rawResponseDigest": raw_digest,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(payload).hexdigest()}"


def _required_action(
    failure_kind: WorkFailureKind,
    retry_after_seconds: int | None,
) -> str:
    if failure_kind == "transient":
        suffix = (
            f" Do not retry before {retry_after_seconds} seconds have elapsed."
            if retry_after_seconds is not None
            else ""
        )
        return "Restore the approved Wikidata endpoint and retry the exact work unit." + suffix
    if failure_kind == "policy_blocked":
        return "Review and reactivate the exact Wikidata source policy before scheduling new work."
    return "Correct the Wikidata work input or connector contract before creating work."
