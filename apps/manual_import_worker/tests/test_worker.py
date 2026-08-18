from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest

from collection_contracts import ManualImportFormat, ManualImportMode
from manual_import_core import (
    build_manual_import_plan,
    canonical_manual_import_plan_json,
    decode_canonical_manual_import_record,
)
from manual_import_worker import (
    ManualWorker,
    ManualWorkerSettings,
    parse_manual_import_source,
    parse_manual_record_source,
)
from source_connector_sdk import LeaseArtifact, SourcePermit, WorkerLease

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
_SOURCE_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000004")
_PLAN_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000005")


class _FakeGateway:
    def __init__(
        self,
        lease: WorkerLease | None,
        *,
        artifact_bodies: dict[UUID, bytes] | None = None,
    ) -> None:
        self.lease = lease
        self.artifact_bodies = artifact_bodies or {}
        self.registered: ManualWorkerSettings | None = None
        self.published: tuple[bytes, str] | None = None
        self.completed: tuple[str, object] | None = None
        self.failed: str | None = None

    def register(self, settings: ManualWorkerSettings) -> None:
        self.registered = settings

    def acquire(self, settings: ManualWorkerSettings) -> WorkerLease | None:
        assert settings.capability == (
            self.lease.capability if self.lease is not None else settings.capability
        )
        return self.lease

    def heartbeat(self, lease: WorkerLease, settings: ManualWorkerSettings) -> WorkerLease:
        del settings
        return lease

    def read_artifact(
        self,
        lease: WorkerLease,
        artifact: LeaseArtifact,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        del lease, timeout_seconds
        body = self.artifact_bodies[artifact.artifact_id]
        assert len(body) <= max_bytes
        return body

    def publish_output(
        self,
        lease: WorkerLease,
        payload: bytes,
        *,
        content_digest: str,
    ) -> object:
        del lease
        assert content_digest == _digest(payload)
        self.published = (payload, content_digest)
        return object()

    def complete(self, lease: WorkerLease, *, output_digest: str, upload: object) -> None:
        del lease
        self.completed = (output_digest, upload)

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


def _settings(capability: str) -> ManualWorkerSettings:
    return ManualWorkerSettings(
        gateway_url="https://gateway.example.test",
        gateway_token="secret",
        capability=capability,  # type: ignore[arg-type]
        build_identity=f"{capability}-worker-test",
        resource_profile="test",
        poll_interval_seconds=0.1,
        lease_duration_seconds=30,
        heartbeat_interval_seconds=5,
        transfer_timeout_seconds=5.0,
        max_source_bytes=1024 * 1024,
        max_plan_bytes=1024 * 1024,
    )


def _source_permit() -> SourcePermit:
    return SourcePermit(
        source_key="manual_seed_import",
        policy_digest="sha256:" + "8" * 64,
        permit_not_before_utc=_NOW,
    )


def _lease(
    capability: str,
    *,
    source_role: str = "manual_source:csv:atomic",
    plan_position: int = 0,
) -> WorkerLease:
    if capability == "manual_import":
        artifacts = (LeaseArtifact(artifact_id=_SOURCE_ARTIFACT_ID, role=source_role),)
        expected_output = "manual-import-plan@1"
        permit = _source_permit()
    else:
        artifacts = (
            LeaseArtifact(artifact_id=_SOURCE_ARTIFACT_ID, role=source_role),
            LeaseArtifact(
                artifact_id=_PLAN_ARTIFACT_ID,
                role=f"manual_import_plan_record:{plan_position}",
            ),
        )
        expected_output = "manual-import-record@1"
        permit = None
    return WorkerLease(
        lease_id=UUID("00000000-0000-0000-0000-000000000001"),
        work_id=UUID("00000000-0000-0000-0000-000000000002"),
        lease_token=UUID("00000000-0000-0000-0000-000000000003"),
        worker_id=f"{capability}-worker-test",
        stage="discovery",
        capability=capability,  # type: ignore[arg-type]
        input_digest="sha256:" + "1" * 64,
        expected_output_contract=expected_output,
        issued_at_utc=_NOW,
        expires_at_utc=_NOW + timedelta(seconds=30),
        heartbeat_deadline_utc=_NOW + timedelta(seconds=10),
        source_permit=permit,
        input_artifacts=artifacts,
        correlation_id="manual-work-test",
    )


def _source_bytes() -> bytes:
    return (
        b"expected_entity_kind,display_name,website,osm_id,"
        b"reference_urls,note,provenance\n"
        b"place,Studio,,,,,manual-test\n"
    )


def _plan_bytes() -> bytes:
    plan = build_manual_import_plan(
        _source_bytes(),
        format=ManualImportFormat.CSV,
        mode=ManualImportMode.ATOMIC,
    )
    return canonical_manual_import_plan_json(plan).encode("utf-8")


def test_settings_require_and_bind_one_explicit_manual_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_GATEWAY_URL", "https://gateway.example.test")
    monkeypatch.setenv("WORKER_GATEWAY_TOKEN", "secret")
    monkeypatch.setenv("MANUAL_WORKER_CAPABILITY", "manual_record")

    settings = ManualWorkerSettings.from_environment()

    assert settings.capability == "manual_record"
    assert settings.build_identity == "manual-record-worker"
    assert settings.resource_profile == "manual-record"
    assert settings.output.output_contract == "manual-import-record@1"


def test_settings_reject_missing_manual_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_GATEWAY_URL", "https://gateway.example.test")
    monkeypatch.setenv("WORKER_GATEWAY_TOKEN", "secret")
    monkeypatch.delenv("MANUAL_WORKER_CAPABILITY", raising=False)

    with pytest.raises(ValueError, match="MANUAL_WORKER_CAPABILITY is required"):
        ManualWorkerSettings.from_environment()


def test_manual_import_worker_publishes_canonical_plan() -> None:
    lease = _lease("manual_import")
    gateway = _FakeGateway(
        lease,
        artifact_bodies={_SOURCE_ARTIFACT_ID: _source_bytes()},
    )
    worker = ManualWorker(gateway, _settings("manual_import"))

    worker.register()
    result = worker.run_once()

    assert gateway.registered is not None
    assert gateway.registered.capability == "manual_import"
    assert result.acquired is True
    assert result.output_contract == "manual-import-plan@1"
    assert result.output_digest is not None
    assert gateway.completed is not None
    assert gateway.completed[0] == result.output_digest
    assert gateway.failed is None


def test_manual_record_worker_materializes_exact_selected_plan_record() -> None:
    lease = _lease("manual_record")
    gateway = _FakeGateway(
        lease,
        artifact_bodies={_PLAN_ARTIFACT_ID: _plan_bytes()},
    )
    worker = ManualWorker(gateway, _settings("manual_record"))

    worker.register()
    result = worker.run_once()

    assert result.acquired is True
    assert result.capability == "manual_record"
    assert result.output_contract == "manual-import-record@1"
    assert result.output_digest is not None
    assert gateway.published is not None
    document = decode_canonical_manual_import_record(
        gateway.published[0],
        expected_content_digest=result.output_digest,
    )
    assert document.plan_record_position == 0
    assert document.locator.pointer == "line:2"
    assert document.record.display_name == "Studio"
    assert document.source_artifact_role == "manual_source:csv:atomic"
    assert gateway.completed is not None
    assert gateway.completed[0] == document.content_digest
    assert gateway.failed is None


def test_worker_returns_without_mutation_when_no_work_is_available() -> None:
    gateway = _FakeGateway(None)

    result = ManualWorker(gateway, _settings("manual_record")).run_once()

    assert result.acquired is False
    assert result.capability == "manual_record"
    assert result.output_contract == "manual-import-record@1"
    assert result.output_digest is None
    assert gateway.completed is None
    assert gateway.failed is None


def test_typed_source_role_selects_jsonl_partial_mode() -> None:
    source = parse_manual_import_source(
        _lease("manual_import", source_role="manual_source:jsonl:partial")
    )

    assert source.format.value == "jsonl"
    assert source.mode.value == "partial"


def test_manual_record_binding_is_zero_based_and_source_permit_free() -> None:
    source = parse_manual_record_source(
        _lease(
            "manual_record",
            source_role="manual_import_source:csv:atomic",
            plan_position=12,
        )
    )

    assert source.plan_record_position == 12
    assert source.plan_artifact.artifact_id == _PLAN_ARTIFACT_ID
    assert source.source_artifact.artifact_id == _SOURCE_ARTIFACT_ID


def test_manual_record_rejects_wrong_capability_and_noncanonical_plan_role() -> None:
    with pytest.raises(ValueError, match="capability"):
        parse_manual_record_source(_lease("manual_import"))

    lease = _lease("manual_record")
    invalid = replace(
        lease,
        input_artifacts=(
            lease.input_artifacts[0],
            LeaseArtifact(
                artifact_id=_PLAN_ARTIFACT_ID,
                role="manual_import_plan_record:01",
            ),
        ),
    )
    with pytest.raises(ValueError, match="binding role"):
        parse_manual_record_source(invalid)


def test_manual_record_contract_failure_is_reported_to_gateway() -> None:
    lease = _lease("manual_record", plan_position=1)
    gateway = _FakeGateway(
        lease,
        artifact_bodies={_PLAN_ARTIFACT_ID: _plan_bytes()},
    )

    with pytest.raises(ValueError, match="outside"):
        ManualWorker(gateway, _settings("manual_record")).run_once()

    assert gateway.failed == "MANUAL_IMPORT_RECORD_INPUT_MISMATCH"
    assert gateway.completed is None


def _digest(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"
