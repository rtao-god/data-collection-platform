"""Submit one sealed export to the backend Ingestion API as its exact rev-3 package.

This module is transport and preflight only. The package bytes and digests come from
`backend_ingestion`/`backend_canonical`, the sealed document comes from the export owner, and
nothing here re-assembles an export, re-maps a field or re-classifies an item. Everything it
checks is a refusal condition: it either submits the exact sealed package or performs no
network mutation at all.

Command identities are derived from the sealed package identity instead of being stored
locally, so a replay after a lost response reuses the same Idempotency-Key and the backend
returns its original result rather than creating a second batch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from collection_contracts.export_package import (
    ExportCandidateEntry,
    ExportPackage,
    canonical_export_package_json,
    decode_export_package,
    verify_export_package,
)
from collector_cli.backend_canonical import compute_digest
from collector_cli.backend_ingestion import (
    IngestionPackageDocuments,
    build_manifest,
    build_package_documents,
    payload_object_key,
    verify_round_trip,
)

# Redistribution rights a sealed entry must carry before it can leave the collection platform.
# `legal_status` is the collector's original grant; `usage_policy` is the right this entry's
# evidence actually carries. Neither is upgraded or translated here.
_REDISTRIBUTING_LEGAL_STATUSES = frozenset({"approved"})
_REDISTRIBUTING_USAGE_POLICIES = frozenset(
    {"publishable", "display_with_attribution", "owner_authorized"}
)
_ACCEPTING_DECISION_KINDS = frozenset({"accept_candidate"})

_PAYLOAD_CONTENT_TYPE = "application/json"
MAXIMUM_PAYLOAD_BYTES = 128 * 1024 * 1024

_TERMINAL_FAILURE_STATES = frozenset(
    {
        "IntegrityFailed",
        "ContractRejected",
        "BlockedByPolicy",
        "CommitFailed",
        "Expired",
        "Cancelled",
    }
)


class SubmissionRefused(Exception):
    """Raised when the sealed package may not be submitted as it stands."""

    def __init__(
        self, code: str, message: str, context: Mapping[str, object] | None = None
    ) -> None:
        self.code = code
        self.context = {key: str(value) for key, value in (context or {}).items()}
        super().__init__(message)


class SubmissionTransportError(Exception):
    """Raised when the backend transport failed or answered with an owner refusal."""

    def __init__(self, code: str, message: str, detail: Mapping[str, object]) -> None:
        self.code = code
        self.detail = {key: str(value) for key, value in detail.items()}
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SubmissionTarget:
    """Backend-owned target identities. They come from the producer registration, never here."""

    producer_identity: str
    producer_build: str
    target_site_key: str
    target_catalog_key: str
    target_configuration_revision_id: str


@dataclass(frozen=True, slots=True)
class SubmissionDocuments:
    """The exact registered manifest, its digest and the payload bytes to upload."""

    documents: IngestionPackageDocuments
    manifest: Mapping[str, Any]
    manifest_digest: str
    object_key: str

    @property
    def payload_bytes(self) -> bytes:
        return self.documents.payload_bytes

    @property
    def payload_digest(self) -> str:
        return self.documents.payload_digest


@dataclass(frozen=True, slots=True)
class SubmissionIdentities:
    """Deterministic command identities for one exact sealed package and target."""

    register: str
    prepare_upload: str
    complete_upload: str

    @classmethod
    def derive(
        cls,
        *,
        producer_identity: str,
        export_id: UUID,
        manifest_digest: str,
        target_configuration_revision_id: str,
    ) -> SubmissionIdentities:
        material = "|".join(
            (
                "collector-ingestion-submit",
                producer_identity,
                str(export_id),
                manifest_digest,
                target_configuration_revision_id,
            )
        )
        base = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return cls(
            register=f"collector-ingestion:{base}:register",
            prepare_upload=f"collector-ingestion:{base}:prepare-upload",
            complete_upload=f"collector-ingestion:{base}:complete-upload",
        )


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    """The durable identity a replay of the same sealed package reconstructs."""

    batch_id: UUID
    export_id: UUID
    export_digest: str
    manifest_digest: str
    payload_digest: str
    item_index_digest: str
    item_count: int
    item_keys: tuple[str, ...]
    object_key: str
    state: str
    aggregate_revision: int
    registration_replayed: bool
    upload_replayed: bool

    def as_document(self) -> Mapping[str, Any]:
        return {
            "batchId": str(self.batch_id),
            "exportId": str(self.export_id),
            "exportDigest": self.export_digest,
            "manifestDigest": self.manifest_digest,
            "payloadDigest": self.payload_digest,
            "itemIndexDigest": self.item_index_digest,
            "itemCount": self.item_count,
            "itemKeys": list(self.item_keys),
            "objectKey": self.object_key,
            "state": self.state,
            "aggregateRevision": self.aggregate_revision,
            "registrationReplayed": self.registration_replayed,
            "uploadReplayed": self.upload_replayed,
        }


class IngestionTransport(Protocol):
    """The narrow backend HTTP boundary this command needs."""

    def post_json(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        idempotency_key: str,
        expected_statuses: tuple[int, ...],
    ) -> Mapping[str, Any]: ...

    def get_json(self, path: str) -> Mapping[str, Any]: ...

    def put_object(
        self, url: str, *, content: bytes, content_type: str, content_digest: str
    ) -> None: ...


def read_sealed_export(path: Path) -> ExportPackage:
    """Read the exact sealed artifact and prove it is the canonical sealed document."""

    try:
        content = path.read_bytes()
    except OSError as exc:
        raise SubmissionRefused(
            "SUBMISSION_EXPORT_UNREADABLE",
            f"The sealed export artifact at {path} could not be read.",
            {"path": str(path), "causeType": type(exc).__name__},
        ) from exc
    try:
        package = decode_export_package(content)
        verify_export_package(package)
    except ValueError as exc:
        raise SubmissionRefused(
            "SUBMISSION_EXPORT_INVALID",
            "The artifact is not a verified export-package@2 document.",
            {"path": str(path), "detail": str(exc)},
        ) from exc
    if canonical_export_package_json(package).encode("utf-8") != content:
        raise SubmissionRefused(
            "SUBMISSION_EXPORT_NOT_CANONICAL",
            "The artifact is not the exact canonical sealed byte sequence.",
            {"path": str(path), "exportDigest": package.package_digest},
        )
    return package


def verify_selected_scope(package: ExportPackage, revisions: tuple[tuple[UUID, int], ...]) -> None:
    """The package must cover exactly the operated selection, without additions or omissions."""

    expected = set(revisions)
    actual = {(entry.candidate_id, entry.candidate_revision) for entry in package.entries}
    if not expected:
        raise SubmissionRefused(
            "SUBMISSION_SELECTION_EMPTY",
            "No candidate revision was selected, so nothing may be submitted.",
            {"exportDigest": package.package_digest},
        )
    if expected != actual:
        missing = sorted(f"{item[0]}:{item[1]}" for item in expected - actual)
        extra = sorted(f"{item[0]}:{item[1]}" for item in actual - expected)
        raise SubmissionRefused(
            "SUBMISSION_SELECTION_MISMATCH",
            "The sealed package does not cover exactly the selected candidate revisions.",
            {
                "exportDigest": package.package_digest,
                "missingFromPackage": ",".join(missing),
                "notSelected": ",".join(extra),
            },
        )


def verify_source_rights(package: ExportPackage) -> tuple[str, ...]:
    """Refuse any entry whose sealed rights do not authorize backend redistribution."""

    source_keys: set[str] = set()
    for entry in package.entries:
        _verify_entry_decision(entry)
        for policy in entry.source_policies:
            source_keys.add(policy.source_key)
            if policy.legal_status not in _REDISTRIBUTING_LEGAL_STATUSES:
                raise SubmissionRefused(
                    "SUBMISSION_SOURCE_RIGHTS_BLOCKED",
                    "A sealed source policy does not authorize redistribution.",
                    {
                        "candidateId": str(entry.candidate_id),
                        "candidateRevision": entry.candidate_revision,
                        "sourceKey": policy.source_key,
                        "legalStatus": policy.legal_status,
                        "usagePolicy": policy.usage_policy,
                    },
                )
            if policy.usage_policy not in _REDISTRIBUTING_USAGE_POLICIES:
                raise SubmissionRefused(
                    "SUBMISSION_SOURCE_RIGHTS_BLOCKED",
                    "A sealed source policy restricts this evidence to non-distributable use.",
                    {
                        "candidateId": str(entry.candidate_id),
                        "candidateRevision": entry.candidate_revision,
                        "sourceKey": policy.source_key,
                        "legalStatus": policy.legal_status,
                        "usagePolicy": policy.usage_policy,
                    },
                )
        for evidence in entry.field_evidence:
            if evidence.legal_status not in _REDISTRIBUTING_LEGAL_STATUSES or (
                evidence.usage_policy not in _REDISTRIBUTING_USAGE_POLICIES
            ):
                raise SubmissionRefused(
                    "SUBMISSION_FIELD_RIGHTS_BLOCKED",
                    "A sealed field evidence carries a right this submission cannot distribute.",
                    {
                        "candidateId": str(entry.candidate_id),
                        "fieldPath": evidence.field_path,
                        "sourceKey": evidence.source_key,
                        "legalStatus": evidence.legal_status,
                        "usagePolicy": evidence.usage_policy,
                    },
                )
    return tuple(sorted(source_keys))


def _verify_entry_decision(entry: ExportCandidateEntry) -> None:
    if not any(
        decision.decision_kind in _ACCEPTING_DECISION_KINDS for decision in entry.review_decisions
    ):
        raise SubmissionRefused(
            "SUBMISSION_CANDIDATE_NOT_ACCEPTED",
            "The candidate revision carries no accepting review decision.",
            {
                "candidateId": str(entry.candidate_id),
                "candidateRevision": entry.candidate_revision,
            },
        )


def build_submission(
    package: ExportPackage,
    *,
    target: SubmissionTarget,
    campaign_key: str,
    run_id: UUID,
) -> SubmissionDocuments:
    """The exact manifest, digest and payload bytes for one verified sealed package."""

    if package.campaign_key != campaign_key:
        raise SubmissionRefused(
            "SUBMISSION_CAMPAIGN_MISMATCH",
            "The sealed package was produced for a different campaign.",
            {
                "campaignKey": campaign_key,
                "sealedCampaignKey": package.campaign_key,
                "exportDigest": package.package_digest,
            },
        )
    if package.run_id != run_id:
        raise SubmissionRefused(
            "SUBMISSION_RUN_MISMATCH",
            "The sealed package belongs to a different run than the selected one.",
            {"runId": str(run_id), "sealedRunId": str(package.run_id)},
        )

    documents = build_package_documents(package)
    verify_round_trip(documents)
    manifest = build_manifest(
        package,
        documents,
        producer_identity=target.producer_identity,
        producer_build=target.producer_build,
        target_site_key=target.target_site_key,
        target_catalog_key=target.target_catalog_key,
        target_catalog_configuration_revision_id=target.target_configuration_revision_id,
    )
    object_key = payload_object_key(package)
    _verify_payload_admission(documents.payload_bytes, object_key)
    return SubmissionDocuments(
        documents=documents,
        manifest=manifest,
        manifest_digest=compute_digest(manifest),
        object_key=object_key,
    )


def _verify_payload_admission(content: bytes, object_key: str) -> None:
    """The same payload admission the backend registration enforces, checked before the call."""

    if not content or len(content) > MAXIMUM_PAYLOAD_BYTES:
        raise SubmissionRefused(
            "SUBMISSION_PAYLOAD_SIZE_INVALID",
            "The candidate payload size is outside the backend's accepted range.",
            {"sizeBytes": len(content), "maximumSizeBytes": MAXIMUM_PAYLOAD_BYTES},
        )
    if not object_key.startswith("ingestion/quarantine/"):
        raise SubmissionRefused(
            "SUBMISSION_OBJECT_KEY_INVALID",
            "The payload object key is not inside the Ingestion quarantine namespace.",
            {"objectKey": object_key},
        )


def submit(
    *,
    documents: SubmissionDocuments,
    package: ExportPackage,
    target: SubmissionTarget,
    transport: IngestionTransport,
) -> SubmissionReceipt:
    """Register, upload and complete one exact package, in that order, under stable identities."""

    identities = SubmissionIdentities.derive(
        producer_identity=target.producer_identity,
        export_id=package.run_id,
        manifest_digest=documents.manifest_digest,
        target_configuration_revision_id=target.target_configuration_revision_id,
    )
    registration = transport.post_json(
        "/api/ingestion/batches",
        {"manifest": documents.manifest, "manifestDigest": documents.manifest_digest},
        idempotency_key=identities.register,
        expected_statuses=(200, 201),
    )
    batch = _mapping(registration, "batch")
    batch_id = _uuid_text(batch, "id")
    _require_not_failed(batch)

    authorization = transport.post_json(
        f"/api/ingestion/batches/{batch_id}/upload-request",
        {"expectedAggregateRevision": _revision(batch)},
        idempotency_key=identities.prepare_upload,
        expected_statuses=(200,),
    )
    if authorization.get("objectKey") != documents.object_key:
        raise SubmissionRefused(
            "SUBMISSION_UPLOAD_KEY_MISMATCH",
            "The backend authorized an object key that is not the registered payload key.",
            {
                "expectedObjectKey": documents.object_key,
                "authorizedObjectKey": _optional_text(authorization.get("objectKey")),
            },
        )
    authorized_batch = _mapping(authorization, "batch")
    content_type = _optional_text(authorization.get("contentType")) or _PAYLOAD_CONTENT_TYPE
    upload_uri = _text(authorization, "uploadUri")
    transport.put_object(
        upload_uri,
        content=documents.payload_bytes,
        content_type=content_type,
        content_digest=documents.payload_digest,
    )

    completed = transport.post_json(
        f"/api/ingestion/batches/{batch_id}/upload-complete",
        {"expectedAggregateRevision": _revision(authorized_batch)},
        idempotency_key=identities.complete_upload,
        expected_statuses=(200,),
    )
    completed_batch = _mapping(completed, "batch")
    _require_not_failed(completed_batch)
    return SubmissionReceipt(
        batch_id=batch_id,
        export_id=package.run_id,
        export_digest=package.package_digest,
        manifest_digest=documents.manifest_digest,
        payload_digest=documents.payload_digest,
        item_index_digest=documents.documents.item_index_digest,
        item_count=len(documents.documents.items),
        item_keys=tuple(str(item["itemKey"]) for item in documents.documents.items),
        object_key=documents.object_key,
        state=_text(completed_batch, "state"),
        aggregate_revision=_revision(completed_batch),
        registration_replayed=_optional_bool(registration.get("replayed")),
        upload_replayed=_optional_bool(authorization.get("replayed"))
        or _optional_bool(completed.get("replayed")),
    )


def read_batch(*, transport: IngestionTransport, batch_id: UUID) -> Mapping[str, Any]:
    """Read-only durable batch receipt; never advances state."""

    return transport.get_json(f"/api/ingestion/batches/{batch_id}")


def _mapping(document: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = document.get(key)
    if not isinstance(value, Mapping):
        raise SubmissionTransportError(
            "SUBMISSION_RESPONSE_INVALID",
            f"The backend response carried no '{key}' document.",
            {"field": key},
        )
    return value


def _text(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise SubmissionTransportError(
            "SUBMISSION_RESPONSE_INVALID",
            f"The backend response carried no '{key}' value.",
            {"field": key},
        )
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_bool(value: object) -> bool:
    return value is True


def _revision(document: Mapping[str, Any]) -> int:
    value = document.get("aggregateRevision")
    if not isinstance(value, int) or value < 0:
        raise SubmissionTransportError(
            "SUBMISSION_RESPONSE_INVALID",
            "The backend batch carries no monotonically increasing aggregate revision.",
            {"field": "aggregateRevision"},
        )
    return value


def _uuid_text(document: Mapping[str, Any], key: str) -> UUID:
    value = document.get(key)
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise SubmissionTransportError(
            "SUBMISSION_RESPONSE_INVALID",
            f"The backend response carried no valid '{key}' identifier.",
            {"field": key},
        ) from exc


def _require_not_failed(batch: Mapping[str, Any]) -> None:
    state = _optional_text(batch.get("state"))
    if state in _TERMINAL_FAILURE_STATES:
        raise SubmissionTransportError(
            "SUBMISSION_BATCH_FAILED",
            "The backend batch reached a terminal failure state.",
            {
                "batchId": _optional_text(batch.get("id")) or "",
                "state": state or "",
                "failureCode": _optional_text(batch.get("failureCode")) or "",
            },
        )


class ClientCredentialsTokenProvider:
    """Collects one workload token from the configured OIDC client-credentials endpoint."""

    def __init__(
        self,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        scope: str | None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not token_endpoint.startswith(("http://", "https://")):
            raise SubmissionRefused(
                "SUBMISSION_TOKEN_ENDPOINT_INVALID",
                "The token endpoint must be an absolute HTTP(S) URL.",
                {"tokenEndpoint": token_endpoint},
            )
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._timeout_seconds = timeout_seconds
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def token(self) -> str:
        if (
            self._token is not None
            and self._expires_at is not None
            and datetime.now(UTC) + timedelta(seconds=30) < self._expires_at
        ):
            return self._token
        body = {"grant_type": "client_credentials"}
        if self._scope:
            body["scope"] = self._scope
        request = urllib.request.Request(  # noqa: S310 - endpoint scheme is validated
            self._token_endpoint,
            data=urllib.parse.urlencode(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                # The client secret is sent as the basic-auth credential so it never appears
                # in the request body, logs or transport diagnostics.
                "Authorization": _basic_authorization(self._client_id, self._client_secret),
            },
        )
        payload = _send(request, timeout_seconds=self._timeout_seconds, operation="token")
        token = _optional_text(payload.get("access_token"))
        expires_in = payload.get("expires_in")
        if not token:
            raise SubmissionTransportError(
                "SUBMISSION_TOKEN_MISSING",
                "The token endpoint returned no access token.",
                {"tokenEndpoint": self._token_endpoint},
            )
        lifetime = expires_in if isinstance(expires_in, int) and expires_in > 0 else 60
        self._token = token
        self._expires_at = datetime.now(UTC) + timedelta(seconds=lifetime)
        return token


def _basic_authorization(client_id: str, client_secret: str) -> str:
    raw = f"{urllib.parse.quote(client_id)}:{urllib.parse.quote(client_secret)}"
    return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")


class HttpIngestionTransport:
    """Authenticated JSON transport for the backend Ingestion API and its upload URLs."""

    def __init__(
        self,
        *,
        base_url: str,
        tokens: ClientCredentialsTokenProvider,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise SubmissionRefused(
                "SUBMISSION_API_URL_INVALID",
                "The Ingestion API base URL must be an absolute HTTP(S) URL.",
                {"baseUrl": base_url},
            )
        self._base_url = base_url.rstrip("/")
        self._tokens = tokens
        self._timeout_seconds = timeout_seconds

    def post_json(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        idempotency_key: str,
        expected_statuses: tuple[int, ...],
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - base URL scheme is validated
            f"{self._base_url}{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Idempotency-Key": idempotency_key,
                "Authorization": f"Bearer {self._tokens.token()}",
            },
        )
        payload = _send(
            request, timeout_seconds=self._timeout_seconds, operation=path.split("?")[0]
        )
        status = payload.get("__status", 0)
        if isinstance(status, int) and expected_statuses and status not in expected_statuses:
            raise SubmissionTransportError(
                "SUBMISSION_STATUS_UNEXPECTED",
                "The backend answered with an unexpected status for this command.",
                {"path": path, "status": status},
            )
        return {key: value for key, value in payload.items() if key != "__status"}

    def get_json(self, path: str) -> Mapping[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - base URL scheme is validated
            f"{self._base_url}{path}",
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._tokens.token()}",
            },
        )
        payload = _send(
            request, timeout_seconds=self._timeout_seconds, operation=path.split("?")[0]
        )
        return {key: value for key, value in payload.items() if key != "__status"}

    def put_object(
        self, url: str, *, content: bytes, content_type: str, content_digest: str
    ) -> None:
        request = urllib.request.Request(  # noqa: S310 - upload URL is backend-authorized
            url,
            data=content,
            method="PUT",
            headers={
                "Content-Type": content_type,
                # The service verifies this exact owner digest from the stored object's
                # metadata, so it is part of the upload rather than a client-side claim.
                "x-amz-meta-sha256": content_digest,
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - backend-authorized upload URL
                request, timeout=self._timeout_seconds
            ) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise SubmissionTransportError(
                "SUBMISSION_UPLOAD_FAILED",
                "The authorized payload upload was refused by the object store.",
                {"status": exc.code, "reason": exc.reason},
            ) from exc
        except urllib.error.URLError as exc:
            raise SubmissionTransportError(
                "SUBMISSION_UPLOAD_UNREACHABLE",
                "The authorized payload upload could not reach the object store.",
                {"causeType": type(exc.reason).__name__},
            ) from exc


def _send(
    request: urllib.request.Request, *, timeout_seconds: float, operation: str
) -> Mapping[str, Any]:
    try:
        with urllib.request.urlopen(  # noqa: S310 - caller-validated absolute URL
            request, timeout=timeout_seconds
        ) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc.read())
        detail["status"] = exc.code
        detail["operation"] = operation
        code = _optional_text(detail.get("code")) or "SUBMISSION_HTTP_ERROR"
        raise SubmissionTransportError(code, "The backend refused the request.", detail) from exc
    except urllib.error.URLError as exc:
        raise SubmissionTransportError(
            "SUBMISSION_HTTP_UNREACHABLE",
            "The backend could not be reached.",
            {"operation": operation, "causeType": type(exc.reason).__name__},
        ) from exc
    document = _decode(body)
    document["__status"] = status
    return document


def _decode(body: bytes) -> dict[str, Any]:
    try:
        document = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubmissionTransportError(
            "SUBMISSION_HTTP_BODY_INVALID",
            "The backend answered with a body that is not a JSON document.",
            {"causeType": type(exc).__name__},
        ) from exc
    if not isinstance(document, dict):
        raise SubmissionTransportError(
            "SUBMISSION_HTTP_BODY_INVALID",
            "The backend answered with a JSON document that is not an object.",
            {},
        )
    return document


def _error_detail(body: bytes) -> dict[str, object]:
    try:
        document = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


__all__ = [
    "ClientCredentialsTokenProvider",
    "HttpIngestionTransport",
    "IngestionTransport",
    "SubmissionDocuments",
    "SubmissionIdentities",
    "SubmissionReceipt",
    "SubmissionRefused",
    "SubmissionTarget",
    "SubmissionTransportError",
    "build_submission",
    "read_batch",
    "read_sealed_export",
    "submit",
    "verify_selected_scope",
    "verify_source_rights",
]
