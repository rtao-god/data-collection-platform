from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from collection_contracts import ManualRecordLineage, canonical_manual_record_lineage_bytes
from http_worker.worker import HttpWorker, HttpWorkerPolicy
from official_http import (
    HttpAcquisitionManifest,
    HttpFetchResult,
    ResponseHeader,
    decode_http_request,
    evaluate_robots,
)
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


def _robots_manifest_bytes() -> bytes:
    return HttpAcquisitionManifest(
        requestId="route-robots-1",
        sourceKey="source-official-example",
        sourcePolicyDigest="sha256:" + "2" * 64,
        requestKind="robots",
        requestedUrl="https://example.com/robots.txt",
        finalUrl="https://example.com/robots.txt",
        outcome="fetched",
        statusCode=200,
        responseHeaders=(),
        remoteIpAddress="93.184.216.34",
        encodedSizeBytes=1,
        decodedSizeBytes=1,
        observedAtUtc=datetime(2026, 8, 14, tzinfo=UTC),
        rawArtifactDigest="sha256:" + "9" * 64,
        robotsAllowed=True,
        robotsDecisionDigest="sha256:" + "8" * 64,
        robotsTargetUrl="https://example.com/studios/private?room=1",
    ).to_bytes()


def _manual_record_lineage_content() -> bytes:
    return canonical_manual_record_lineage_bytes(
        ManualRecordLineage(
            runId=UUID(int=20),
            manualRecordWorkId=UUID(int=21),
            manualRecordArtifactId=UUID(int=22),
            manualRecordArtifactDigest="sha256:" + "2" * 64,
            manualRecordContentDigest="sha256:" + "4" * 64,
            recordDigest="sha256:" + "4" * 64,
            expectedEntityKind="place",
            sourceArtifactId=UUID(int=23),
            sourceArtifactDigest="sha256:" + "3" * 64,
        )
    )


def _manual_record_lineage_identity(content: bytes) -> LeaseArtifact:
    return LeaseArtifact(
        artifact_id=UUID(int=9),
        role="manual_record_lineage",
        content_digest=f"sha256:{sha256(content).hexdigest()}",
        size_bytes=len(content),
        content_type="application/vnd.collection.manual-record-lineage+json",
    )


class Gateway:
    def __init__(
        self,
        lease: WorkerLease | None,
        request: bytes,
        route_context: bytes | None = None,
    ) -> None:
        self.lease = lease
        self.request = request
        self.route_context = route_context
        self.manual_record_lineage_content = _manual_record_lineage_content()
        self.robots_manifest = _robots_manifest_bytes()
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

    def read_website_route_context(
        self,
        lease: WorkerLease,
        *,
        maximum_bytes: int,
    ) -> bytes | None:
        assert lease == self.lease
        if self.route_context is not None:
            assert len(self.route_context) <= maximum_bytes
            assert any(item.role == "website_route_context" for item in lease.input_artifacts)
        return self.route_context

    def read_manual_record_lineage(
        self,
        lease: WorkerLease,
        *,
        maximum_bytes: int,
    ) -> tuple[LeaseArtifact, bytes] | None:
        assert lease == self.lease
        artifact = next(
            (item for item in lease.input_artifacts if item.role == "manual_record_lineage"),
            None,
        )
        if artifact is None:
            return None
        assert len(self.manual_record_lineage_content) <= maximum_bytes
        return artifact, self.manual_record_lineage_content

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

    def read_robots_manifest(self, lease: WorkerLease, *, maximum_bytes: int) -> bytes | None:
        assert lease == self.lease
        if self.robots_manifest is None:
            return None
        assert len(self.robots_manifest) <= maximum_bytes
        assert any(item.role == "robots_decision" for item in lease.input_artifacts)
        return self.robots_manifest

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


def _website_robots_work() -> tuple[WorkerLease, bytes, bytes]:
    request_document = json.loads(_request())
    request_document.update(
        {
            "requestId": "route-robots-1",
            "requestKind": "robots",
            "url": "https://example.com/robots.txt",
            "pageInterests": [],
            "robotsAllowed": None,
            "robotsArtifactId": None,
            "robotsDecisionDigest": None,
        }
    )
    request_payload = json.dumps(
        request_document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    context_payload = json.dumps(
        {
            "contract": "collector-website-route-context",
            "contractRevision": "website-route-context-v1",
            "requestId": "route-robots-1",
            "requestDigest": f"sha256:{sha256(request_payload).hexdigest()}",
            "campaignBundleDigest": "sha256:" + "3" * 64,
            "recordDigest": "sha256:" + "4" * 64,
            "routeKey": "manual_website",
            "sourceBindingKey": "manual_seed_import",
            "targetBindingKey": "official_website",
            "sourceKey": "source-official-example",
            "sourcePolicyDigest": "sha256:" + "2" * 64,
            "sourceUrl": "https://example.com/studios/private?room=1",
            "pageInterests": [{"key": "contact", "tokens": ["contact", "kontakt"], "priority": 80}],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    lineage_artifact = _manual_record_lineage_identity(_manual_record_lineage_content())
    lease = replace(
        _lease(),
        input_artifacts=(
            LeaseArtifact(artifact_id=UUID(int=4), role="http_request"),
            LeaseArtifact(artifact_id=UUID(int=7), role="website_route_context"),
            lineage_artifact,
        ),
    )
    return lease, request_payload, context_payload


def _website_page_work(*, conditional: bool = False) -> tuple[WorkerLease, bytes, bytes]:
    lease, _, context_payload = _website_robots_work()
    context_document = json.loads(context_payload)
    request_document = json.loads(_request(conditional=conditional))
    request_document.update(
        {
            "requestId": "route-page-1",
            "requestKind": "page",
            "url": context_document["sourceUrl"],
            "pageInterests": context_document["pageInterests"],
            "robotsAllowed": True,
            "robotsArtifactId": str(UUID(int=8)),
            "robotsDecisionDigest": "sha256:" + "8" * 64,
            "robotsManifestDigest": f"sha256:{sha256(_robots_manifest_bytes()).hexdigest()}",
        }
    )
    request_payload = json.dumps(
        request_document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    context_document["requestId"] = request_document["requestId"]
    context_document["requestDigest"] = f"sha256:{sha256(request_payload).hexdigest()}"
    context_payload = json.dumps(
        context_document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    lease = replace(
        lease,
        input_artifacts=(
            LeaseArtifact(artifact_id=UUID(int=4), role="http_request"),
            LeaseArtifact(artifact_id=UUID(int=7), role="website_route_context"),
            next(item for item in lease.input_artifacts if item.role == "manual_record_lineage"),
            LeaseArtifact(artifact_id=UUID(int=8), role="robots_decision"),
            *(
                (LeaseArtifact(artifact_id=UUID(int=5), role="previous_raw_artifact"),)
                if conditional
                else ()
            ),
        ),
    )
    return lease, request_payload, context_payload


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


def test_worker_binds_robots_decision_to_seed_path_from_saved_route_context() -> None:
    lease, request_payload, context_payload = _website_robots_work()
    gateway = Gateway(lease, request_payload, context_payload)
    robots_body = b"User-agent: *\nDisallow: /studios/private\n"
    result = replace(
        _result(
            200,
            body=robots_body,
            headers=(ResponseHeader(name="content-type", value="text/plain"),),
        ),
        final_url="https://example.com/robots.txt",
    )

    assert _worker(gateway, Fetcher(result)).run_once() is True

    assert gateway.failures == []
    assert gateway.published is not None
    manifest, _, _ = gateway.published
    assert manifest.robots_allowed is False
    assert manifest.robots_decision_digest is not None
    assert manifest.robots_decision_digest.startswith("sha256:")
    request = decode_http_request(request_payload)
    target_decision = evaluate_robots(
        robots_body,
        request=request,
        target_url="https://example.com/studios/private?room=1",
    )
    root_decision = evaluate_robots(
        robots_body,
        request=request,
        target_url="https://example.com/",
    )
    assert manifest.robots_decision_digest == target_decision.decision_digest
    assert target_decision.allowed is False
    assert root_decision.allowed is True
    assert target_decision.decision_digest != root_decision.decision_digest


def test_worker_executes_page_request_with_bound_route_and_robots_evidence() -> None:
    lease, request_payload, context_payload = _website_page_work()
    gateway = Gateway(lease, request_payload, context_payload)
    fetcher = Fetcher(
        replace(
            _result(200),
            final_url="https://example.com/studios/private?room=1",
        )
    )

    assert _worker(gateway, fetcher).run_once() is True

    assert fetcher.calls == 1
    assert gateway.failures == []
    assert gateway.published is not None
    manifest, raw_body, _ = gateway.published
    assert manifest.outcome == "fetched"
    assert manifest.robots_allowed is True
    assert manifest.robots_artifact_id == UUID(int=8)
    assert manifest.robots_decision_digest == "sha256:" + "8" * 64
    assert manifest.robots_target_url == "https://example.com/studios/private?room=1"
    assert raw_body is not None


def test_routed_work_rejects_missing_or_tampered_manual_record_lineage() -> None:
    lease, request_payload, context_payload = _website_robots_work()
    lineage_content = _manual_record_lineage_content()
    lineage_artifact = next(
        item for item in lease.input_artifacts if item.role == "manual_record_lineage"
    )
    cases = (
        (
            replace(
                lease,
                input_artifacts=tuple(
                    item for item in lease.input_artifacts if item.role != "manual_record_lineage"
                ),
            ),
            lineage_content,
            "OFFICIAL_HTTP_MANUAL_RECORD_LINEAGE_MISSING",
        ),
        (
            replace(
                lease,
                input_artifacts=tuple(
                    replace(item, content_digest="sha256:" + "f" * 64)
                    if item.role == "manual_record_lineage"
                    else item
                    for item in lease.input_artifacts
                ),
            ),
            lineage_content,
            "OFFICIAL_HTTP_MANUAL_RECORD_LINEAGE_INVALID",
        ),
        (
            replace(
                lease,
                input_artifacts=tuple(
                    replace(item, size_bytes=(item.size_bytes or 0) + 1)
                    if item.role == "manual_record_lineage"
                    else item
                    for item in lease.input_artifacts
                ),
            ),
            lineage_content,
            "OFFICIAL_HTTP_MANUAL_RECORD_LINEAGE_INVALID",
        ),
        (
            replace(
                lease,
                input_artifacts=tuple(
                    replace(item, content_type="application/json")
                    if item.role == "manual_record_lineage"
                    else item
                    for item in lease.input_artifacts
                ),
            ),
            lineage_content,
            "OFFICIAL_HTTP_MANUAL_RECORD_LINEAGE_INVALID",
        ),
        (
            lease,
            lineage_content + b" ",
            "OFFICIAL_HTTP_MANUAL_RECORD_LINEAGE_INVALID",
        ),
    )

    for lease_case, content, expected_code in cases:
        assert lineage_artifact.role == "manual_record_lineage"
        gateway = Gateway(lease_case, request_payload, context_payload)
        gateway.manual_record_lineage_content = content
        fetcher = Fetcher(_result(200))

        assert _worker(gateway, fetcher).run_once() is True
        assert gateway.failures[-1][1] == expected_code
        assert fetcher.calls == 0


def test_routed_page_rejects_missing_substituted_and_mismatched_robots_evidence() -> None:
    lease, request_payload, context_payload = _website_page_work()

    missing = Gateway(lease, request_payload, context_payload)
    missing.robots_manifest = None
    fetcher = Fetcher(_result(200))
    assert _worker(missing, fetcher).run_once() is True
    assert fetcher.calls == 0
    assert missing.failures == [("policy_blocked", "OFFICIAL_HTTP_ROBOTS_EVIDENCE_MISSING", None)]

    substituted = Gateway(lease, request_payload, context_payload)
    substituted.robots_manifest += b" "
    fetcher = Fetcher(_result(200))
    assert _worker(substituted, fetcher).run_once() is True
    assert fetcher.calls == 0
    assert substituted.failures == [
        ("policy_blocked", "OFFICIAL_HTTP_ROBOTS_EVIDENCE_MISMATCH", None)
    ]

    changed_manifest = json.loads(_robots_manifest_bytes())
    changed_manifest["robotsDecisionDigest"] = "sha256:" + "7" * 64
    changed_manifest_bytes = HttpAcquisitionManifest.model_validate(changed_manifest).to_bytes()
    request_document = json.loads(request_payload)
    request_document["robotsManifestDigest"] = (
        f"sha256:{sha256(changed_manifest_bytes).hexdigest()}"
    )
    changed_request = json.dumps(
        request_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    context_document = json.loads(context_payload)
    context_document["requestDigest"] = f"sha256:{sha256(changed_request).hexdigest()}"
    changed_context = json.dumps(
        context_document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    mismatched = Gateway(lease, changed_request, changed_context)
    mismatched.robots_manifest = changed_manifest_bytes
    fetcher = Fetcher(_result(200))
    assert _worker(mismatched, fetcher).run_once() is True
    assert fetcher.calls == 0
    assert mismatched.failures == [
        ("policy_blocked", "OFFICIAL_HTTP_ROBOTS_EVIDENCE_MISMATCH", None)
    ]


def test_routed_page_without_lease_context_cannot_fall_back_to_unrouted_path() -> None:
    lease, request_payload, _ = _website_page_work()
    lease = replace(
        lease,
        input_artifacts=tuple(
            item for item in lease.input_artifacts if item.role != "website_route_context"
        ),
    )
    gateway = Gateway(lease, request_payload, None)
    fetcher = Fetcher(_result(200))

    assert _worker(gateway, fetcher).run_once() is True
    assert fetcher.calls == 0
    assert gateway.failures == [("policy_blocked", "OFFICIAL_HTTP_ROBOTS_EVIDENCE_MISSING", None)]


def test_worker_preserves_robot_permission_on_non_success_page_outcomes() -> None:
    cases = (
        (304, True, (ResponseHeader(name="etag", value='"etag"'),), "unchanged"),
        (302, False, (ResponseHeader(name="location", value="/new-path"),), "redirect"),
        (404, False, None, "not_found"),
        (410, False, None, "not_found"),
    )
    for status, conditional, headers, expected_outcome in cases:
        lease, request_payload, context_payload = _website_page_work(conditional=conditional)
        gateway = Gateway(lease, request_payload, context_payload)
        fetcher = Fetcher(_result(status, body=b"", headers=headers))

        assert _worker(gateway, fetcher).run_once() is True
        assert gateway.failures == []
        assert gateway.published is not None
        manifest, raw_body, _ = gateway.published
        assert manifest.outcome == expected_outcome
        assert manifest.robots_allowed is True
        assert manifest.robots_artifact_id == UUID(int=8)
        assert manifest.robots_decision_digest == "sha256:" + "8" * 64
        assert raw_body is None


def test_worker_rejects_website_route_context_not_bound_to_request_bytes() -> None:
    lease, request_payload, context_payload = _website_robots_work()
    context_document = json.loads(context_payload)
    context_document["requestDigest"] = "sha256:" + "0" * 64
    context_payload = json.dumps(
        context_document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    gateway = Gateway(lease, request_payload, context_payload)
    fetcher = Fetcher(_result(200))

    assert _worker(gateway, fetcher).run_once() is True

    assert fetcher.calls == 0
    assert gateway.published is None
    assert gateway.failures == [
        ("policy_blocked", "OFFICIAL_HTTP_WEBSITE_ROUTE_CONTEXT_MISMATCH", None)
    ]


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
