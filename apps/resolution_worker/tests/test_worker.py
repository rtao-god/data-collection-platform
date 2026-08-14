from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from resolution_contracts import (
    canonical_resolution_snapshot_json,
    decode_resolution_batch,
    decode_resolution_snapshot,
)
from resolution_worker import ResolutionWorker, ResolutionWorkerSettings, build_resolution_snapshot
from source_connector_sdk import LeaseArtifact, SourcePermit, WorkerLease

_ROOT = Path(__file__).resolve().parents[3]
_GOLDEN_BATCH = (_ROOT / "datasets/entity_resolution/golden-batch.json").read_bytes()
_GOLDEN_SNAPSHOT = (_ROOT / "datasets/entity_resolution/golden-snapshot.json").read_bytes()
_NOW = datetime(2026, 8, 14, tzinfo=UTC)


class _Gateway:
    def __init__(self, lease: WorkerLease, content: bytes) -> None:
        self.lease = lease
        self.content = content
        self.registered: ResolutionWorkerSettings | None = None
        self.published: tuple[bytes, str, str, str] | None = None
        self.failures: list[tuple[str, str]] = []

    def register(self, settings: ResolutionWorkerSettings) -> None:
        self.registered = settings

    def acquire(self, settings: ResolutionWorkerSettings) -> WorkerLease | None:
        assert settings.capability == "entity_resolution"
        return self.lease

    def heartbeat(
        self,
        lease: WorkerLease,
        settings: ResolutionWorkerSettings,
    ) -> WorkerLease:
        del settings
        return lease

    def read_input(self, lease: WorkerLease, *, role: str, maximum_bytes: int) -> bytes:
        lease.artifact(role)
        assert len(self.content) <= maximum_bytes
        return self.content

    def publish_and_complete(
        self,
        lease: WorkerLease,
        *,
        content: bytes,
        content_type: str,
        output_role: str,
        output_digest: str,
    ) -> None:
        assert lease is self.lease
        self.published = (content, content_type, output_role, output_digest)

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: str,
        code: str,
        message: str,
        required_action: str,
    ) -> None:
        del lease, message, required_action
        self.failures.append((failure_kind, code))


def _settings() -> ResolutionWorkerSettings:
    return ResolutionWorkerSettings(
        gateway_url="http://worker-gateway.test",
        gateway_token="token",
        build_identity="resolution-worker@tests",
        heartbeat_interval_seconds=60,
    )


def _lease(*, source_permit: SourcePermit | None = None) -> WorkerLease:
    return WorkerLease(
        lease_id=UUID(int=1),
        work_id=UUID(int=2),
        lease_token=UUID(int=3),
        worker_id="worker-resolution-tests",
        stage="entity_resolution",
        capability="entity_resolution",
        input_digest=f"sha256:{'4' * 64}",
        expected_output_contract="entity-resolution-snapshot@1",
        issued_at_utc=_NOW,
        expires_at_utc=_NOW + timedelta(minutes=5),
        heartbeat_deadline_utc=_NOW + timedelta(minutes=1),
        source_permit=source_permit,
        input_artifacts=(LeaseArtifact(artifact_id=UUID(int=10), role="resolution_batch"),),
        correlation_id="correlation-resolution-tests",
    )


def test_worker_publishes_exact_golden_snapshot() -> None:
    settings = _settings()
    gateway = _Gateway(_lease(), _GOLDEN_BATCH)
    worker = ResolutionWorker(gateway, settings)

    worker.register()
    result = worker.run_once()

    assert gateway.registered == settings
    assert gateway.published is not None
    content, content_type, role, digest = gateway.published
    expected = decode_resolution_snapshot(_GOLDEN_SNAPSHOT)
    assert content == _GOLDEN_SNAPSHOT
    assert content_type == "application/vnd.collection.entity-resolution-snapshot+json"
    assert role == "resolution_snapshot"
    assert digest == expected.snapshot_digest == result.output_digest


def test_resolution_lease_rejects_source_permit() -> None:
    gateway = _Gateway(
        _lease(
            source_permit=SourcePermit(
                source_key="source-test",
                policy_digest=f"sha256:{'5' * 64}",
                permit_not_before_utc=_NOW,
            )
        ),
        _GOLDEN_BATCH,
    )
    worker = ResolutionWorker(gateway, _settings())

    with pytest.raises(ValueError, match="must not carry"):
        worker.run_once()

    assert gateway.published is None
    assert gateway.failures == [("permanent", "RESOLUTION_CONTRACT_INVALID")]


def test_golden_dataset_is_byte_deterministic() -> None:
    batch = decode_resolution_batch(_GOLDEN_BATCH)
    first = build_resolution_snapshot(batch)
    second = build_resolution_snapshot(batch)

    assert first == second
    assert canonical_resolution_snapshot_json(first).encode() == _GOLDEN_SNAPSHOT
