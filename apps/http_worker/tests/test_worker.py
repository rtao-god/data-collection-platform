from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from http_worker.worker import HttpWorker, HttpWorkerPolicy

from official_http import HttpAcquisitionManifest, HttpFetchResult, ResponseHeader
from source_connector_sdk import LeaseArtifact, SourcePermit, WorkerLease


def _lease(*, conditional: bool = False) -> WorkerLease:
    issued = datetime(2026, 8, 14, tzinfo=UTC)
    artifacts = [
        LeaseArtifact(artifact_id=UUID(int=4), role="http_request"),
        LeaseArtifact(artifact_id=UUID(int=6), role="robots_decision"),
    ]
    if conditional:
        artifacts.append(LeaseArtifact(artifact_id=UUID(int=5), role="previous_raw_artifact"))
    return WorkerLease(
        lease_id=UUID(int=1),
        work_id=UUID(int=2),
        lease_token=UUID(int=3),
        worker_id="http-worker-1",
        stage="acquisition",
        capability="http_fetch",
        input_digest="sha256:" + "1" * 64,
        expected_output_contract="official-http-acquisition@1",
        issued_at_utc=issued,
        expires_at_utc=issued + timedelta(minutes=5),
        heartbeat_deadline_utc=issued + timedelta(minutes=1),
        source_permit=SourcePermit(
            source_key="source-official-example",
            policy_digest="sha256:" + "2" * 64,
            permit_not_before_utc=issued,
        ),
        input_artifacts=tuple(artifacts),
        correlation_id="correlation-1",
    )


def _request(*, conditional: bool = False) -> bytes:
    document: dict[str, object] = {
        "contract": "official-http-request",
        "contractRevision": "official-http-request-v1",
        "requestId": "request-1",
        "sourceKey": "source-official-example",
        "sourcePolicyDigest": "sha256:" + "2" * 64,
        "requestKind": "page",
        "url": "https://example.com/contact",
        "allowedOrigin": "https://example.com",
        "userAgent": "DataCollectionPlatform/1",
        "timeoutSeconds": 30,
        "maximumEncodedBytes": 1048576,
        "maximumDecodedBytes": 2097152,
        "depth": 0,
        "maximumDiscoveredUrls": 10,
        "trackingQueryParameters": [],
        "pageInterests": [{"key": "contact", "tokens": ["contact"], "priority": 100}],
        "robotsAllowed": True,
        "robotsArtifactId": "00000000-0000-0000-0000-000000000006",
        "robotsDecisionDigest": "sha256:" + "6" * 64,
        "ifNoneMatch": None,
        "ifModifiedSince": None,
        "priorArtifactId": None,
        "priorContentDigest": None,
    }
    if conditional:
        document.update(
            {
                "ifNoneMatch": '"etag"',
                "priorArtifactId": str(UUID(int=5)),
                "priorContentDigest": "sha256:" + "5" * 64,
            }
        )
    return json.dumps(document, separators=(",", ":")).encode()


class Gateway:
    def __init__(self, lease: WorkerLease | None, request: bytes) -> None:
        self.lease = lease
        self.request = request
        self.published: tuple[HttpAcquisitionManifest, bytes | None, str] | None = None
        self.failures: list[tuple[str, str, int | None]] = []

    def acquire(
        self, *, lease_duration_seconds: int, heartbeat_interval_seconds: int
    ) -> WorkerLease | None:
        assert lease_duration_seconds == 300
        assert heartbeat_interval_seconds == 60
        return self.lease

    def heartbeat(
        self, lease: WorkerLease, *, lease_duration_seconds: int, heartbeat_interval_seconds: int
    ) -> WorkerLease:
        return lease

    def read_request(self, lease: WorkerLease, *, maximum_bytes: int) -> bytes:
        assert lease == self.lease
        assert len(self.request) <= maximum_bytes
        return self.request

    def previous_artifact_id(self, lease: WorkerLease) -> UUID | None:
        return next(
            (
                item.artifact_id
                for item in lease.input_artifacts
                if item.role == "previous_raw_artifact"
            ),
            None,
        )

    def robots_artifact_id(self, lease: WorkerLease) -> UUID | None:
        return next(
            (item.artifact_id for item in lease.input_artifacts if item.role == "robots_decision"),
            None,
        )

    def publish(
        self,
        lease: WorkerLease,
        *,
        manifest: HttpAcquisitionManifest,
        raw_body: bytes | None,
        raw_content_type: str,
    ) -> None:
        assert lease == self.lease
        self.published = (manifest, raw_body, raw_content_type)

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: str,
        error_code: str,
        message: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        del lease, message
        self.failures.append((failure_kind, error_code, retry_after_seconds))


class Fetcher:
    def __init__(self, result: HttpFetchResult) -> None:
        self.result = result
        self.calls = 0

    def fetch(self, request: object) -> HttpFetchResult:
        del request
        self.calls += 1
        return self.result


def _result(
    status: int,
    *,
    body: bytes = b"<a href='/contact'>Contact</a>",
    headers: tuple[ResponseHeader, ...] | None = None,
) -> HttpFetchResult:
    return HttpFetchResult(
        status_code=status,
        final_url="https://example.com/contact",
        headers=headers or (ResponseHeader(name="content-type", value="text/html"),),
        remote_ip_address="93.184.216.34",
        encoded_size_bytes=len(body),
        decoded_size_bytes=len(body),
        observed_at_utc=datetime(2026, 8, 14, tzinfo=UTC),
        body=body,
    )


def _worker(gateway: Gateway, fetcher: Fetcher) -> HttpWorker:
    return HttpWorker(
        gateway,
        fetcher,
        policy=HttpWorkerPolicy(
            lease_duration_seconds=300,
            heartbeat_interval_seconds=60,
            poll_interval_seconds=0.1,
        ),
    )


def test_worker_publishes_raw_body_and_manifest_for_success() -> None:
    gateway = Gateway(_lease(), _request())
    assert _worker(gateway, Fetcher(_result(200))).run_once() is True
    assert gateway.failures == []
    assert gateway.published is not None
    manifest, raw_body, content_type = gateway.published
    assert manifest.outcome == "fetched"
    assert manifest.raw_artifact_digest == f"sha256:{sha256(raw_body or b'').hexdigest()}"
    assert content_type == "text/html"


def test_worker_reuses_exact_prior_artifact_for_304_without_uploading_body() -> None:
    gateway = Gateway(_lease(conditional=True), _request(conditional=True))
    fetcher = Fetcher(
        _result(304, body=b"", headers=(ResponseHeader(name="etag", value='"etag"'),))
    )
    assert _worker(gateway, fetcher).run_once() is True
    assert gateway.published is not None
    manifest, raw_body, _ = gateway.published
    assert manifest.outcome == "unchanged"
    assert manifest.reused_artifact_id == UUID(int=5)
    assert raw_body is None


def test_worker_maps_403_to_policy_block_and_never_browser_escalates() -> None:
    gateway = Gateway(_lease(), _request())
    assert _worker(gateway, Fetcher(_result(403, body=b""))).run_once() is True
    assert gateway.published is None
    assert gateway.failures == [("policy_blocked", "OFFICIAL_HTTP_FORBIDDEN", None)]


def test_worker_maps_429_retry_after_to_transient_failure() -> None:
    gateway = Gateway(_lease(), _request())
    fetcher = Fetcher(
        _result(429, body=b"", headers=(ResponseHeader(name="retry-after", value="30"),))
    )
    assert _worker(gateway, fetcher).run_once() is True
    assert gateway.failures == [("transient", "OFFICIAL_HTTP_RATE_LIMITED", 30)]


def test_worker_does_not_call_fetcher_for_source_permit_mismatch() -> None:
    lease = _lease()
    mismatched = replace(
        lease,
        source_permit=SourcePermit(
            source_key="different-source",
            policy_digest="sha256:" + "2" * 64,
            permit_not_before_utc=lease.issued_at_utc,
        ),
    )
    gateway = Gateway(mismatched, _request())
    fetcher = Fetcher(_result(200))
    assert _worker(gateway, fetcher).run_once() is True
    assert fetcher.calls == 0
    assert gateway.failures[0][0] == "policy_blocked"


def test_worker_returns_false_without_eligible_work() -> None:
    gateway = Gateway(None, _request())
    fetcher = Fetcher(_result(200))
    assert _worker(gateway, fetcher).run_once() is False
    assert fetcher.calls == 0
