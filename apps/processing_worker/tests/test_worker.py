from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest

from collection_contracts import (
    EvidenceLocatorKind,
    EvidenceReference,
    ExtractedField,
    ExtractedRecordPayload,
    ExtractedTextValue,
    ExtractionRequest,
    NormalizationFieldRule,
    NormalizationProfile,
    canonical_extracted_record_json,
    decode_extracted_record,
    decode_observation_batch,
    seal_extracted_record,
)
from processing_worker import ProcessingWorker, ProcessingWorkerSettings
from source_connector_sdk import LeaseArtifact, WorkerLease

_NOW = datetime(2026, 8, 14, tzinfo=UTC)
_RAW = b"<html><head><title>Example Studio</title></head><body><p>Mixing</p></body></html>"
_DIGEST = f"sha256:{sha256(_RAW).hexdigest()}"


class _Metadata:
    def extract(self, html_text: str, *, base_url: str) -> Mapping[str, object]:
        assert "Example Studio" in html_text
        assert base_url == "https://studio.example/"
        return {"json-ld": [{"@type": "LocalBusiness", "name": "Example Studio"}]}


class _Gateway:
    def __init__(self, lease: WorkerLease, content_by_role: dict[str, bytes]) -> None:
        self.lease = lease
        self.content_by_role = content_by_role
        self.registered: ProcessingWorkerSettings | None = None
        self.published: tuple[bytes, str, str, str] | None = None
        self.failures: list[tuple[str, str]] = []

    def register(self, settings: ProcessingWorkerSettings) -> None:
        self.registered = settings

    def acquire(self, settings: ProcessingWorkerSettings) -> WorkerLease | None:
        assert settings.capability == self.lease.capability
        return self.lease

    def heartbeat(
        self,
        lease: WorkerLease,
        settings: ProcessingWorkerSettings,
    ) -> WorkerLease:
        del settings
        return lease

    def read_input(self, lease: WorkerLease, *, role: str, maximum_bytes: int) -> bytes:
        lease.artifact(role)
        content = self.content_by_role[role]
        assert len(content) <= maximum_bytes
        return content

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


def _settings(capability: str) -> ProcessingWorkerSettings:
    return ProcessingWorkerSettings(
        gateway_url="http://worker-gateway.test",
        gateway_token="token",
        build_identity="processing-worker@tests",
        capability=capability,
        heartbeat_interval_seconds=60,
    )


def _lease(capability: str, roles: tuple[str, ...], output_contract: str) -> WorkerLease:
    return WorkerLease(
        lease_id=UUID(int=1),
        work_id=UUID(int=2),
        lease_token=UUID(int=3),
        worker_id="worker-processing-tests",
        stage=capability,
        capability=capability,
        input_digest=f"sha256:{'4' * 64}",
        expected_output_contract=output_contract,
        issued_at_utc=_NOW,
        expires_at_utc=_NOW + timedelta(minutes=5),
        heartbeat_deadline_utc=_NOW + timedelta(minutes=1),
        source_permit=None,
        input_artifacts=tuple(
            LeaseArtifact(artifact_id=UUID(int=index + 10), role=role)
            for index, role in enumerate(roles)
        ),
        correlation_id="correlation-processing-tests",
    )


def _request() -> ExtractionRequest:
    return ExtractionRequest(
        source_record_id="source-record-example",
        raw_artifact_digest=_DIGEST,
        source_url="https://studio.example/",
        content_type="text/html",
        source_policy_digest=f"sha256:{'5' * 64}",
        extractor_revision="official-website-extractor@1",
        observed_at_utc=_NOW,
        allowed_fields=("display_name",),
    )


def _record_bytes() -> bytes:
    evidence = EvidenceReference(
        raw_artifact_digest=_DIGEST,
        source_url="https://studio.example/",
        locator_kind=EvidenceLocatorKind.JSON_POINTER,
        locator_value="/json-ld/0/name",
        evidence_digest=f"sha256:{'6' * 64}",
        evidence_span="Example Studio",
        observed_at_utc=_NOW,
        extractor_revision="official-website-extractor@1",
    )
    record = seal_extracted_record(
        ExtractedRecordPayload(
            source_record_id="source-record-example",
            raw_artifact_digest=_DIGEST,
            source_url="https://studio.example/",
            content_type="text/html",
            source_policy_digest=f"sha256:{'5' * 64}",
            extractor_revision="official-website-extractor@1",
            observed_at_utc=_NOW,
            fields=(
                ExtractedField(
                    field_key="display_name",
                    value=ExtractedTextValue(value="Example Studio"),
                    evidence=evidence,
                ),
            ),
        )
    )
    return canonical_extracted_record_json(record).encode()


def test_extraction_capability_publishes_one_verified_derived_contract() -> None:
    settings = _settings("extraction")
    lease = _lease(
        "extraction",
        ("raw_source_document", "extraction_request"),
        "extracted-record@1",
    )
    gateway = _Gateway(
        lease,
        {
            "raw_source_document": _RAW,
            "extraction_request": _request().model_dump_json(by_alias=True).encode(),
        },
    )
    worker = ProcessingWorker(gateway, settings, metadata_extractor=_Metadata())

    worker.register()
    result = worker.run_once()

    assert gateway.registered == settings
    assert result.acquired is True
    assert gateway.published is not None
    content, content_type, role, digest = gateway.published
    record = decode_extracted_record(content)
    assert content_type == "application/vnd.collection.extracted-record+json"
    assert role == "extracted_record"
    assert digest == record.content_digest == result.output_digest
    assert {field.field_key for field in record.fields} == {"display_name"}


def test_normalization_capability_publishes_typed_observation_batch() -> None:
    profile = NormalizationProfile(
        normalizer_revision="website-normalizer@1",
        default_phone_region="DE",
        field_rules=(
            NormalizationFieldRule(
                source_field="display_name",
                target_field="display_name",
                value_kind="text",
            ),
        ),
    )
    settings = _settings("normalization")
    lease = _lease(
        "normalization",
        ("extracted_record", "normalization_profile"),
        "field-observation-batch@1",
    )
    gateway = _Gateway(
        lease,
        {
            "extracted_record": _record_bytes(),
            "normalization_profile": profile.model_dump_json(by_alias=True).encode(),
        },
    )
    worker = ProcessingWorker(gateway, settings)

    worker.register()
    result = worker.run_once()

    assert gateway.published is not None
    content, content_type, role, digest = gateway.published
    batch = decode_observation_batch(content)
    assert content_type == "application/vnd.collection.field-observation-batch+json"
    assert role == "field_observation_batch"
    assert digest == batch.content_digest == result.output_digest
    assert batch.observations[0].field_key == "display_name"


def test_unexpected_input_role_fails_before_reading_or_publishing() -> None:
    settings = _settings("extraction")
    lease = _lease(
        "extraction",
        ("raw_source_document", "wrong_request"),
        "extracted-record@1",
    )
    gateway = _Gateway(lease, {})
    worker = ProcessingWorker(gateway, settings, metadata_extractor=_Metadata())

    with pytest.raises(ValueError, match="roles"):
        worker.run_once()

    assert gateway.published is None
    assert gateway.failures == [("contract_invalid", "PROCESSING_CONTRACT_INVALID")]
