from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.pool import StaticPool

import collection_infrastructure.postgres.succeeded_work_catalog as catalog_module
from collection_application import CampaignSnapshotService
from collection_application.canonicalization import sha256_json
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineAdvancementConflict,
    SucceededWorkOutput,
)
from collection_contracts import (
    ManualImportFormat,
    ManualImportMode,
    ManualRecordLineage,
    NormalizationProfile,
    NormalizationProfilesDocument,
    ObservationBatchPayload,
    SourceBindingsDocument,
    SourcePolicy,
    canonical_manual_record_lineage_bytes,
    seal_observation_batch,
)
from collection_domain import WorkStage
from collection_infrastructure.filesystem_campaign_source import FilesystemCampaignBundleSource
from collection_infrastructure.postgres.pipeline_advancement_metadata import (
    pipeline_advancements,
)
from collection_infrastructure.postgres.succeeded_work_catalog import (
    PostgresSucceededWorkCatalog,
    _BoundArtifact,
    _verify_official_http_bindings,
)
from manual_import_core import (
    build_manual_import_plan,
    canonical_manual_import_plan_json,
    canonical_manual_import_record_json,
    materialize_manual_import_record,
)

_WORK_ID = UUID("00000000-0000-0000-0000-000000000601")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000602")
_STAGE_RUN_ID = UUID("00000000-0000-0000-0000-000000000603")
_INPUT_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000604")
_OUTPUT_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000605")
_INPUT_DIGEST = f"sha256:{'1' * 64}"
_OUTPUT_DIGEST = f"sha256:{'2' * 64}"


class _Result:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> object | None:
        if len(self._rows) > 1:
            raise AssertionError("scripted result contains more than one row")
        return self._rows[0] if self._rows else None

    def all(self) -> list[object]:
        return list(self._rows)

    def scalars(self) -> _Result:
        return self

    def __iter__(self) -> Iterator[object]:
        return iter(self._rows)


class _Connection:
    def __init__(self, results: tuple[_Result, ...]) -> None:
        self._results = list(results)

    def execute(self, _statement: object) -> _Result:
        if not self._results:
            raise AssertionError("unexpected SQL execution")
        return self._results.pop(0)

    def assert_consumed(self) -> None:
        assert not self._results


def _work(*, state: str = "succeeded", output_digest: str = _OUTPUT_DIGEST) -> dict[str, object]:
    return {
        "run_id": _RUN_ID,
        "stage_run_id": _STAGE_RUN_ID,
        "state": state,
        "capability": "manual_import",
        "source_binding_key": "manual_seed_import",
        "output_contract": "manual-import-plan@1",
        "output_digest": output_digest,
    }


def _run() -> dict[str, object]:
    return {
        "config_bundle_digest": f"sha256:{'4' * 64}",
        "campaign_key": "berlin_recording_services",
    }


def _stage() -> dict[str, object]:
    return {
        "run_id": _RUN_ID,
        "stage": "discovery",
    }


def _artifact(
    *,
    artifact_id: UUID,
    role: str,
    digest: str,
    size_bytes: int = 128,
) -> dict[str, object]:
    return {
        "binding_position": 0,
        "binding_role": role,
        "artifact_id": artifact_id,
        "content_digest": digest,
        "size_bytes": size_bytes,
        "content_type": "application/json",
    }


_CATALOG_PLAN_BYTES = canonical_manual_import_plan_json(
    build_manual_import_plan(
        (
            b"expected_entity_kind,display_name,category,website,osm_id,reference_urls,note,"
            b"provenance\n"
            b"place,Studio A,recording_studio,https://studio.example,"
            b"\"https://studio.example/contact\",,Operator reviewed the official site\n"
        ),
        format=ManualImportFormat.CSV,
        mode=ManualImportMode.PARTIAL,
    )
).encode("utf-8")
_CATALOG_PLAN_DIGEST = f"sha256:{hashlib.sha256(_CATALOG_PLAN_BYTES).hexdigest()}"


class _PlanReader:
    """Returns the plan bytes the catalogue is expected to verify."""

    def read_verified(self, **kwargs: object) -> bytes:
        assert kwargs["expected_digest"] == _CATALOG_PLAN_DIGEST
        assert kwargs["expected_size_bytes"] == len(_CATALOG_PLAN_BYTES)
        return _CATALOG_PLAN_BYTES


def test_read_rehydrates_exact_canonical_succeeded_work() -> None:
    connection = _Connection(
        (
            _Result((_work(),)),
            _Result((_stage(),)),
            _Result(
                (
                    _artifact(
                        artifact_id=_INPUT_ARTIFACT_ID,
                        role="manual_source:csv:atomic",
                        digest=_INPUT_DIGEST,
                    ),
                )
            ),
            _Result(
                (
                    _artifact(
                        artifact_id=_OUTPUT_ARTIFACT_ID,
                        role="manual_import_plan",
                        digest=_CATALOG_PLAN_DIGEST,
                        size_bytes=len(_CATALOG_PLAN_BYTES),
                    ),
                )
            ),
            _Result((_run(),)),
            _Result(
                (
                    {
                        "content_type": "application/json",
                        "artifact_kind": "manual_import_plan",
                        "content_digest": _CATALOG_PLAN_DIGEST,
                        "size_bytes": len(_CATALOG_PLAN_BYTES),
                        "storage_reference": "raw-artifacts/test-plan",
                    },
                )
            ),
        )
    )

    result = PostgresSucceededWorkCatalog(
        object(),  # type: ignore[arg-type]
        object_store=_PlanReader(),  # type: ignore[arg-type]
    ).read(connection, _WORK_ID)

    assert result.source_work_unit_id == _WORK_ID
    assert result.run_id == _RUN_ID
    assert result.stage_run_id == _STAGE_RUN_ID
    assert result.stage.value == "discovery"
    assert result.capability == "manual_import"
    assert result.output_contract == "manual-import-plan@1"
    assert result.output_artifact.artifact_id == _OUTPUT_ARTIFACT_ID
    assert result.output_artifact.role == "manual_import_plan"
    assert result.output_artifact.content_digest == _CATALOG_PLAN_DIGEST
    assert tuple(item.artifact_id for item in result.input_artifacts) == (_INPUT_ARTIFACT_ID,)
    # The plan document is part of the output: the admission transition cannot state its
    # child work from an artifact identity alone.
    assert result.output_content == _CATALOG_PLAN_BYTES
    connection.assert_consumed()


def test_read_reports_a_missing_plan_reader_instead_of_inventing_content() -> None:
    connection = _Connection(
        (
            _Result((_work(),)),
            _Result((_stage(),)),
            _Result(
                (
                    _artifact(
                        artifact_id=_INPUT_ARTIFACT_ID,
                        role="manual_source:csv:atomic",
                        digest=_INPUT_DIGEST,
                    ),
                )
            ),
            _Result(
                (
                    _artifact(
                        artifact_id=_OUTPUT_ARTIFACT_ID,
                        role="manual_import_plan",
                        digest=_CATALOG_PLAN_DIGEST,
                        size_bytes=len(_CATALOG_PLAN_BYTES),
                    ),
                )
            ),
            _Result((_run(),)),
        )
    )

    with pytest.raises(PipelineAdvancementConflict) as error:
        PostgresSucceededWorkCatalog(object()).read(connection, _WORK_ID)  # type: ignore[arg-type]

    assert error.value.code == "PIPELINE_ARTIFACT_READER_UNAVAILABLE"


def test_catalog_reads_canonical_bundle_produced_by_snapshot_owner() -> None:
    repository_root = Path(__file__).parents[3]
    compiled = CampaignSnapshotService(
        FilesystemCampaignBundleSource(repository_root / "campaigns")
    ).compile("berlin_recording_services", "snapshot-catalog-test")
    snapshot = compiled.snapshot
    artifact_id = UUID("00000000-0000-0000-0000-000000000699")
    connection = _Connection(
        (
            _Result(
                (
                    {
                        "bundle_digest": snapshot.bundle_digest,
                        "campaign_key": snapshot.campaign_key,
                        "contract": snapshot.contract,
                        "contract_revision": snapshot.contract_revision,
                        "readiness": snapshot.readiness,
                    },
                )
            ),
            _Result(
                tuple(
                    SimpleNamespace(path=item.path, component_digest=item.digest)
                    for item in snapshot.components
                )
            ),
            _Result((artifact_id,)),
            _Result(
                (
                    {
                        "content_type": "application/json",
                        "artifact_kind": "config_bundle",
                        "content_digest": snapshot.bundle_digest,
                        "size_bytes": len(compiled.canonical_content),
                        "storage_reference": "config-bundles/test-bundle",
                    },
                )
            ),
        )
    )

    class VerifiedReader:
        def read_verified(self, **kwargs: object) -> bytes:
            assert kwargs["expected_digest"] == snapshot.bundle_digest
            assert kwargs["expected_size_bytes"] == len(compiled.canonical_content)
            return compiled.canonical_content

    content = PostgresSucceededWorkCatalog(
        object(),
        object_store=VerifiedReader(),  # type: ignore[arg-type]
    )._read_campaign_bundle(
        connection,
        snapshot.bundle_digest,
        _WORK_ID,
        expected_campaign_key=snapshot.campaign_key,
    )

    assert content == compiled.canonical_content
    connection.assert_consumed()


def test_normalization_profile_must_match_exact_run_snapshot_bytes() -> None:
    repository_root = Path(__file__).parents[3]
    compiled = CampaignSnapshotService(
        FilesystemCampaignBundleSource(repository_root / "campaigns")
    ).compile("berlin_recording_services", "normalization-profile-snapshot-test")
    snapshot = json.loads(compiled.canonical_content)
    components = {item["path"]: item["content"] for item in snapshot["components"]}
    bindings = SourceBindingsDocument.model_validate(components["source_bindings.yaml"])
    source_binding = bindings.items[0].model_copy(update={"normalization_profile": "profile_v1"})
    components["source_bindings.yaml"] = bindings.model_copy(
        update={"items": (source_binding,)}
    ).model_dump(mode="json", by_alias=True)
    profile = NormalizationProfile(
        normalizerRevision="normalizer_v1",
        fieldRules=({"sourceField": "district", "targetField": "district", "valueKind": "text"},),
    )
    profiles = NormalizationProfilesDocument(
        schema_revision="normalization-profiles-v1",
        items=({"key": "profile_v1", "profile": profile},),
    )
    components["normalization_profiles.yaml"] = profiles.model_dump(mode="json", by_alias=True)
    snapshot["components"] = [
        {"path": path, "content": content} for path, content in sorted(components.items())
    ]
    snapshot_content = json.dumps(
        snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    policy = SourcePolicy.model_validate(components["source_policies/manual_seed_policy.yaml"])
    batch = seal_observation_batch(
        ObservationBatchPayload(
            sourceRecordId="manual-record-test",
            extractedRecordDigest=f"sha256:{'1' * 64}",
            rawArtifactDigest=f"sha256:{'2' * 64}",
            sourcePolicyDigest=sha256_json(policy.model_dump(mode="json")),
            normalizerRevision=profile.normalizer_revision,
            observations=(),
        )
    )
    batch_content = (
        json.dumps(
            batch.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    canonical_profile = json.dumps(
        profile.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    PostgresSucceededWorkCatalog._verify_normalization_profile_snapshot(
        _WORK_ID,
        snapshot_content=snapshot_content,
        source_binding_key=source_binding.key,
        profile_content=canonical_profile,
        batch_content=batch_content,
    )

    substituted_profile = profile.model_copy(
        update={
            "field_rules": (profile.field_rules[0].model_copy(update={"target_field": "website"}),)
        }
    )
    substituted_content = json.dumps(
        substituted_profile.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert substituted_profile.normalizer_revision == profile.normalizer_revision
    with pytest.raises(PipelineAdvancementConflict) as error:
        PostgresSucceededWorkCatalog._verify_normalization_profile_snapshot(
            _WORK_ID,
            snapshot_content=snapshot_content,
            source_binding_key=source_binding.key,
            profile_content=substituted_content,
            batch_content=batch_content,
        )
    assert error.value.code == "PIPELINE_NORMALIZATION_PROFILE_SNAPSHOT_MISMATCH"


def test_discovery_rotates_past_errors_and_rechecks_corrected_work() -> None:
    broken_ids = tuple(UUID(int=value) for value in range(697, 701))
    good_id = UUID(int=701)
    invalid_work_ids = set(broken_ids)
    base_time = datetime(2026, 8, 19, tzinfo=UTC)
    engine = sa.create_engine("sqlite+pysqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def attach_work_schema(
        dbapi_connection: sqlite3.Connection,
        _connection_record: object,
    ) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS work")

    def insert_work(work_id: UUID, ordinal: int) -> None:
        timestamp = (base_time + timedelta(seconds=ordinal)).strftime("%Y-%m-%d %H:%M:%S.%f")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO work.work_units (work_id, state, updated_at_utc) "
                "VALUES (?, 'succeeded', ?)",
                (work_id.hex, timestamp),
            )

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE work.work_units ("
            "work_id CHAR(32) PRIMARY KEY, state TEXT NOT NULL, updated_at_utc DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE work.pipeline_advancements (source_work_unit_id CHAR(32) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE work.pipeline_advancement_discovery_cursor ("
            "cursor_id INTEGER PRIMARY KEY CHECK (cursor_id = 1), "
            "last_scanned_updated_at_utc DATETIME, last_scanned_work_id CHAR(32), "
            "cycle_max_updated_at_utc DATETIME, cycle_max_work_id CHAR(32), "
            "CHECK ((last_scanned_updated_at_utc IS NULL) = (last_scanned_work_id IS NULL)), "
            "CHECK ((cycle_max_updated_at_utc IS NULL) = (cycle_max_work_id IS NULL)), "
            "CHECK (last_scanned_updated_at_utc IS NULL OR cycle_max_updated_at_utc IS NOT NULL))"
        )
        connection.exec_driver_sql(
            "INSERT INTO work.pipeline_advancement_discovery_cursor "
            "(cursor_id, last_scanned_updated_at_utc, last_scanned_work_id, "
            "cycle_max_updated_at_utc, cycle_max_work_id) VALUES (1, NULL, NULL, NULL, NULL)"
        )

    for ordinal, work_id in enumerate((*broken_ids, good_id), start=1):
        insert_work(work_id, ordinal)

    class DiscoveryCatalog(PostgresSucceededWorkCatalog):
        def read(self, _connection: object, source_work_unit_id: UUID) -> SucceededWorkOutput:
            if source_work_unit_id in invalid_work_ids:
                raise PipelineAdvancementConflict(
                    code="PIPELINE_SNAPSHOT_ARTIFACT_INVALID",
                    message="The exact campaign snapshot artifact is malformed.",
                    context={"sourceWorkUnitId": str(source_work_unit_id)},
                    required_action="Repair the immutable snapshot through its owner.",
                )
            return SucceededWorkOutput(
                source_work_unit_id=source_work_unit_id,
                run_id=_RUN_ID,
                stage_run_id=_STAGE_RUN_ID,
                stage=WorkStage.DISCOVERY,
                capability="manual_import",
                output_contract="manual-import-plan@1",
                output_digest=_OUTPUT_DIGEST,
                output_artifact=ArtifactIdentity(
                    artifact_id=source_work_unit_id,
                    role="manual_import_plan",
                    content_digest=_OUTPUT_DIGEST,
                    size_bytes=128,
                    content_type="application/json",
                ),
                input_artifacts=(),
            )

    catalog = DiscoveryCatalog(engine)
    try:
        first = catalog.list_unregistered_succeeded(
            limit=2,
            correlation_id="pipeline-discovery-tick-1",
        )
        assert tuple(item.source_work_unit_id for item in first.outputs) == ()
        assert tuple(item.source_work_unit_id for item in first.failures) == broken_ids[:2]
        assert all(item.code == "PIPELINE_SNAPSHOT_ARTIFACT_INVALID" for item in first.failures)

        insert_work(UUID(int=706), 6)
        insert_work(UUID(int=707), 7)
        second = catalog.list_unregistered_succeeded(
            limit=2,
            correlation_id="pipeline-discovery-tick-2",
        )
        assert second.outputs == ()
        assert tuple(item.source_work_unit_id for item in second.failures) == broken_ids[2:]

        insert_work(UUID(int=708), 8)
        insert_work(UUID(int=709), 9)
        third = catalog.list_unregistered_succeeded(
            limit=2,
            correlation_id="pipeline-discovery-tick-3",
        )
        assert tuple(item.source_work_unit_id for item in third.outputs) == (good_id,)
        assert third.failures == ()
        invalid_work_ids.remove(broken_ids[0])
        insert_work(UUID(int=710), 10)
        insert_work(UUID(int=711), 11)
        fourth = catalog.list_unregistered_succeeded(
            limit=2,
            correlation_id="pipeline-discovery-tick-4",
        )
        assert tuple(item.source_work_unit_id for item in fourth.outputs) == (broken_ids[0],)
        assert tuple(item.source_work_unit_id for item in fourth.failures) == (broken_ids[1],)
        assert fourth.failures[0].context["correlationId"] == "pipeline-discovery-tick-4"

        insert_work(UUID(int=712), 12)
        insert_work(UUID(int=713), 13)
        fifth = catalog.list_unregistered_succeeded(
            limit=2,
            correlation_id="pipeline-discovery-tick-5",
        )
        assert tuple(item.source_work_unit_id for item in fifth.failures) == broken_ids[2:]

        insert_work(UUID(int=714), 14)
        insert_work(UUID(int=715), 15)
        sixth = catalog.list_unregistered_succeeded(
            limit=2,
            correlation_id="pipeline-discovery-tick-6",
        )
        assert tuple(item.source_work_unit_id for item in sixth.outputs) == (good_id, UUID(int=706))
        assert sixth.failures == ()
    finally:
        engine.dispose()


def test_read_rejects_work_that_is_not_succeeded() -> None:
    connection = _Connection((_Result((_work(state="pending"),)),))

    with pytest.raises(PipelineAdvancementConflict) as error:
        PostgresSucceededWorkCatalog(object()).read(  # type: ignore[arg-type]
            connection,
            _WORK_ID,
        )

    assert error.value.code == "PIPELINE_SOURCE_WORK_NOT_SUCCEEDED"
    connection.assert_consumed()


def test_read_preserves_distinct_semantic_and_artifact_digests() -> None:
    semantic_digest = f"sha256:{'3' * 64}"
    connection = _Connection(
        (
            _Result((_work(output_digest=semantic_digest),)),
            _Result((_stage(),)),
            _Result(()),
            _Result(
                (
                    _artifact(
                        artifact_id=_OUTPUT_ARTIFACT_ID,
                        role="manual_import_plan",
                        digest=_CATALOG_PLAN_DIGEST,
                        size_bytes=len(_CATALOG_PLAN_BYTES),
                    ),
                )
            ),
            _Result((_run(),)),
            _Result(
                (
                    {
                        "content_type": "application/json",
                        "artifact_kind": "manual_import_plan",
                        "content_digest": _CATALOG_PLAN_DIGEST,
                        "size_bytes": len(_CATALOG_PLAN_BYTES),
                        "storage_reference": "raw-artifacts/test-plan",
                    },
                )
            ),
        )
    )

    result = PostgresSucceededWorkCatalog(
        object(),  # type: ignore[arg-type]
        object_store=_PlanReader(),  # type: ignore[arg-type]
    ).read(connection, _WORK_ID)

    assert result.output_digest == semantic_digest
    assert result.output_artifact.content_digest == _CATALOG_PLAN_DIGEST
    assert result.output_content == _CATALOG_PLAN_BYTES
    connection.assert_consumed()


def test_discovery_limit_is_fail_closed_before_storage_access() -> None:
    catalog = PostgresSucceededWorkCatalog(object())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="between 1 and 1000"):
        catalog.list_unregistered_succeeded(limit=0, correlation_id="pipeline-test")


def test_advancement_metadata_foreign_key_targets_canonical_work_id() -> None:
    foreign_key = next(iter(pipeline_advancements.c.source_work_unit_id.foreign_keys))

    assert foreign_key.target_fullname == "work.work_units.work_id"
    assert foreign_key.ondelete == "RESTRICT"


def test_official_http_catalog_contract_checks_raw_manifest_digest_link() -> None:
    raw_digest = f"sha256:{'7' * 64}"
    manifest_digest = f"sha256:{'8' * 64}"
    manifest = json.dumps(
        {
            "contract": "official-http-acquisition",
            "contractRevision": "official-http-acquisition-v1",
            "requestId": "request-1",
            "sourceKey": "source-1",
            "sourcePolicyDigest": _INPUT_DIGEST,
            "requestKind": "robots",
            "requestedUrl": "https://example.test/robots.txt",
            "finalUrl": "https://example.test/robots.txt",
            "outcome": "fetched",
            "statusCode": 200,
            "responseHeaders": [],
            "remoteIpAddress": "192.0.2.1",
            "encodedSizeBytes": 128,
            "decodedSizeBytes": 128,
            "observedAtUtc": "2026-08-19T00:00:00Z",
            "rawArtifactDigest": raw_digest,
            "reusedArtifactId": None,
            "reusedContentDigest": None,
            "robotsAllowed": True,
            "robotsArtifactId": None,
            "robotsDecisionDigest": _OUTPUT_DIGEST,
            "robotsTargetUrl": "https://example.test/seed",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload = json.dumps(
        {
            "contract": "official-http-acquisition@1",
            "manifestDigest": manifest_digest,
            "rawOrReusedDigest": raw_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    result_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    raw_artifact = ArtifactIdentity(
        artifact_id=_OUTPUT_ARTIFACT_ID,
        role="http_raw_response",
        content_digest=raw_digest,
        size_bytes=128,
        content_type="text/html",
    )

    _verify_official_http_bindings(
        manifest,
        output_digest=result_digest,
        manifest_digest=manifest_digest,
        raw_output=raw_artifact,
        input_artifacts=(),
        source_work_unit_id=_WORK_ID,
    )

    with pytest.raises(PipelineAdvancementConflict) as error:
        _verify_official_http_bindings(
            manifest,
            output_digest=result_digest,
            manifest_digest=manifest_digest,
            raw_output=ArtifactIdentity(
                artifact_id=_OUTPUT_ARTIFACT_ID,
                role="http_raw_response",
                content_digest=_INPUT_DIGEST,
                size_bytes=128,
                content_type="text/html",
            ),
            input_artifacts=(),
            source_work_unit_id=_WORK_ID,
        )
    assert error.value.code == "PIPELINE_HTTP_RAW_MANIFEST_CONFLICT"

    for discriminator in ("contract", "contractRevision"):
        incomplete = json.loads(manifest)
        incomplete.pop(discriminator)
        with pytest.raises(PipelineAdvancementConflict) as missing_error:
            _verify_official_http_bindings(
                json.dumps(incomplete).encode(),
                output_digest=result_digest,
                manifest_digest=manifest_digest,
                raw_output=raw_artifact,
                input_artifacts=(),
                source_work_unit_id=_WORK_ID,
            )
        assert missing_error.value.code == "PIPELINE_HTTP_MANIFEST_INVALID"


def test_catalog_reads_official_http_manifest_and_related_raw_output() -> None:
    raw_id = UUID("00000000-0000-0000-0000-000000000606")
    raw_digest = f"sha256:{'9' * 64}"
    manifest = json.dumps(
        {
            "contract": "official-http-acquisition",
            "contractRevision": "official-http-acquisition-v1",
            "requestId": "request-2",
            "sourceKey": "source-2",
            "sourcePolicyDigest": _INPUT_DIGEST,
            "requestKind": "robots",
            "requestedUrl": "https://example.test/robots.txt",
            "finalUrl": "https://example.test/robots.txt",
            "outcome": "fetched",
            "statusCode": 200,
            "responseHeaders": [],
            "remoteIpAddress": "192.0.2.2",
            "encodedSizeBytes": 128,
            "decodedSizeBytes": 128,
            "observedAtUtc": "2026-08-19T00:00:00Z",
            "rawArtifactDigest": raw_digest,
            "reusedArtifactId": None,
            "reusedContentDigest": None,
            "robotsAllowed": True,
            "robotsArtifactId": None,
            "robotsDecisionDigest": _OUTPUT_DIGEST,
            "robotsTargetUrl": "https://example.test/seed",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_digest = f"sha256:{hashlib.sha256(manifest).hexdigest()}"
    completion_payload = json.dumps(
        {
            "contract": "official-http-acquisition@1",
            "manifestDigest": manifest_digest,
            "rawOrReusedDigest": raw_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    work = _work()
    work.update(
        capability="http_fetch",
        output_contract="official-http-acquisition@1",
        output_digest=f"sha256:{hashlib.sha256(completion_payload).hexdigest()}",
    )
    connection = _Connection(
        (
            _Result((work,)),
            _Result((_stage(),)),
            _Result(
                (
                    {
                        **_artifact(
                            artifact_id=_INPUT_ARTIFACT_ID,
                            role="http_request",
                            digest=_INPUT_DIGEST,
                        ),
                        "content_type": "application/vnd.collection.official-http-request+json",
                    },
                )
            ),
            _Result(
                (
                    {
                        **_artifact(
                            artifact_id=_OUTPUT_ARTIFACT_ID,
                            role="http_acquisition_manifest",
                            digest=manifest_digest,
                        ),
                        "binding_position": 0,
                        "content_type": "application/vnd.collection.official-http-acquisition+json",
                        "size_bytes": len(manifest),
                    },
                    {
                        **_artifact(
                            artifact_id=raw_id,
                            role="http_raw_response",
                            digest=raw_digest,
                        ),
                        "binding_position": 1,
                        "content_type": "text/html",
                    },
                )
            ),
            _Result((_run(),)),
        )
    )

    class ManifestReader(PostgresSucceededWorkCatalog):
        def _read_artifact_content(
            self,
            _connection: object,
            identity: ArtifactIdentity,
            _source_work_unit_id: UUID,
            *,
            maximum_bytes: int,
        ) -> bytes:
            if identity.role == "http_request":
                assert identity.artifact_id == _INPUT_ARTIFACT_ID
                assert maximum_bytes == 1024 * 1024
                return b"{}"
            assert identity.artifact_id == _OUTPUT_ARTIFACT_ID
            assert maximum_bytes == 2 * 1024 * 1024
            return manifest

    result = ManifestReader(object()).read(connection, _WORK_ID)  # type: ignore[arg-type]

    assert result.output_artifact.artifact_id == _OUTPUT_ARTIFACT_ID
    assert result.output_artifact.role == "http_acquisition_manifest"
    assert result.output_content == manifest
    assert result.http_request_content == b"{}"
    assert result.website_route_context_content is None
    assert tuple(item.artifact_id for item in result.related_output_artifacts) == (raw_id,)
    connection.assert_consumed()

    missing_request_connection = _Connection(
        (
            _Result((work,)),
            _Result((_stage(),)),
            _Result(
                (
                    {
                        **_artifact(
                            artifact_id=UUID("00000000-0000-0000-0000-000000000607"),
                            role="website_route_context",
                            digest=_INPUT_DIGEST,
                        ),
                    },
                )
            ),
            _Result(
                (
                    {
                        **_artifact(
                            artifact_id=_OUTPUT_ARTIFACT_ID,
                            role="http_acquisition_manifest",
                            digest=manifest_digest,
                        ),
                        "binding_position": 0,
                        "content_type": "application/vnd.collection.official-http-acquisition+json",
                        "size_bytes": len(manifest),
                    },
                    {
                        **_artifact(
                            artifact_id=raw_id,
                            role="http_raw_response",
                            digest=raw_digest,
                        ),
                        "binding_position": 1,
                        "content_type": "text/html",
                    },
                )
            ),
        )
    )
    with pytest.raises(PipelineAdvancementConflict) as error:
        ManifestReader(object()).read(missing_request_connection, _WORK_ID)  # type: ignore[arg-type]
    assert error.value.code == "PIPELINE_WEBSITE_ROUTE_BINDINGS_INVALID"


def test_normalization_output_requires_and_accepts_exact_lineage_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = NormalizationProfile(
        normalizerRevision="normalizer_v1",
        fieldRules=({"sourceField": "district", "targetField": "district", "valueKind": "text"},),
    )
    profile_content = json.dumps(
        profile.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    batch_content = b"canonical-batch"
    batch_digest = "sha256:" + "7" * 64
    extracted_digest = "sha256:" + "8" * 64
    lineage = ManualRecordLineage(
        runId=_RUN_ID,
        manualRecordWorkId=UUID(int=610),
        manualRecordArtifactId=UUID(int=611),
        manualRecordArtifactDigest="sha256:" + "1" * 64,
        recordDigest="sha256:" + "2" * 64,
        expectedEntityKind="place",
        manualRecordContentDigest="sha256:" + "3" * 64,
        sourceArtifactId=UUID(int=612),
        sourceArtifactDigest="sha256:" + "4" * 64,
    )
    monkeypatch.setattr(
        catalog_module,
        "decode_observation_batch",
        lambda _content: SimpleNamespace(
            content_digest=batch_digest,
            source_record_id=lineage.manual_record_content_digest,
            extracted_record_digest=extracted_digest,
            raw_artifact_digest="sha256:" + "9" * 64,
            source_policy_digest=_INPUT_DIGEST,
            normalizer_revision="normalizer_v1",
        ),
    )
    monkeypatch.setattr(
        catalog_module,
        "canonical_observation_batch_json",
        lambda _batch: batch_content.decode(),
    )
    monkeypatch.setattr(
        catalog_module,
        "decode_extracted_record",
        lambda _content: SimpleNamespace(
            source_record_id=lineage.manual_record_content_digest,
            content_digest=extracted_digest,
            raw_artifact_digest="sha256:" + "9" * 64,
            source_policy_digest=_INPUT_DIGEST,
        ),
    )

    identities = tuple(
        ArtifactIdentity(
            artifact_id=UUID(int=index),
            role=role,
            content_digest=f"sha256:{index:064x}",
            size_bytes=1,
            content_type=content_type,
        )
        for index, (role, content_type) in enumerate(
            (
                ("extracted_record", "application/vnd.collection.extracted-record+json"),
                ("normalization_profile", "application/vnd.collection.normalization-profile+json"),
                (
                    "manual_record_lineage",
                    "application/vnd.collection.manual-record-lineage+json",
                ),
            ),
            start=1,
        )
    )
    output = ArtifactIdentity(
        artifact_id=UUID(int=10),
        role="field_observation_batch",
        content_digest=batch_digest,
        size_bytes=len(batch_content),
        content_type="application/vnd.collection.field-observation-batch+json",
    )
    bound = tuple(
        _BoundArtifact(position=index, artifact=item) for index, item in enumerate(identities)
    )

    class ArtifactReader(PostgresSucceededWorkCatalog):
        def _read_artifact_content(
            self,
            _connection: object,
            identity: ArtifactIdentity,
            _source_work_unit_id: UUID,
            *,
            maximum_bytes: int,
        ) -> bytes:
            assert maximum_bytes > 0
            if identity.role == "normalization_profile":
                return profile_content
            if identity.role == "field_observation_batch":
                return batch_content
            if identity.role == "extracted_record":
                return b"extracted"
            raise AssertionError("lineage content is read by catalog.read before this method")

    catalog = ArtifactReader(object())  # type: ignore[arg-type]
    read_output = catalog._read_normalization_output(
        object(),
        _WORK_ID,
        outputs=(_BoundArtifact(0, output),),
        inputs=bound,
        manual_record_lineage=lineage,
        output_digest=batch_digest,
    )
    assert read_output == (output, batch_content, profile_content)

    with pytest.raises(PipelineAdvancementConflict) as lineage_error:
        catalog._read_normalization_output(
            object(),
            _WORK_ID,
            outputs=(_BoundArtifact(0, output),),
            inputs=bound,
            manual_record_lineage=lineage.model_copy(
                update={"manual_record_content_digest": "sha256:" + "5" * 64}
            ),
            output_digest=batch_digest,
        )
    assert lineage_error.value.code == "PIPELINE_MANUAL_RECORD_LINEAGE_PROVENANCE_INVALID"
    assert lineage_error.value.context["reason"] == "observation_lineage_source_record_mismatch"

    for invalid_inputs in (bound[:-1], (*bound, bound[0])):
        with pytest.raises(PipelineAdvancementConflict) as error:
            catalog._read_normalization_output(
                object(),
                _WORK_ID,
                outputs=(_BoundArtifact(0, output),),
                inputs=invalid_inputs,
                output_digest=batch_digest,
                manual_record_lineage=lineage,
            )
        assert error.value.code == "PIPELINE_NORMALIZATION_ARTIFACT_BINDINGS_INVALID"


def test_manual_record_lineage_resolves_exact_work_plan_and_source_bindings() -> None:
    source_content = json.dumps(
        [
            {
                "expected_entity_kind": "place",
                "display_name": "Lineage Studio",
                "category": "recording_studio",
                "website": "https://studio.example",
                "osm_id": None,
                "reference_urls": [],
                "note": None,
                "provenance": "operator fixture",
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    source_digest = "sha256:" + hashlib.sha256(source_content).hexdigest()
    source_artifact_id = UUID(int=620)
    manual_work_id = UUID(int=621)
    plan_work_id = UUID(int=622)
    plan_artifact_id = UUID(int=623)
    record_artifact_id = UUID(int=624)
    manual_stage_run_id = UUID(int=625)
    plan_stage_run_id = UUID(int=626)
    source_role = "manual_import_source:json:atomic"
    plan = build_manual_import_plan(
        source_content,
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )
    plan_content = canonical_manual_import_plan_json(plan).encode("utf-8")
    record = materialize_manual_import_record(
        plan_content,
        source_artifact_role=source_role,
        plan_record_position=0,
    )
    record_content = canonical_manual_import_record_json(record).encode("utf-8")
    record_digest = "sha256:" + hashlib.sha256(record_content).hexdigest()
    lineage = ManualRecordLineage(
        runId=_RUN_ID,
        manualRecordWorkId=manual_work_id,
        manualRecordArtifactId=record_artifact_id,
        manualRecordArtifactDigest=record_digest,
        recordDigest=record.record_digest,
        expectedEntityKind=record.record.expected_entity_kind,
        manualRecordContentDigest=record.content_digest,
        sourceArtifactId=source_artifact_id,
        sourceArtifactDigest=source_digest,
    )
    lineage_content = canonical_manual_record_lineage_bytes(lineage)
    lineage_artifact = ArtifactIdentity(
        artifact_id=UUID(int=627),
        role="manual_record_lineage",
        content_digest="sha256:" + hashlib.sha256(lineage_content).hexdigest(),
        size_bytes=len(lineage_content),
        content_type="application/vnd.collection.manual-record-lineage+json",
    )

    def binding(
        *,
        position: int,
        artifact_id: UUID,
        role: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, object]:
        return {
            "binding_position": position,
            "binding_role": role,
            "artifact_id": artifact_id,
            "content_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "content_type": content_type,
        }

    source_type = "application/json"
    plan_type = "application/vnd.collection.manual-import-plan+json"
    record_type = "application/vnd.collection.manual-import-record+json"
    source_binding = binding(
        position=0,
        artifact_id=source_artifact_id,
        role=source_role,
        content=source_content,
        content_type=source_type,
    )
    plan_binding = binding(
        position=1,
        artifact_id=plan_artifact_id,
        role="manual_import_plan_record:0",
        content=plan_content,
        content_type=plan_type,
    )
    record_output = binding(
        position=0,
        artifact_id=record_artifact_id,
        role="manual_import_record",
        content=record_content,
        content_type=record_type,
    )
    plan_output = binding(
        position=0,
        artifact_id=plan_artifact_id,
        role="manual_import_plan",
        content=plan_content,
        content_type=plan_type,
    )
    manual_work = {
        "run_id": _RUN_ID,
        "stage_run_id": manual_stage_run_id,
        "state": "succeeded",
        "stage": WorkStage.DISCOVERY.value,
        "capability": "manual_record",
        "output_contract": "manual-import-record@1",
        "output_digest": record.content_digest,
    }
    plan_work = {
        "run_id": _RUN_ID,
        "stage_run_id": plan_stage_run_id,
        "state": "succeeded",
        "stage": WorkStage.DISCOVERY.value,
        "capability": "manual_import",
        "output_contract": "manual-import-plan@1",
        "output_digest": plan.plan_digest,
    }
    connection = _Connection(
        (
            _Result((manual_work,)),
            _Result(({"run_id": _RUN_ID, "stage": WorkStage.DISCOVERY.value},)),
            _Result((record_output,)),
            _Result((source_binding, plan_binding)),
            _Result((manual_work_id,)),
            _Result((plan_work_id,)),
            _Result((plan_work,)),
            _Result(({"run_id": _RUN_ID, "stage": WorkStage.DISCOVERY.value},)),
            _Result((plan_output,)),
            _Result((source_binding,)),
        )
    )

    class ArtifactReader(PostgresSucceededWorkCatalog):
        def _read_artifact_content(
            self,
            _connection: object,
            identity: ArtifactIdentity,
            _source_work_unit_id: UUID,
            *,
            maximum_bytes: int,
        ) -> bytes:
            assert maximum_bytes > 0
            return {
                record_artifact_id: record_content,
                source_artifact_id: source_content,
                plan_artifact_id: plan_content,
            }[identity.artifact_id]

    catalog = ArtifactReader(object())  # type: ignore[arg-type]
    verified = catalog._verify_manual_record_lineage(
        connection,  # type: ignore[arg-type]
        lineage_content=lineage_content,
        lineage_artifact=lineage_artifact,
        run_id=_RUN_ID,
        source_work_unit_id=_WORK_ID,
    )
    assert verified == lineage
    connection.assert_consumed()

    invalid_connection = _Connection(
        (
            _Result((manual_work,)),
            _Result(({"run_id": _RUN_ID, "stage": WorkStage.DISCOVERY.value},)),
            _Result((record_output,)),
            _Result((source_binding, plan_binding)),
            _Result((manual_work_id,)),
            _Result((plan_work_id,)),
            _Result((plan_work,)),
            _Result(({"run_id": _RUN_ID, "stage": WorkStage.DISCOVERY.value},)),
            _Result((plan_output,)),
            _Result(
                (
                    binding(
                        position=0,
                        artifact_id=UUID(int=628),
                        role=source_role,
                        content=source_content,
                        content_type=source_type,
                    ),
                )
            ),
        )
    )
    with pytest.raises(PipelineAdvancementConflict) as error:
        catalog._verify_manual_record_lineage(
            invalid_connection,  # type: ignore[arg-type]
            lineage_content=lineage_content,
            lineage_artifact=lineage_artifact,
            run_id=_RUN_ID,
            source_work_unit_id=_WORK_ID,
        )
    assert error.value.context["reason"] == "manual_import_plan_source_or_output_binding_mismatch"
