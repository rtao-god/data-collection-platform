from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from osm_overpass import OverpassFetchFailure, OverpassFetchResult
from osm_worker.worker import OSMWorker, OSMWorkerPolicy
from source_connector_sdk import LeaseArtifact, WorkerLease


def _lease() -> WorkerLease:
    issued = datetime(2026, 8, 13, tzinfo=UTC)
    return WorkerLease(
        lease_id=UUID(int=1),
        work_id=UUID(int=2),
        lease_token=UUID(int=3),
        worker_id="osm-worker-1",
        stage="discovery",
        capability="osm_query",
        input_digest="sha256:" + "1" * 64,
        expected_output_contract="osm-overpass-result/1",
        issued_at_utc=issued,
        expires_at_utc=issued + timedelta(minutes=5),
        heartbeat_deadline_utc=issued + timedelta(minutes=1),
        source_permit=None,
        input_artifacts=(
            LeaseArtifact(
                artifact_id=UUID(int=4),
                role="overpass_query_spec",
            ),
        ),
        correlation_id="correlation-1",
    )


def _query_spec() -> bytes:
    return json.dumps(
        {
            "schemaRevision": "osm-overpass-query/1",
            "polygon": [[52.5, 13.3], [52.6, 13.3], [52.55, 13.5]],
            "elementTypes": ["node"],
            "tagFilters": [{"key": "amenity", "values": ["studio"]}],
            "timeoutSeconds": 60,
            "maximumElements": 100,
        },
        separators=(",", ":"),
    ).encode()


class Gateway:
    def __init__(
        self,
        acquired: WorkerLease | None,
        *,
        query_spec: bytes | None = None,
    ) -> None:
        self.acquired = acquired
        self.query_spec = query_spec or _query_spec()
        self.published: tuple[bytes, bytes] | None = None
        self.failures: list[tuple[str, str, str, int | None]] = []

    def acquire(
        self,
        *,
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> WorkerLease | None:
        assert lease_duration_seconds == 300
        assert heartbeat_interval_seconds == 60
        return self.acquired

    def heartbeat(
        self,
        lease: WorkerLease,
        *,
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> WorkerLease:
        assert lease_duration_seconds == 300
        assert heartbeat_interval_seconds == 60
        return lease

    def read_artifact(
        self,
        lease: WorkerLease,
        *,
        role: str,
        maximum_bytes: int,
    ) -> bytes:
        assert lease == self.acquired
        assert role == "overpass_query_spec"
        assert len(self.query_spec) <= maximum_bytes
        return self.query_spec

    def publish_result(
        self,
        lease: WorkerLease,
        *,
        raw_response: bytes,
        observations: bytes,
    ) -> None:
        assert lease == self.acquired
        self.published = (raw_response, observations)

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: str,
        error_code: str,
        message: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        assert lease == self.acquired
        self.failures.append((failure_kind, error_code, message, retry_after_seconds))


class Fetcher:
    def __init__(
        self,
        *,
        body: bytes | None = None,
        failure: OverpassFetchFailure | None = None,
    ) -> None:
        self.body = body or b'{"elements":[]}'
        self.failure = failure
        self.calls = 0

    def fetch(self, spec: object) -> OverpassFetchResult:
        del spec
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return OverpassFetchResult(
            body=self.body,
            content_digest=f"sha256:{sha256(self.body).hexdigest()}",
            content_type="application/json",
        )


def _worker(gateway: Gateway, fetcher: Fetcher) -> OSMWorker:
    return OSMWorker(
        gateway,
        fetcher,
        policy=OSMWorkerPolicy(
            lease_duration_seconds=300,
            heartbeat_interval_seconds=60,
            poll_interval_seconds=0.1,
        ),
    )


def test_worker_preserves_raw_response_and_publishes_observations() -> None:
    raw = json.dumps(
        {
            "version": 0.6,
            "generator": "Overpass API",
            "elements": [
                {
                    "type": "node",
                    "id": 42,
                    "lat": 52.5,
                    "lon": 13.4,
                    "tags": {"name": "Studio"},
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    gateway = Gateway(_lease())
    assert _worker(gateway, Fetcher(body=raw)).run_once() is True
    assert gateway.failures == []
    assert gateway.published is not None
    raw_response, observations = gateway.published
    assert raw_response == raw
    document = json.loads(observations)
    assert document["attribution"] == "© OpenStreetMap contributors"
    assert document["observations"][0]["osmId"] == 42
    assert document["observations"][0]["sourceUrl"].endswith("/node/42")


def test_worker_maps_temporary_overpass_failure_to_retryable_failure() -> None:
    failure = OverpassFetchFailure(
        kind="transient",
        code="OVERPASS_TEMPORARILY_UNAVAILABLE",
        message="The Overpass endpoint is temporarily unavailable.",
        status_code=429,
        retry_after_seconds=30,
    )
    gateway = Gateway(_lease())
    assert _worker(gateway, Fetcher(failure=failure)).run_once() is True
    assert gateway.published is None
    assert gateway.failures == [
        (
            "transient",
            "OVERPASS_TEMPORARILY_UNAVAILABLE",
            "The Overpass endpoint is temporarily unavailable.",
            30,
        )
    ]


def test_worker_fails_invalid_query_artifact_without_network_call() -> None:
    invalid = json.dumps(
        {
            "schemaRevision": "osm-overpass-query/1",
            "city": "Berlin",
        }
    ).encode()
    gateway = Gateway(_lease(), query_spec=invalid)
    fetcher = Fetcher()
    assert _worker(gateway, fetcher).run_once() is True
    assert fetcher.calls == 0
    assert gateway.published is None
    assert gateway.failures[0][0] == "contract_invalid"
    assert gateway.failures[0][1] == "OVERPASS_QUERY_SPEC_FIELDS_INVALID"


def test_worker_returns_false_when_no_work_is_available() -> None:
    gateway = Gateway(None)
    fetcher = Fetcher()
    assert _worker(gateway, fetcher).run_once() is False
    assert fetcher.calls == 0
    assert gateway.published is None
