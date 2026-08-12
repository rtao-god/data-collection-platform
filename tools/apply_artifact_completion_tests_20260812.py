from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def write(relative: str, content: str) -> None:
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def patch_unit_proof() -> None:
    relative = "packages/collection_infrastructure/tests/test_artifact_metadata.py"
    text = read(relative)
    if "test_raw_artifact_ddl_enforces_upload_and_attempt_lineage" not in text:
        text += '''

def test_raw_artifact_ddl_enforces_upload_and_attempt_lineage() -> None:
    sql = str(CreateTable(raw_artifacts).compile(dialect=postgresql.dialect()))

    assert "CONSTRAINT fk_raw_artifacts_upload_lineage" in sql
    assert "CONSTRAINT fk_raw_artifacts_attempt_lineage" in sql
    assert "CONSTRAINT ck_raw_artifacts_kind" in sql
'''
    write(relative, text)

    relative = "packages/collection_application/tests/test_work_engine.py"
    text = read(relative)
    if "test_work_completion_rejects_duplicate_output_artifact_roles" not in text:
        text += '''

def test_work_completion_rejects_duplicate_output_artifact_roles() -> None:
    from collection_application import WorkOutputArtifact

    with pytest.raises(ValueError, match="roles must be unique"):
        WorkCompletion(
            work_id=_ID1,
            lease_id=_ID2,
            lease_token=_ID3,
            worker_id="worker-1",
            input_digest=_DIGEST,
            output_contract="output-contract",
            output_digest=_DIGEST,
            worker_build_identity="build-1",
            correlation_id="correlation-1",
            output_artifacts=(
                WorkOutputArtifact(upload_id=_ID1, role="primary"),
                WorkOutputArtifact(upload_id=_ID2, role="primary"),
            ),
        )
'''
    write(relative, text)

    relative = "packages/collection_infrastructure/tests/test_s3_artifact_object_store.py"
    text = read(relative)
    matches = list(re.finditer(r"class (\w+Client):\n", text))
    for match in reversed(matches):
        start = match.end()
        next_class = text.find("\nclass ", start)
        end = len(text) if next_class < 0 else next_class
        block = text[match.start() : end]
        if "def head_bucket(" not in block:
            method = (
                "    def head_bucket(self, **kwargs: object) -> dict[str, object]:\n"
                "        del kwargs\n"
                "        return {}\n\n"
            )
            text = text[:start] + method + text[start:]
    write(relative, text)


def write_integration_proof() -> None:
    write(
        "database/tests/test_artifact_completion_runtime.py",
        '''from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from collection_application import (
    ArtifactKind,
    ArtifactTransferConflict,
    CollectionRunSpec,
    CollectionRunState,
    LeaseRequest,
    PrepareArtifactRead,
    RetryPolicy,
    StageRunSpec,
    StageRunState,
    WorkCapability,
    WorkCompletion,
    WorkCompletionStatus,
    WorkEngineConflict,
    WorkInputArtifact,
    WorkOutputArtifact,
    WorkStage,
    WorkUnitSpec,
    WorkerRegistration,
)
from collection_infrastructure.object_store import S3ArtifactObjectStore
from collection_infrastructure.postgres import PostgresArtifactTransfer, PostgresWorkEngine
from collection_infrastructure.postgres.artifact_metadata import (
    artifact_objects,
    artifact_uploads,
    raw_artifacts,
    work_output_artifacts,
)
from collection_infrastructure.postgres.metadata import (
    config_bundle_components,
    config_bundles,
)
from collection_infrastructure.postgres.work_metadata import work_units

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)


def _database_url() -> str:
    value = os.environ.get("COLLECTOR_DATABASE_URL")
    if not value:
        pytest.skip("COLLECTOR_DATABASE_URL is required for PostgreSQL integration tests")
    return value


def _digest(value: str) -> str:
    return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"


def _publish_ready_bundle(engine: sa.Engine, suffix: str) -> tuple[str, str]:
    bundle_digest = _digest(f"artifact-bundle:{suffix}")
    component_digest = _digest(f"artifact-component:{suffix}")
    campaign_key = f"artifact_integration_{suffix[:12]}"
    with engine.begin() as connection:
        connection.execute(
            sa.insert(config_bundle_components).values(
                bundle_digest=bundle_digest,
                position=0,
                path="campaign.yaml",
                component_digest=component_digest,
            )
        )
        connection.execute(
            sa.insert(config_bundles).values(
                bundle_digest=bundle_digest,
                campaign_key=campaign_key,
                contract="collector-campaign-snapshot",
                contract_revision="campaign-snapshot-v1",
                readiness="ready",
                recorded_at_utc=_NOW,
            )
        )
    return campaign_key, bundle_digest


def _create_running_owner(
    store: PostgresWorkEngine,
    engine: sa.Engine,
    suffix: str,
) -> tuple[UUID, UUID]:
    campaign_key, bundle_digest = _publish_ready_bundle(engine, suffix)
    run_id = uuid4()
    stage_run_id = uuid4()
    store.create_run(
        CollectionRunSpec(
            run_id=run_id,
            campaign_key=campaign_key,
            config_bundle_digest=bundle_digest,
            initial_state=CollectionRunState.RUNNING,
            correlation_id=f"artifact-run-{suffix}",
        )
    )
    store.create_stage(
        StageRunSpec(
            stage_run_id=stage_run_id,
            run_id=run_id,
            stage=WorkStage.EXTRACTION,
            initial_state=StageRunState.RUNNING,
            correlation_id=f"artifact-stage-{suffix}",
        )
    )
    return run_id, stage_run_id


def _enqueue(
    store: PostgresWorkEngine,
    *,
    run_id: UUID,
    stage_run_id: UUID,
    suffix: str,
    output_contract: str,
    input_artifacts: tuple[WorkInputArtifact, ...] = (),
) -> tuple[UUID, str]:
    work_id = uuid4()
    input_digest = _digest(f"artifact-input:{suffix}")
    store.enqueue_work(
        WorkUnitSpec(
            work_id=work_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage=WorkStage.EXTRACTION,
            capability=WorkCapability.EXTRACTION,
            source_key=None,
            semantic_key=_digest(f"artifact-semantic:{suffix}"),
            input_digest=input_digest,
            expected_output_contract=output_contract,
            priority=0,
            retry_policy=RetryPolicy(3, 10, 2, 60),
            available_at_utc=_NOW,
            correlation_id=f"artifact-enqueue-{suffix}",
            input_artifacts=input_artifacts,
        )
    )
    return work_id, input_digest


def _register_worker(store: PostgresWorkEngine, worker_id: str) -> None:
    store.register_worker(
        WorkerRegistration(
            worker_id=worker_id,
            build_identity="artifact-build",
            capabilities=frozenset({WorkCapability.EXTRACTION}),
            supported_output_contracts=frozenset(
                {"artifact-output", "downstream-output", "failure-output"}
            ),
            max_concurrency=3,
            resource_profile="artifact-test",
            correlation_id="artifact-register",
        )
    )


def _acquire(store: PostgresWorkEngine, worker_id: str):
    lease = store.acquire_lease(
        LeaseRequest(
            worker_id=worker_id,
            capability=WorkCapability.EXTRACTION,
            lease_duration_seconds=300,
            heartbeat_interval_seconds=60,
            correlation_id="artifact-acquire",
        )
    )
    assert lease is not None
    return lease


def _insert_verified_uploads(
    engine: sa.Engine,
    lease,
    uploads: tuple[tuple[UUID, str, str], ...],
) -> None:
    with engine.begin() as connection:
        for upload_id, digest, suffix in uploads:
            connection.execute(
                sa.insert(artifact_uploads).values(
                    upload_id=upload_id,
                    work_id=lease.work_id,
                    lease_id=lease.lease_id,
                    lease_token=lease.lease_token,
                    worker_id=lease.worker_id,
                    input_digest=lease.input_digest,
                    artifact_kind=ArtifactKind.RAW_ARTIFACT.value,
                    expected_digest=digest,
                    expected_size_bytes=128,
                    content_type="application/json",
                    staging_reference=f"raw-artifacts/staging/{upload_id}",
                    final_reference=f"raw-artifacts/sha256/{digest[-64:]}",
                    state="verified",
                    prepared_at_utc=_NOW,
                    expires_at_utc=_NOW.replace(hour=7),
                    verified_at_utc=_NOW,
                    consumed_at_utc=None,
                    revision=1,
                    correlation_id=f"artifact-verified-{suffix}",
                )
            )


class _ReadOnlyS3Client:
    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: Mapping[str, object],
        ExpiresIn: int,
        HttpMethod: str | None = None,
    ) -> str:
        del Params, ExpiresIn
        assert client_method == "get_object"
        assert HttpMethod == "GET"
        return "https://objects.example.test/scoped-read"

    def head_bucket(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {}

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError(f"unexpected get_object: {kwargs}")

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError(f"unexpected head_object: {kwargs}")

    def copy_object(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError(f"unexpected copy_object: {kwargs}")

    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError(f"unexpected delete_object: {kwargs}")


def test_verified_outputs_complete_atomically_and_inputs_are_scoped() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)
    suffix = uuid4().hex
    store = PostgresWorkEngine(engine, clock=lambda: _NOW)
    run_id, stage_run_id = _create_running_owner(store, engine, suffix)
    worker_id = f"artifact-worker-{suffix[:12]}"
    _register_worker(store, worker_id)

    work_id, input_digest = _enqueue(
        store,
        run_id=run_id,
        stage_run_id=stage_run_id,
        suffix=f"{suffix}-producer",
        output_contract="artifact-output",
    )
    lease = _acquire(store, worker_id)
    assert lease.work_id == work_id
    assert lease.input_artifacts == ()

    shared_digest = _digest(f"shared-object:{suffix}")
    first_upload = uuid4()
    second_upload = uuid4()
    _insert_verified_uploads(
        engine,
        lease,
        (
            (first_upload, shared_digest, "first"),
            (second_upload, shared_digest, "second"),
        ),
    )
    command = WorkCompletion(
        work_id=work_id,
        lease_id=lease.lease_id,
        lease_token=lease.lease_token,
        worker_id=worker_id,
        input_digest=input_digest,
        output_contract="artifact-output",
        output_digest=_digest(f"artifact-result:{suffix}"),
        worker_build_identity="artifact-build",
        correlation_id="artifact-complete",
        output_artifacts=(
            WorkOutputArtifact(upload_id=first_upload, role="primary"),
            WorkOutputArtifact(upload_id=second_upload, role="secondary"),
        ),
    )

    result = store.complete(command)
    replay = store.complete(command)
    assert result.status is WorkCompletionStatus.APPLIED
    assert replay.status is WorkCompletionStatus.ALREADY_APPLIED

    with engine.connect() as connection:
        object_count = connection.scalar(
            sa.select(sa.func.count()).select_from(artifact_objects).where(
                artifact_objects.c.content_digest == shared_digest
            )
        )
        artifact_rows = connection.execute(
            sa.select(raw_artifacts.c.artifact_id).where(
                raw_artifacts.c.work_id == work_id
            )
        ).all()
        output_rows = connection.execute(
            sa.select(
                work_output_artifacts.c.position,
                work_output_artifacts.c.artifact_id,
                work_output_artifacts.c.role,
            )
            .where(work_output_artifacts.c.work_id == work_id)
            .order_by(work_output_artifacts.c.position)
        ).mappings().all()
        upload_states = tuple(
            connection.execute(
                sa.select(artifact_uploads.c.state)
                .where(artifact_uploads.c.upload_id.in_((first_upload, second_upload)))
                .order_by(artifact_uploads.c.upload_id)
            ).scalars()
        )
    assert object_count == 1
    assert len(artifact_rows) == 2
    assert tuple(row["role"] for row in output_rows) == ("primary", "secondary")
    assert upload_states == ("consumed", "consumed")

    primary_artifact_id = UUID(
        str(next(row["artifact_id"] for row in output_rows if row["role"] == "primary"))
    )
    secondary_artifact_id = UUID(
        str(next(row["artifact_id"] for row in output_rows if row["role"] == "secondary"))
    )
    downstream_id, downstream_digest = _enqueue(
        store,
        run_id=run_id,
        stage_run_id=stage_run_id,
        suffix=f"{suffix}-consumer",
        output_contract="downstream-output",
        input_artifacts=(WorkInputArtifact(primary_artifact_id, "source_document"),),
    )
    downstream_lease = _acquire(store, worker_id)
    assert downstream_lease.work_id == downstream_id
    assert downstream_lease.input_artifacts == (
        WorkInputArtifact(primary_artifact_id, "source_document"),
    )

    transfer = PostgresArtifactTransfer(
        engine,
        S3ArtifactObjectStore(_ReadOnlyS3Client(), bucket="integration-artifacts"),
        clock=lambda: _NOW,
    )
    prepared = transfer.prepare_read(
        PrepareArtifactRead(
            artifact_id=primary_artifact_id,
            work_id=downstream_id,
            lease_id=downstream_lease.lease_id,
            lease_token=downstream_lease.lease_token,
            worker_id=worker_id,
            input_digest=downstream_digest,
            expires_in_seconds=300,
            correlation_id="artifact-read-authorized",
        )
    )
    assert prepared.url == "https://objects.example.test/scoped-read"

    with pytest.raises(ArtifactTransferConflict) as raised:
        transfer.prepare_read(
            PrepareArtifactRead(
                artifact_id=secondary_artifact_id,
                work_id=downstream_id,
                lease_id=downstream_lease.lease_id,
                lease_token=downstream_lease.lease_token,
                worker_id=worker_id,
                input_digest=downstream_digest,
                expires_in_seconds=300,
                correlation_id="artifact-read-forbidden",
            )
        )
    assert raised.value.code == "ARTIFACT_READ_FORBIDDEN"


def test_missing_verified_output_rolls_back_entire_completion() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)
    suffix = uuid4().hex
    store = PostgresWorkEngine(engine, clock=lambda: _NOW)
    run_id, stage_run_id = _create_running_owner(store, engine, suffix)
    worker_id = f"artifact-rollback-{suffix[:12]}"
    _register_worker(store, worker_id)
    work_id, input_digest = _enqueue(
        store,
        run_id=run_id,
        stage_run_id=stage_run_id,
        suffix=f"{suffix}-rollback",
        output_contract="failure-output",
    )
    lease = _acquire(store, worker_id)
    valid_upload = uuid4()
    missing_upload = uuid4()
    _insert_verified_uploads(
        engine,
        lease,
        ((valid_upload, _digest(f"rollback-object:{suffix}"), "valid"),),
    )

    with pytest.raises(WorkEngineConflict) as raised:
        store.complete(
            WorkCompletion(
                work_id=work_id,
                lease_id=lease.lease_id,
                lease_token=lease.lease_token,
                worker_id=worker_id,
                input_digest=input_digest,
                output_contract="failure-output",
                output_digest=_digest(f"rollback-result:{suffix}"),
                worker_build_identity="artifact-build",
                correlation_id="artifact-rollback",
                output_artifacts=(
                    WorkOutputArtifact(valid_upload, "primary"),
                    WorkOutputArtifact(missing_upload, "secondary"),
                ),
            )
        )
    assert raised.value.code == "ARTIFACT_UPLOAD_NOT_VERIFIED"

    with engine.connect() as connection:
        state = connection.scalar(
            sa.select(work_units.c.state).where(work_units.c.work_id == work_id)
        )
        upload_state = connection.scalar(
            sa.select(artifact_uploads.c.state).where(
                artifact_uploads.c.upload_id == valid_upload
            )
        )
        raw_count = connection.scalar(
            sa.select(sa.func.count()).select_from(raw_artifacts).where(
                raw_artifacts.c.work_id == work_id
            )
        )
    assert state == "leased"
    assert upload_state == "verified"
    assert raw_count == 0
''',
    )


def main() -> None:
    patch_unit_proof()
    write_integration_proof()


if __name__ == "__main__":
    main()
