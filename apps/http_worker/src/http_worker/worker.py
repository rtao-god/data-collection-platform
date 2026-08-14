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
from source_connector_sdk import WorkerLease, WorkFailureKind

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

    def previous_artifact_id(self, lease: WorkerLease) -> UUID | None: ...

    def robots_artifact_id(self, lease: WorkerLease) -> UUID | None: ...

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
                request = self._validate_and_read(heartbeat.lease)
                result = self._fetcher.fetch(request)
                manifest, raw_body, content_type = _build_manifest(
                    request,
                    result,
                    previous_artifact_id=self._gateway.previous_artifact_id(heartbeat.lease),
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

    def _validate_and_read(self, lease: WorkerLease) -> OfficialHttpRequest:
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
        if request.prior_artifact_id != previous:
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_PRIOR_ARTIFACT_MISMATCH",
                message="The HTTP conditional request does not match the leased prior artifact.",
            )
        return request


def _build_manifest(
    request: OfficialHttpRequest,
    result: HttpFetchResult,
    *,
    previous_artifact_id: UUID | None,
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
    robots_allowed: bool | None = None
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
            evaluation = evaluate_robots(result.body, request=request)
            robots_allowed = evaluation.allowed
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
        robots_decision_digest=request.robots_decision_digest,
    )
    return manifest, raw_body, content_type


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    if value.isdigit():
        return min(int(value), 86_400)
    return None


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
