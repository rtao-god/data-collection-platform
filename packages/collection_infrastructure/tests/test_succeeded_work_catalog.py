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

from collection_application import CampaignSnapshotService
from collection_application.canonicalization import sha256_json
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineAdvancementConflict,
    SucceededWorkOutput,
)
from collection_contracts import (
    NormalizationProfile,
    NormalizationProfilesDocument,
    ObservationBatchPayload,
    SourceBindingsDocument,
    SourcePolicy,
    seal_observation_batch,
)
from collection_domain import WorkStage
from collection_infrastructure.filesystem_campaign_source import FilesystemCampaignBundleSource
from collection_infrastructure.postgres.pipeline_advancement_metadata import (
    pipeline_advancements,
)
from collection_infrastructure.postgres.succeeded_work_catalog import (
    PostgresSucceededWorkCatalog,
    _verify_official_http_bindings,
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

    def scalars(self) -> Iterator[object]:
        return iter(self._rows)

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
) -> dict[str, object]:
    return {
        "binding_position": 0,
        "binding_role": role,
        "artifact_id": artifact_id,
        "content_digest": digest,
        "size_bytes": 128,
        "content_type": "application/json",
    }


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
                        digest=_OUTPUT_DIGEST,
                    ),
                )
            ),
            _Result((_run(),)),
        )
    )

    result = PostgresSucceededWorkCatalog(object()).read(connection, _WORK_ID)  # type: ignore[arg-type]

    assert result.source_work_unit_id == _WORK_ID
    assert result.run_id == _RUN_ID
    assert result.stage_run_id == _STAGE_RUN_ID
    assert result.stage.value == "discovery"
    assert result.capability == "manual_import"
    assert result.output_contract == "manual-import-plan@1"
    assert result.output_digest == _OUTPUT_DIGEST
    assert result.source_binding_key == "manual_seed_import"
    assert result.output_artifact.artifact_id == _OUTPUT_ARTIFACT_ID
    assert result.output_artifact.role == "manual_import_plan"
    assert tuple(item.artifact_id for item in result.input_artifacts) == (_INPUT_ARTIFACT_ID,)
    connection.assert_consumed()


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
                        digest=_OUTPUT_DIGEST,
                    ),
                )
            ),
            _Result((_run(),)),
        )
    )

    result = PostgresSucceededWorkCatalog(object()).read(  # type: ignore[arg-type]
        connection,
        _WORK_ID,
    )

    assert result.output_digest == semantic_digest
    assert result.output_artifact.content_digest == _OUTPUT_DIGEST
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
