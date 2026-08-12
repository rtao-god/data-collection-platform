from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from botocore.exceptions import ClientError
from collection_infrastructure.object_store.s3 import S3Client
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from collection_application import (
    ArtifactKind,
    ArtifactTransferConflict,
    CollectionRunSpec,
    CollectionRunState,
    LeaseRequest,
    PrepareArtifactRead,
    PrepareArtifactUpload,
    RetryPolicy,
    SourceCapacitySpec,
    SourceOperationalState,
    StageRunSpec,
    StageRunState,
    VerifyArtifactUpload,
    WorkCapability,
    WorkCompletion,
    WorkCompletionStatus,
    WorkEngineConflict,
    WorkerRegistration,
    WorkInputArtifact,
    WorkOutputArtifact,
    WorkStage,
    WorkUnitSpec,
)
from collection_infrastructure import (
    PostgresArtifactTransfer,
    PostgresWorkEngine,
    S3ArtifactObjectStore,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)
_BODY = b"atomic artifact body"
_DIGEST = f"sha256:{sha256(_BODY).hexdigest()}"
_CONTENT_TYPE = "text/html"


@dataclass(slots=True)
class StoredObject:
    content: bytes
    content_type: str
    metadata: dict[str, str]


class Body:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._offset = 0

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._content) - self._offset
        chunk = self._content[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        return None


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.last_prepared_key: str | None = None
        self.copy_count = 0

    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: Mapping[str, object],
        ExpiresIn: int,
        HttpMethod: str | None = None,
    ) -> str:
        del ExpiresIn, HttpMethod
        key = str(Params["Key"])
        if client_method == "put_object":
            self.last_prepared_key = key
        return f"https://object-store.invalid/{key}"

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        key = str(kwargs["Key"])
        stored = self.objects.get(key)
        if stored is None:
            raise _missing_key(key)
        return {
            "Body": Body(stored.content),
            "ContentLength": len(stored.content),
            "ContentType": stored.content_type,
            "Metadata": dict(stored.metadata),
        }

    def head_bucket(self, **kwargs: object) -> Mapping[str, object]:
        del kwargs
        return {}

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        key = str(kwargs["Key"])
        stored = self.objects.get(key)
        if stored is None:
            raise _missing_key(key)
        return {
            "ContentLength": len(stored.content),
            "ContentType": stored.content_type,
            "Metadata": dict(stored.metadata),
        }

    def copy_object(self, **kwargs: object) -> Mapping[str, object]:
        target_key = str(kwargs["Key"])
        source = cast(Mapping[str, object], kwargs["CopySource"])
        source_key = str(source["Key"])
        stored = self.objects[source_key]
        metadata = cast(Mapping[str, str], kwargs["Metadata"])
        self.objects[target_key] = StoredObject(
            content=stored.content,
            content_type=str(kwargs["ContentType"]),
            metadata=dict(metadata),
        )
        self.copy_count += 1
        return {}

    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        self.objects.pop(str(kwargs["Key"]), None)
        return {}

    def upload_prepared_body(self, content: bytes = _BODY) -> None:
        if self.last_prepared_key is None:
            raise AssertionError("prepare upload did not establish a staging key")
        self.objects[self.last_prepared_key] = StoredObject(
            content=content,
            content_type=_CONTENT_TYPE,
            metadata={"sha256": _DIGEST.removeprefix("sha256:")},
        )


def _missing_key(key: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "NoSuchKey", "Message": f"missing {key}"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadObject",
    )


def _database_url() -> str:
    value = os.environ.get("COLLECTOR_DATABASE_URL", "").strip()
    if not value:
        pytest.fail("COLLECTOR_DATABASE_URL is required for artifact runtime tests.")
    return value


@pytest.fixture
def engine() -> Iterator[Engine]:
    value = sa.create_engine(_database_url(), poolclass=NullPool)
    try:
        yield value
    finally:
        value.dispose()


def _label(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _digest(*parts: str) -> str:
    return f"sha256:{sha256(':'.join(parts).encode()).hexdigest()}"


def _insert_snapshot(engine: Engine, label: str) -> tuple[str, str]:
    campaign_key = f"campaign_{label}"
    bundle_digest = _digest(label, "bundle")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO config.config_bundle_components (
                    bundle_digest,
                    position,
                    path,
                    component_digest
                ) VALUES (
                    :bundle_digest,
                    0,
                    'campaign.yaml',
                    :component_digest
                )
                """
            ),
            {
                "bundle_digest": bundle_digest,
                "component_digest": _digest(label, "component"),
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO config.config_bundles (
                    bundle_digest,
                    campaign_key,
                    contract,
                    contract_revision,
                    readiness,
                    recorded_at_utc
                ) VALUES (
                    :bundle_digest,
                    :campaign_key,
                    'collector-campaign-snapshot',
                    'campaign-snapshot-v1',
                    'ready',
                    :recorded_at_utc
                )
                """
            ),
            {
                "bundle_digest": bundle_digest,
                "campaign_key": campaign_key,
                "recorded_at_utc": _NOW,
            },
        )
    return campaign_key, bundle_digest


@dataclass(frozen=True, slots=True)
class RuntimeHarness:
    work_engine: PostgresWorkEngine
    transfer: PostgresArtifactTransfer
    s3: FakeS3Client
    run_id: UUID
    acquisition_stage_id: UUID
    source_key: str
    worker_id: str
    worker_build: str
    output_contract: str
    label: str


def _harness(engine: Engine, label: str) -> RuntimeHarness:
    campaign_key, bundle_digest = _insert_snapshot(engine, label)
    work_engine = PostgresWorkEngine(engine, clock=lambda: _NOW)
    run_id = uuid4()
    acquisition_stage_id = uuid4()
    source_key = f"source_{label}"
    worker_id = f"worker-{label[-20:]}"
    worker_build = f"build-{label[-20:]}"
    output_contract = f"fetch-output-{label[-16:]}"
    work_engine.create_run(
        CollectionRunSpec(
            run_id=run_id,
            campaign_key=campaign_key,
            config_bundle_digest=bundle_digest,
            initial_state=CollectionRunState.RUNNING,
            correlation_id=f"correlation-{label}",
        )
    )
    work_engine.create_stage(
        StageRunSpec(
            stage_run_id=acquisition_stage_id,
            run_id=run_id,
            stage=WorkStage.ACQUISITION,
            initial_state=StageRunState.RUNNING,
            correlation_id=f"correlation-{label}",
        )
    )
    work_engine.configure_source(
        SourceCapacitySpec(
            source_key=source_key,
            policy_digest=_digest(label, "policy"),
            state=SourceOperationalState.ACTIVE,
            max_active_requests=1,
            minimum_interval_milliseconds=0,
            correlation_id=f"correlation-{label}",
        )
    )
    work_engine.register_worker(
        WorkerRegistration(
            worker_id=worker_id,
            build_identity=worker_build,
            capabilities=frozenset({WorkCapability.HTTP_FETCH}),
            supported_output_contracts=frozenset({output_contract}),
            max_concurrency=1,
            resource_profile="integration-test",
            correlation_id=f"correlation-{label}",
        )
    )
    s3 = FakeS3Client()
    object_store = S3ArtifactObjectStore(cast(S3Client, s3), bucket="collector-artifacts")
    return RuntimeHarness(
        work_engine=work_engine,
        transfer=PostgresArtifactTransfer(engine, object_store, clock=lambda: _NOW),
        s3=s3,
        run_id=run_id,
        acquisition_stage_id=acquisition_stage_id,
        source_key=source_key,
        worker_id=worker_id,
        worker_build=worker_build,
        output_contract=output_contract,
        label=label,
    )


def _enqueue_acquisition(harness: RuntimeHarness, suffix: str) -> WorkUnitSpec:
    command = WorkUnitSpec(
        work_id=uuid4(),
        run_id=harness.run_id,
        stage_run_id=harness.acquisition_stage_id,
        stage=WorkStage.ACQUISITION,
        capability=WorkCapability.HTTP_FETCH,
        source_key=harness.source_key,
        semantic_key=_digest(harness.label, suffix, "semantic"),
        input_digest=_digest(harness.label, suffix, "input"),
        expected_output_contract=harness.output_contract,
        priority=0,
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_delay_seconds=2,
            multiplier=2,
            max_delay_seconds=60,
        ),
        available_at_utc=_NOW,
        correlation_id=f"correlation-{harness.label}-{suffix}",
    )
    harness.work_engine.enqueue_work(command)
    return command


def _lease(harness: RuntimeHarness):
    lease = harness.work_engine.acquire_lease(
        LeaseRequest(
            worker_id=harness.worker_id,
            capability=WorkCapability.HTTP_FETCH,
            lease_duration_seconds=300,
            heartbeat_interval_seconds=60,
            correlation_id=f"correlation-{harness.label}",
        )
    )
    assert lease is not None
    return lease


def _prepare_and_verify(harness: RuntimeHarness, lease, upload_id: UUID) -> None:
    harness.transfer.prepare_upload(
        PrepareArtifactUpload(
            upload_id=upload_id,
            work_id=lease.work_id,
            lease_id=lease.lease_id,
            lease_token=lease.lease_token,
            worker_id=harness.worker_id,
            input_digest=lease.input_digest,
            artifact_kind=ArtifactKind.RAW_ARTIFACT,
            expected_digest=_DIGEST,
            expected_size_bytes=len(_BODY),
            content_type=_CONTENT_TYPE,
            expires_in_seconds=300,
            correlation_id=f"correlation-{harness.label}",
        )
    )
    harness.s3.upload_prepared_body()
    harness.transfer.verify_upload(
        VerifyArtifactUpload(
            upload_id=upload_id,
            work_id=lease.work_id,
            lease_id=lease.lease_id,
            lease_token=lease.lease_token,
            worker_id=harness.worker_id,
            input_digest=lease.input_digest,
            correlation_id=f"correlation-{harness.label}",
        )
    )


def _complete(harness: RuntimeHarness, lease, upload_id: UUID):
    command = WorkCompletion(
        work_id=lease.work_id,
        lease_id=lease.lease_id,
        lease_token=lease.lease_token,
        worker_id=harness.worker_id,
        input_digest=lease.input_digest,
        output_contract=harness.output_contract,
        output_digest=_digest(harness.label, str(lease.work_id), "output"),
        worker_build_identity=harness.worker_build,
        correlation_id=f"correlation-{harness.label}",
        output_artifacts=(WorkOutputArtifact(upload_id=upload_id, role="response_body"),),
    )
    return command, harness.work_engine.complete(command)


def test_verified_output_is_committed_atomically_and_becomes_scoped_input(
    engine: Engine,
) -> None:
    harness = _harness(engine, _label("artifact-flow"))
    _enqueue_acquisition(harness, "first")
    lease = _lease(harness)
    upload_id = uuid4()
    _prepare_and_verify(harness, lease, upload_id)

    completion, applied = _complete(harness, lease, upload_id)
    repeated = harness.work_engine.complete(completion)

    assert applied.status is WorkCompletionStatus.APPLIED
    assert repeated.status is WorkCompletionStatus.ALREADY_APPLIED
    with engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    """
                SELECT
                    upload.state,
                    record.artifact_id,
                    binding.role,
                    object.content_digest
                FROM sources.artifact_uploads AS upload
                JOIN sources.artifact_records AS record
                  ON record.upload_id = upload.upload_id
                JOIN work.work_output_artifacts AS binding
                  ON binding.artifact_id = record.artifact_id
                JOIN sources.artifact_objects AS object
                  ON object.object_id = record.object_id
                WHERE upload.upload_id = :upload_id
                """
                ),
                {"upload_id": upload_id},
            )
            .mappings()
            .one()
        )
    assert row["state"] == "consumed"
    assert row["role"] == "response_body"
    assert row["content_digest"] == _DIGEST
    artifact_id = UUID(str(row["artifact_id"]))

    extraction_stage_id = uuid4()
    harness.work_engine.create_stage(
        StageRunSpec(
            stage_run_id=extraction_stage_id,
            run_id=harness.run_id,
            stage=WorkStage.EXTRACTION,
            initial_state=StageRunState.RUNNING,
            correlation_id=f"correlation-{harness.label}-extraction",
        )
    )
    extraction_worker_id = f"extractor-{harness.label[-20:]}"
    extraction_build = f"extractor-build-{harness.label[-16:]}"
    extraction_contract = f"extract-output-{harness.label[-16:]}"
    harness.work_engine.register_worker(
        WorkerRegistration(
            worker_id=extraction_worker_id,
            build_identity=extraction_build,
            capabilities=frozenset({WorkCapability.EXTRACTION}),
            supported_output_contracts=frozenset({extraction_contract}),
            max_concurrency=1,
            resource_profile="integration-test",
            correlation_id=f"correlation-{harness.label}-extraction",
        )
    )
    downstream = WorkUnitSpec(
        work_id=uuid4(),
        run_id=harness.run_id,
        stage_run_id=extraction_stage_id,
        stage=WorkStage.EXTRACTION,
        capability=WorkCapability.EXTRACTION,
        source_key=None,
        semantic_key=_digest(harness.label, "extraction", "semantic"),
        input_digest=_digest(harness.label, "extraction", "input"),
        expected_output_contract=extraction_contract,
        priority=0,
        retry_policy=RetryPolicy(
            max_attempts=1,
            initial_delay_seconds=2,
            multiplier=2,
            max_delay_seconds=60,
        ),
        available_at_utc=_NOW,
        correlation_id=f"correlation-{harness.label}-extraction",
        input_artifacts=(WorkInputArtifact(artifact_id=artifact_id, role="source_document"),),
    )
    harness.work_engine.enqueue_work(downstream)
    downstream_lease = harness.work_engine.acquire_lease(
        LeaseRequest(
            worker_id=extraction_worker_id,
            capability=WorkCapability.EXTRACTION,
            lease_duration_seconds=300,
            heartbeat_interval_seconds=60,
            correlation_id=f"correlation-{harness.label}-extraction",
        )
    )
    assert downstream_lease is not None
    assert downstream_lease.input_artifacts == (
        WorkInputArtifact(artifact_id=artifact_id, role="source_document"),
    )

    prepared_read = harness.transfer.prepare_read(
        PrepareArtifactRead(
            artifact_id=artifact_id,
            work_id=downstream_lease.work_id,
            lease_id=downstream_lease.lease_id,
            lease_token=downstream_lease.lease_token,
            worker_id=extraction_worker_id,
            input_digest=downstream_lease.input_digest,
            expires_in_seconds=120,
            correlation_id=f"correlation-{harness.label}-extraction",
        )
    )
    assert prepared_read.url.endswith(str(row["content_digest"]).removeprefix("sha256:"))

    with pytest.raises(ArtifactTransferConflict) as forbidden:
        harness.transfer.prepare_read(
            PrepareArtifactRead(
                artifact_id=uuid4(),
                work_id=downstream_lease.work_id,
                lease_id=downstream_lease.lease_id,
                lease_token=downstream_lease.lease_token,
                worker_id=extraction_worker_id,
                input_digest=downstream_lease.input_digest,
                expires_in_seconds=120,
                correlation_id=f"correlation-{harness.label}-forbidden-read",
            )
        )
    assert forbidden.value.code == "ARTIFACT_READ_FORBIDDEN"


def test_unverified_output_cannot_partially_complete_work(engine: Engine) -> None:
    harness = _harness(engine, _label("artifact-unverified"))
    work = _enqueue_acquisition(harness, "first")
    lease = _lease(harness)
    upload_id = uuid4()
    harness.transfer.prepare_upload(
        PrepareArtifactUpload(
            upload_id=upload_id,
            work_id=lease.work_id,
            lease_id=lease.lease_id,
            lease_token=lease.lease_token,
            worker_id=harness.worker_id,
            input_digest=lease.input_digest,
            artifact_kind=ArtifactKind.RAW_ARTIFACT,
            expected_digest=_DIGEST,
            expected_size_bytes=len(_BODY),
            content_type=_CONTENT_TYPE,
            expires_in_seconds=300,
            correlation_id=f"correlation-{harness.label}",
        )
    )

    with pytest.raises(WorkEngineConflict) as raised:
        _complete(harness, lease, upload_id)
    assert raised.value.code == "WORK_OUTPUT_ARTIFACT_NOT_VERIFIED"

    with engine.connect() as connection:
        state = connection.execute(
            sa.text("SELECT state FROM work.work_units WHERE work_id = :work_id"),
            {"work_id": work.work_id},
        ).scalar_one()
        attempt_outcome = connection.execute(
            sa.text("SELECT outcome FROM work.work_attempts WHERE work_id = :work_id"),
            {"work_id": work.work_id},
        ).scalar_one()
        upload_state = connection.execute(
            sa.text("SELECT state FROM sources.artifact_uploads WHERE upload_id = :upload_id"),
            {"upload_id": upload_id},
        ).scalar_one()
        record_count = connection.execute(
            sa.text("SELECT count(*) FROM sources.artifact_records WHERE work_id = :work_id"),
            {"work_id": work.work_id},
        ).scalar_one()
        binding_count = connection.execute(
            sa.text("SELECT count(*) FROM work.work_output_artifacts WHERE work_id = :work_id"),
            {"work_id": work.work_id},
        ).scalar_one()
    assert state == "leased"
    assert attempt_outcome == "leased"
    assert upload_state == "prepared"
    assert record_count == 0
    assert binding_count == 0


def test_duplicate_content_reuses_object_and_preserves_distinct_records(engine: Engine) -> None:
    harness = _harness(engine, _label("artifact-dedup"))
    upload_ids: list[UUID] = []
    for suffix in ("first", "second"):
        _enqueue_acquisition(harness, suffix)
        lease = _lease(harness)
        upload_id = uuid4()
        upload_ids.append(upload_id)
        _prepare_and_verify(harness, lease, upload_id)
        _complete(harness, lease, upload_id)

    with engine.connect() as connection:
        object_count = connection.execute(
            sa.text(
                """
                SELECT count(*)
                FROM sources.artifact_objects
                WHERE artifact_kind = 'raw_artifact'
                  AND content_digest = :content_digest
                """
            ),
            {"content_digest": _DIGEST},
        ).scalar_one()
        record_count = connection.execute(
            sa.text(
                """
                SELECT count(*)
                FROM sources.artifact_records
                WHERE upload_id IN (:first_upload_id, :second_upload_id)
                """
            ),
            {
                "first_upload_id": upload_ids[0],
                "second_upload_id": upload_ids[1],
            },
        ).scalar_one()
    assert object_count == 1
    assert record_count == 2
    assert harness.s3.copy_count == 1
