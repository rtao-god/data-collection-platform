from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from manual_import_worker import (
    ManualImportWorker,
    ManualImportWorkerSettings,
    parse_manual_import_source,
)
from source_connector_sdk import LeaseArtifact, WorkerLease

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


class _FakeGateway:
    def __init__(self, lease: WorkerLease | None) -> None:
        self.lease = lease
        self.registered = False
        self.completed: tuple[str, object] | None = None
        self.failed: str | None = None

    def register(self, settings: ManualImportWorkerSettings) -> None:
        del settings
        self.registered = True

    def acquire(self, settings: ManualImportWorkerSettings) -> WorkerLease | None:
        del settings
        return self.lease

    def heartbeat(self, lease: WorkerLease, settings: ManualImportWorkerSettings) -> WorkerLease:
        del settings
        return lease

    def read_source(
        self,
        lease: WorkerLease,
        source: LeaseArtifact,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        del lease, source, max_bytes, timeout_seconds
        return (
            b"expected_entity_kind,display_name,website,osm_id,"
            b"reference_urls,note,provenance\n"
            b"place,Studio,,,,,manual-test\n"
        )

    def publish_plan(
        self,
        lease: WorkerLease,
        payload: bytes,
        *,
        content_digest: str,
        timeout_seconds: float,
    ) -> object:
        del lease, payload, content_digest, timeout_seconds
        return object()

    def complete(self, lease: WorkerLease, *, plan_digest: str, upload: object) -> None:
        del lease
        self.completed = (plan_digest, upload)

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: str,
        code: str,
        message: str,
        required_action: str,
    ) -> None:
        del lease, failure_kind, message, required_action
        self.failed = code


def _settings() -> ManualImportWorkerSettings:
    return ManualImportWorkerSettings(
        gateway_url="https://gateway.example.test",
        gateway_token="secret",
        build_identity="manual-import-worker-test",
        resource_profile="test",
        poll_interval_seconds=0.1,
        lease_duration_seconds=30,
        heartbeat_interval_seconds=5,
        transfer_timeout_seconds=5.0,
        max_source_bytes=1024,
    )


def _lease(role: str = "manual_source:csv:atomic") -> WorkerLease:
    return WorkerLease(
        lease_id=UUID("00000000-0000-0000-0000-000000000001"),
        work_id=UUID("00000000-0000-0000-0000-000000000002"),
        lease_token=UUID("00000000-0000-0000-0000-000000000003"),
        worker_id="manual-import-worker-test",
        stage=cast("object", "acquisition"),
        capability=cast("object", "manual_import"),
        input_digest="sha256:" + "1" * 64,
        expected_output_contract="manual-import-plan@1",
        issued_at_utc=_NOW,
        expires_at_utc=_NOW + timedelta(seconds=30),
        heartbeat_deadline_utc=_NOW + timedelta(seconds=10),
        source_permit=None,
        input_artifacts=(
            LeaseArtifact(
                artifact_id=UUID("00000000-0000-0000-0000-000000000004"),
                role=role,
            ),
        ),
        correlation_id="manual-import-test",
    )


def test_worker_publishes_and_completes_canonical_plan() -> None:
    gateway = _FakeGateway(_lease())
    worker = ManualImportWorker(gateway, _settings())

    worker.register()
    result = worker.run_once()

    assert gateway.registered is True
    assert result.acquired is True
    assert result.work_id == "00000000-0000-0000-0000-000000000002"
    assert result.plan_digest is not None
    assert result.plan_digest.startswith("sha256:")
    assert gateway.completed is not None
    assert gateway.completed[0] == result.plan_digest
    assert gateway.failed is None


def test_worker_returns_without_mutation_when_no_work_is_available() -> None:
    gateway = _FakeGateway(None)

    result = ManualImportWorker(gateway, _settings()).run_once()

    assert result.acquired is False
    assert gateway.completed is None
    assert gateway.failed is None


def test_typed_source_role_selects_jsonl_partial_mode() -> None:
    source = parse_manual_import_source(_lease("manual_source:jsonl:partial"))

    assert source.format.value == "jsonl"
    assert source.mode.value in {"partial", "accept_valid"}


def test_unknown_source_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="manual import source role"):
        parse_manual_import_source(_lease("source_file"))
