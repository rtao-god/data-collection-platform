from __future__ import annotations

import threading
from dataclasses import dataclass
from hashlib import sha256
from time import sleep
from typing import Literal, Protocol
from urllib.parse import urljoin
from uuid import UUID

from official_http import (
    DiscoveredResource,
    HttpAcquisitionManifest,
    HttpFetchResult,
    OfficialHttpError,
    OfficialHttpRequest,
    ScrapyChildFetcher,
    canonical_origin,
    decode_http_request,
    evaluate_robots,
    normalize_http_url,
    plan_html,
    plan_sitemap,
    selected_header,
)
from source_connector_sdk import (
    LeaseArtifact,
    WebsiteRouteContext,
    WorkerLease,
    WorkFailureKind,
    decode_manual_record_lineage,
    decode_website_route_context,
)

_OUTPUT_CONTRACT = "official-http-acquisition@1"


class HttpGateway(Protocol):
    def acquire(
        self,
        *,
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> WorkerLease | None: ...

    def heartbeat(
        self,
        lease: WorkerLease,
        *,
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> WorkerLease: ...

    def read_request(self, lease: WorkerLease, *, maximum_bytes: int) -> bytes: ...

    def read_website_route_context(
        self,
        lease: WorkerLease,
        *,
        maximum_bytes: int,
    ) -> bytes | None: ...

    def read_manual_record_lineage(
        self,
        lease: WorkerLease,
        *,
        maximum_bytes: int,
    ) -> tuple[LeaseArtifact, bytes] | None: ...

    def previous_artifact_id(self, lease: WorkerLease) -> UUID | None: ...

    def robots_artifact_id(self, lease: WorkerLease) -> UUID | None: ...

    def read_robots_manifest(self, lease: WorkerLease, *, maximum_bytes: int) -> bytes | None: ...

    def publish(
        self,
        lease: WorkerLease,
        *,
        manifest: HttpAcquisitionManifest,
        raw_body: bytes | None,
        raw_content_type: str,
    ) -> None: ...

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: WorkFailureKind,
        error_code: str,
        message: str,
        retry_after_seconds: int | None = None,
    ) -> None: ...


class HttpFetcher(Protocol):
    def fetch(self, request: OfficialHttpRequest) -> HttpFetchResult: ...


@dataclass(frozen=True, slots=True)
class HttpWorkerPolicy:
    lease_duration_seconds: int = 300
    heartbeat_interval_seconds: int = 60
    poll_interval_seconds: float = 5.0
    maximum_request_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if not 30 <= self.lease_duration_seconds <= 3_600:
            raise ValueError("HTTP worker lease duration is outside the supported range")
        if not 5 <= self.heartbeat_interval_seconds < self.lease_duration_seconds:
            raise ValueError("HTTP worker heartbeat interval is invalid")
        if not 0.1 <= self.poll_interval_seconds <= 300:
            raise ValueError("HTTP worker poll interval is outside the supported range")
        if not 1_024 <= self.maximum_request_bytes <= 4 * 1024 * 1024:
            raise ValueError("HTTP request artifact byte limit is outside the supported range")


class HttpWorker:
    def __init__(
        self,
        gateway: HttpGateway,
        fetcher: HttpFetcher | None = None,
        *,
        policy: HttpWorkerPolicy | None = None,
    ) -> None:
        self._gateway = gateway
        self._fetcher = fetcher or ScrapyChildFetcher()
        self._policy = policy or HttpWorkerPolicy()

    def run_once(self) -> bool:
        lease = self._gateway.acquire(
            lease_duration_seconds=self._policy.lease_duration_seconds,
            heartbeat_interval_seconds=self._policy.heartbeat_interval_seconds,
        )
        if lease is None:
            return False
        with _HeartbeatPump(self._gateway, lease, policy=self._policy) as heartbeat:
            try:
                request, route_context = self._validate_and_read(heartbeat.lease)
                result = self._fetcher.fetch(request)
                manifest, raw_body, content_type = _build_manifest(
                    request,
                    result,
                    previous_artifact_id=self._gateway.previous_artifact_id(heartbeat.lease),
                    robots_target_url=(
                        str(route_context.source_url) if route_context is not None else None
                    ),
                )
                heartbeat.raise_if_failed()
                self._gateway.publish(
                    heartbeat.lease,
                    manifest=manifest,
                    raw_body=raw_body,
                    raw_content_type=content_type,
                )
            except OfficialHttpError as exc:
                heartbeat.raise_if_failed()
                self._gateway.fail(
                    heartbeat.lease,
                    failure_kind=exc.kind,
                    error_code=exc.code,
                    message=str(exc),
                    retry_after_seconds=exc.retry_after_seconds,
                )
            except ValueError:
                heartbeat.raise_if_failed()
                self._gateway.fail(
                    heartbeat.lease,
                    failure_kind="contract_invalid",
                    error_code="OFFICIAL_HTTP_WORK_INPUT_INVALID",
                    message="The official HTTP work input does not satisfy its owner contract.",
                )
            return True

    def run_forever(self) -> None:
        while True:
            if not self.run_once():
                sleep(self._policy.poll_interval_seconds)

    def _validate_and_read(
        self,
        lease: WorkerLease,
    ) -> tuple[OfficialHttpRequest, WebsiteRouteContext | None]:
        if lease.stage != "acquisition" or lease.capability != "http_fetch":
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_LEASE_CAPABILITY_INVALID",
                message="The lease is not an official HTTP acquisition work unit.",
            )
        if lease.expected_output_contract != _OUTPUT_CONTRACT:
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_OUTPUT_CONTRACT_UNSUPPORTED",
                message="The lease requests an unsupported HTTP output contract.",
            )
        if lease.source_permit is None:
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_SOURCE_PERMIT_MISSING",
                message="Official HTTP acquisition requires a source permit.",
                kind="policy_blocked",
            )
        body = self._gateway.read_request(
            lease,
            maximum_bytes=self._policy.maximum_request_bytes,
        )
        request = decode_http_request(body, maximum_bytes=self._policy.maximum_request_bytes)
        route_context_bytes = self._gateway.read_website_route_context(
            lease,
            maximum_bytes=self._policy.maximum_request_bytes,
        )
        route_context = (
            decode_website_route_context(
                route_context_bytes,
                maximum_bytes=self._policy.maximum_request_bytes,
            )
            if route_context_bytes is not None
            else None
        )
        if route_context is not None:
            _validate_manual_record_lineage(
                self._gateway,
                lease,
                route_context,
                maximum_bytes=self._policy.maximum_request_bytes,
            )
            self._validate_website_route_context(request, body, route_context)
        if (
            request.source_key != lease.source_permit.source_key
            or request.source_policy_digest != lease.source_permit.policy_digest
        ):
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_SOURCE_PERMIT_MISMATCH",
                message="The HTTP request source or policy does not match its source permit.",
                kind="policy_blocked",
            )
        robots_artifact = self._gateway.robots_artifact_id(lease)
        if request.robots_artifact_id != robots_artifact:
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_ROBOTS_ARTIFACT_MISMATCH",
                message="The HTTP request does not match the leased robots decision artifact.",
                kind="policy_blocked",
            )
        previous = self._gateway.previous_artifact_id(lease)
        if request.request_kind == "page" and (
            route_context is not None or request.robots_manifest_digest is not None
        ):
            if route_context is None or request.robots_manifest_digest is None:
                raise OfficialHttpError(
                    code="OFFICIAL_HTTP_ROBOTS_EVIDENCE_MISSING",
                    message=(
                        "Page work requires its exact leased robots manifest and route context."
                    ),
                    kind="policy_blocked",
                )
            robots_manifest_bytes = self._gateway.read_robots_manifest(
                lease, maximum_bytes=2 * 1024 * 1024
            )
            if robots_manifest_bytes is None:
                raise OfficialHttpError(
                    code="OFFICIAL_HTTP_ROBOTS_EVIDENCE_MISSING",
                    message="The leased robots manifest could not be read.",
                    kind="policy_blocked",
                )
            try:
                robots_manifest = HttpAcquisitionManifest.model_validate_json(
                    robots_manifest_bytes, strict=True
                )
            except ValueError as exc:
                raise OfficialHttpError(
                    code="OFFICIAL_HTTP_ROBOTS_EVIDENCE_INVALID",
                    message="The leased robots manifest violates its producer contract.",
                    kind="policy_blocked",
                ) from exc
            if (
                f"sha256:{sha256(robots_manifest_bytes).hexdigest()}"
                != request.robots_manifest_digest
                or robots_manifest.request_kind != "robots"
                or robots_manifest.outcome not in {"fetched", "empty"}
                or robots_manifest.robots_target_url != str(route_context.source_url)
                or robots_manifest.robots_allowed is not True
                or robots_manifest.robots_decision_digest != request.robots_decision_digest
                or robots_manifest.source_key != request.source_key
                or robots_manifest.source_policy_digest != request.source_policy_digest
            ):
                raise OfficialHttpError(
                    code="OFFICIAL_HTTP_ROBOTS_EVIDENCE_MISMATCH",
                    message=(
                        "The leased robots manifest does not bind the exact page seed, "
                        "source policy, and decision digest."
                    ),
                    kind="policy_blocked",
                )
        if request.prior_artifact_id != previous:
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_PRIOR_ARTIFACT_MISMATCH",
                message="The HTTP conditional request does not match the leased prior artifact.",
            )
        return request, route_context

    @staticmethod
    def _validate_website_route_context(
        request: OfficialHttpRequest,
        request_payload: bytes,
        context: WebsiteRouteContext,
    ) -> None:
        base_identity_matches = (
            context.request_id == request.request_id
            and context.request_digest == f"sha256:{sha256(request_payload).hexdigest()}"
            and context.source_key == request.source_key
            and context.source_policy_digest == request.source_policy_digest
            and canonical_origin(str(context.source_url)) == request.allowed_origin
        )
        if request.request_kind == "robots":
            request_kind_matches = request.url == f"{request.allowed_origin}/robots.txt"
        else:
            expected_interests = tuple(
                (item.key, item.tokens, item.priority) for item in context.page_interests
            )
            actual_interests = tuple(
                (item.key, item.tokens, item.priority) for item in request.page_interests
            )
            request_kind_matches = (
                request.url == str(context.source_url)
                and request.robots_allowed is True
                and request.robots_artifact_id is not None
                and request.robots_decision_digest is not None
                and expected_interests == actual_interests
            )
        context_matches = base_identity_matches and request_kind_matches
        if not context_matches:
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_WEBSITE_ROUTE_CONTEXT_MISMATCH",
                message=(
                    "The website route context does not bind to this exact robots request, "
                    "source policy, and target origin."
                ),
                kind="policy_blocked",
            )


def _build_manifest(
    request: OfficialHttpRequest,
    result: HttpFetchResult,
    *,
    previous_artifact_id: UUID | None,
    robots_target_url: str | None = None,
) -> tuple[HttpAcquisitionManifest, bytes | None, str]:
    status = result.status_code
    content_type = (
        (selected_header(result, "content-type") or "application/octet-stream")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    if not content_type or "/" not in content_type:
        content_type = "application/octet-stream"
    discovered: tuple[DiscoveredResource, ...] = ()
    robots_allowed: bool | None = request.robots_allowed
    robots_decision_digest: str | None = None
    raw_digest = None
    reused_id = None
    reused_digest = None
    redirect_location = None
    raw_body: bytes | None = None
    outcome: Literal["fetched", "empty", "unchanged", "redirect", "not_found"]
    if 200 <= status <= 299:
        raw_body = result.body or None
        raw_digest = f"sha256:{sha256(result.body).hexdigest()}" if result.body else None
        if request.request_kind == "robots":
            evaluation = evaluate_robots(
                result.body,
                request=request,
                target_url=robots_target_url,
            )
            robots_allowed = evaluation.allowed
            robots_decision_digest = evaluation.decision_digest
            discovered = tuple(
                DiscoveredResource(
                    url=url,
                    resource_kind="sitemap",
                    interest_key=None,
                    score=1_000,
                )
                for url in evaluation.sitemap_urls
            )
        elif request.request_kind == "sitemap":
            discovered = plan_sitemap(result.body, request=request).resources
        else:
            discovered = plan_html(result.body, request=request).resources
        outcome = "fetched" if result.body else "empty"
    elif status == 304:
        if request.prior_artifact_id is None or request.prior_content_digest is None:
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_304_WITHOUT_PRIOR_ARTIFACT",
                message="HTTP 304 cannot be accepted without the exact prior artifact.",
            )
        if previous_artifact_id != request.prior_artifact_id:
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_304_ARTIFACT_MISMATCH",
                message="HTTP 304 refers to a prior artifact outside the lease.",
            )
        reused_id = request.prior_artifact_id
        reused_digest = request.prior_content_digest
        outcome = "unchanged"
    elif status in {404, 410}:
        outcome = "not_found"
    elif 300 <= status <= 399:
        location = selected_header(result, "location")
        if location is None:
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_REDIRECT_LOCATION_MISSING",
                message="The redirect response is missing its Location header.",
                kind="permanent",
            )
        redirect_location = normalize_http_url(
            urljoin(result.final_url, location),
            tracking_parameters=request.tracking_query_parameters,
        )
        if canonical_origin(redirect_location) != request.allowed_origin:
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_REDIRECT_ORIGIN_FORBIDDEN",
                message="The redirect target is outside the approved origin.",
                kind="policy_blocked",
            )
        outcome = "redirect"
    elif status == 403:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_FORBIDDEN",
            message="The source returned HTTP 403; browser escalation is not permitted.",
            kind="policy_blocked",
        )
    elif status == 429:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_RATE_LIMITED",
            message="The source returned HTTP 429.",
            kind="transient",
            retry_after_seconds=_retry_after_seconds(selected_header(result, "retry-after")),
        )
    elif 500 <= status <= 599:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_SERVER_UNAVAILABLE",
            message="The source returned a transient server failure.",
            kind="transient",
        )
    else:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_STATUS_UNSUPPORTED",
            message="The source returned a permanent unsupported HTTP status.",
            kind="permanent",
            context={"statusCode": status},
        )
    manifest = HttpAcquisitionManifest(
        request_id=request.request_id,
        source_key=request.source_key,
        source_policy_digest=request.source_policy_digest,
        request_kind=request.request_kind,
        requested_url=request.url,
        final_url=result.final_url,
        outcome=outcome,
        status_code=status,
        response_headers=result.headers,
        remote_ip_address=result.remote_ip_address,
        encoded_size_bytes=result.encoded_size_bytes,
        decoded_size_bytes=result.decoded_size_bytes,
        observed_at_utc=result.observed_at_utc,
        raw_artifact_digest=raw_digest,
        reused_artifact_id=reused_id,
        reused_content_digest=reused_digest,
        redirect_location=redirect_location,
        discovered_resources=discovered,
        robots_allowed=robots_allowed,
        robots_artifact_id=request.robots_artifact_id,
        robots_decision_digest=(robots_decision_digest or request.robots_decision_digest),
        robots_target_url=robots_target_url,
    )
    return manifest, raw_body, content_type


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    if value.isdigit():
        return min(int(value), 86_400)
    return None


def _validate_manual_record_lineage(
    gateway: HttpGateway,
    lease: WorkerLease,
    route_context: WebsiteRouteContext,
    *,
    maximum_bytes: int,
) -> None:
    binding = gateway.read_manual_record_lineage(lease, maximum_bytes=maximum_bytes)
    if binding is None:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_MANUAL_RECORD_LINEAGE_MISSING",
            message="Routed HTTP work requires its exact manual-record lineage artifact.",
        )
    artifact, content = binding
    if (
        artifact.role != "manual_record_lineage"
        or artifact.content_digest is None
        or artifact.size_bytes is None
        or artifact.content_type != "application/vnd.collection.manual-record-lineage+json"
        or len(content) != artifact.size_bytes
        or f"sha256:{sha256(content).hexdigest()}" != artifact.content_digest
    ):
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_MANUAL_RECORD_LINEAGE_INVALID",
            message="Transferred lineage bytes differ from their exact leased artifact identity.",
        )
    try:
        lineage = decode_manual_record_lineage(content)
    except ValueError as exc:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_MANUAL_RECORD_LINEAGE_INVALID",
            message="Transferred manual-record lineage violates its canonical contract.",
        ) from exc
    if lineage.manual_record_content_digest != route_context.record_digest:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_MANUAL_RECORD_LINEAGE_MISMATCH",
            message="Route context is not bound to the transferred manual-record lineage.",
        )


class _HeartbeatPump:
    def __init__(
        self,
        gateway: HttpGateway,
        lease: WorkerLease,
        *,
        policy: HttpWorkerPolicy,
    ) -> None:
        self._gateway = gateway
        self._lease = lease
        self._policy = policy
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"http-heartbeat-{lease.work_id}",
            daemon=True,
        )

    @property
    def lease(self) -> WorkerLease:
        with self._lock:
            return self._lease

    def __enter__(self) -> _HeartbeatPump:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        self._thread.join(timeout=self._policy.heartbeat_interval_seconds + 1)
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("HTTP worker lost its lease heartbeat") from self._failure

    def _run(self) -> None:
        while not self._stop.wait(self._policy.heartbeat_interval_seconds):
            try:
                renewed = self._gateway.heartbeat(
                    self.lease,
                    lease_duration_seconds=self._policy.lease_duration_seconds,
                    heartbeat_interval_seconds=self._policy.heartbeat_interval_seconds,
                )
            except BaseException as exc:
                self._failure = exc
                self._stop.set()
                return
            with self._lock:
                self._lease = renewed
