"""Source-native candidate intake: provenance retention, rollback, retry, duplicates."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest

from collection_application.canonicalization import canonical_json_bytes, sha256_json
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    CandidateIntakeAction,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    SourceRecordIntakeProvenance,
    SucceededWorkOutput,
    candidate_intake_result_digest,
)
from collection_contracts.observations import (
    GeoPointObservationValue,
    ObservationState,
    SourceRecordBatchPayload,
    SourceRecordEvidence,
    SourceRecordLocatorKind,
    SourceRecordObservationPayload,
    TextObservationValue,
    canonical_source_record_observation_batch_json,
    seal_source_record_observation,
    seal_source_record_observation_batch,
)
from collection_domain import WorkCapability, WorkStage
from collection_infrastructure.postgres.candidate_intake_applier import (
    PostgresCandidateIntakeApplier,
)
from resolution_contracts import GeographyCoverage

_RUN_ID = UUID("00000000-0000-0000-0000-000000000901")
# Bound for the fixture record: a square around the observed point, so the candidate's
# geography is decided by the exact boundary rather than defaulted.
_BOUNDARY = {
    "type": "MultiPolygon",
    "coordinates": [
        [
            [
                [13.30, 52.40],
                [13.50, 52.40],
                [13.50, 52.60],
                [13.30, 52.60],
                [13.30, 52.40],
            ]
        ]
    ],
}
_WORK_ID = UUID("00000000-0000-0000-0000-000000000902")
_RECORD_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000903")
_BATCH_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000904")
_PROFILE_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000905")
_RECORD_DIGEST = "sha256:" + "a" * 64
_POLICY_DIGEST = "sha256:" + "b" * 64
_OBSERVED_AT = datetime(2026, 1, 5, tzinfo=UTC)
_PROFILE = {
    "campaignKey": "berlin_recording_services",
    "entityKind": "place",
    "fieldProjection": {"display_name": "names", "website": "website_urls"},
    "mappingRevision": "osm-overpass-source-record-mapping-v1",
    "runId": str(_RUN_ID),
    "sourceBindingKey": "osm_overpass_berlin",
    "sourceKey": "openstreetmap",
    "sourcePolicyDigest": _POLICY_DIGEST,
}


def _observation(field_key: str, value: str, *, index: int) -> SourceRecordObservationPayload:
    return SourceRecordObservationPayload(
        sourceRecordId="osm:node:101",
        fieldKey=field_key,
        state=ObservationState.OBSERVED,
        value=TextObservationValue(original=value, normalized=value),
        evidence=SourceRecordEvidence(
            rawArtifactId=_RECORD_ARTIFACT_ID,
            rawArtifactDigest=_RECORD_DIGEST,
            sourceUrl="https://www.openstreetmap.org/node/101",
            locatorKind=SourceRecordLocatorKind.COLUMN,
            locatorValue=f"tags.{field_key}",
            evidenceDigest="sha256:" + f"{index}" * 64,
            observedAtUtc=_OBSERVED_AT,
            mappingRevision="osm-overpass-source-record-mapping-v1",
        ),
        sourcePolicyDigest=_POLICY_DIGEST,
        mappingRevision="osm-overpass-source-record-mapping-v1",
        confidence=1.0,
    )


def _point_observation(latitude: str, longitude: str) -> SourceRecordObservationPayload:
    return SourceRecordObservationPayload(
        sourceRecordId="osm:node:101",
        fieldKey="coordinates",
        state=ObservationState.OBSERVED,
        value=GeoPointObservationValue(
            latitude=Decimal(latitude),
            longitude=Decimal(longitude),
            geometrySourceRecordId="osm:node:101",
            geometryLocator="/geometry",
        ),
        evidence=SourceRecordEvidence(
            rawArtifactId=_RECORD_ARTIFACT_ID,
            rawArtifactDigest=_RECORD_DIGEST,
            sourceUrl="https://www.openstreetmap.org/node/101",
            locatorKind=SourceRecordLocatorKind.COLUMN,
            locatorValue="/geometry",
            evidenceDigest="sha256:" + "3" * 64,
            observedAtUtc=_OBSERVED_AT,
            mappingRevision="osm-overpass-source-record-mapping-v1",
        ),
        sourcePolicyDigest=_POLICY_DIGEST,
        mappingRevision="osm-overpass-source-record-mapping-v1",
        confidence=1.0,
    )


def _batch_bytes(point: tuple[str, str] | None = ("52.50", "13.40")) -> bytes:
    batch = seal_source_record_observation_batch(
        SourceRecordBatchPayload(
            runId=_RUN_ID,
            sourceKey="openstreetmap",
            sourceBindingKey="osm_overpass_berlin",
            sourceRecordId="osm:node:101",
            sourceRecordArtifactId=_RECORD_ARTIFACT_ID,
            sourceRecordArtifactDigest=_RECORD_DIGEST,
            datasetRelease="osm-overpass-live-v1",
            sourcePolicyDigest=_POLICY_DIGEST,
            mappingRevision="osm-overpass-source-record-mapping-v1",
            observationProfileDigest="sha256:" + "c" * 64,
            observations=(
                seal_source_record_observation(_observation("display_name", "Studio A", index=1)),
                seal_source_record_observation(
                    _observation("website", "https://a.example", index=2)
                ),
                *(
                    (seal_source_record_observation(_point_observation(*point)),)
                    if point is not None
                    else ()
                ),
            ),
        )
    )
    return canonical_source_record_observation_batch_json(batch).encode("utf-8")


_BATCH_BYTES = _batch_bytes()
_BATCH_DIGEST = "sha256:" + sha256(_BATCH_BYTES).hexdigest()


def _batch_with_point(*, latitude: str, longitude: str) -> bytes:
    return _batch_bytes((latitude, longitude))


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _source(
    *,
    batch_content: bytes | None = _BATCH_BYTES,
    content_digest: str = _BATCH_DIGEST,
) -> SucceededWorkOutput:
    return SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=UUID("00000000-0000-0000-0000-000000000906"),
        stage=WorkStage.NORMALIZATION,
        capability=WorkCapability.SOURCE_RECORD_MAPPING.value,
        output_contract="source-record-observation-batch@1",
        output_digest=content_digest,
        output_artifact=ArtifactIdentity(
            artifact_id=_BATCH_ARTIFACT_ID,
            role="source_record_observation_batch",
            content_digest=content_digest,
            size_bytes=len(_BATCH_BYTES),
            content_type="application/vnd.collection.source-record-observation+json",
        ),
        input_artifacts=(
            ArtifactIdentity(
                artifact_id=_RECORD_ARTIFACT_ID,
                role="source_record",
                content_digest=_RECORD_DIGEST,
                size_bytes=10,
                content_type="application/vnd.collection.osm-source-record+json",
            ),
            ArtifactIdentity(
                artifact_id=_PROFILE_ARTIFACT_ID,
                role="source_mapping_profile",
                content_digest="sha256:" + sha256(_profile_payload()).hexdigest(),
                size_bytes=len(_profile_payload()),
                content_type="application/json",
            ),
        ),
        source_binding_key="osm_overpass_berlin",
        config_bundle_digest="sha256:" + sha256(_campaign_bundle()).hexdigest(),
        campaign_bundle_content=_campaign_bundle(),
        output_content=batch_content,
        normalization_profile_content=_profile_payload(),
    )


def _campaign_bundle_bytes(*, legal_status: str = "approved") -> bytes:
    bundle = json.loads(_campaign_bundle().decode("utf-8"))
    for component in bundle["components"]:
        if component["path"].startswith("source_policies/"):
            component["content"]["legal_status"] = legal_status
            component["digest"] = sha256_json(component["content"])
    return canonical_json_bytes(bundle)


def _campaign_bundle() -> bytes:
    geography = {
        "schema_revision": "geography-config-v1",
        "geography_revision": "berlin_official_boundary_v1",
        "boundary_artifact_path": "geography/berlin-boundary.geojson",
        "boundary_digest": sha256_json(_BOUNDARY),
        "provenance_artifact_path": "geography/berlin-boundary.provenance.json",
        "provenance_digest": "sha256:" + "e" * 64,
    }
    bindings = {
        "schema_revision": "source-bindings-v1",
        "items": [
            {
                "key": "osm_overpass_berlin",
                "source_key": "openstreetmap",
                "connector_key": "osm_overpass",
                "source_policy_key": "osm_test_policy",
                "capability": "openstreetmap_query",
                "seed_provider": {"kind": "not_applicable"},
                "crawl_budget": {"state": "not_applicable"},
                "schedule_enabled": False,
                "extraction_profile": "osm-test-v1",
            }
        ],
    }
    policy = {
        "schema_revision": "source-policy-v1",
        "policy_key": "osm_test_policy",
        "policy_revision": "osm-test-policy-v1",
        "source_key": "openstreetmap",
        "source_type": "openstreetmap",
        # Only an approved policy authorizes redistribution; the fixture states it so
        # intake can seal the export-side policy facts the sealed entry needs.
        "legal_status": "approved",
        "terms_review": {"state": "not_applicable", "reason": "Fixture policy."},
        "access": {
            "kind": "network",
            "allowed_methods": ["GET"],
            "allowed_host_patterns": ["overpass.test"],
            "max_requests_per_second": 1,
            "max_concurrency": 1,
            "connect_timeout_seconds": 5,
            "read_timeout_seconds": 5,
            "max_encoded_bytes": 1048576,
            "max_decoded_bytes": 1048576,
            "content_type_allowlist": ["application/json"],
            "redirect_limit": 0,
        },
        "storage_class": "collector-structured-evidence-v1",
        "retention_days": 30,
        "allowed_fields": ["name", "website"],
        "prohibited_fields": [],
        "attribution_required": True,
        "browser": "forbidden",
        "screenshots": "forbidden",
        "retry_budget": 0,
        "stop_on_status_codes": [],
        "reviewer": "fixture-reviewer",
        "approver": "fixture-approver",
    }
    components = (
        ("geography.yaml", geography),
        ("geography/berlin-boundary.geojson", _BOUNDARY),
        ("source_bindings.yaml", bindings),
        ("source_policies/osm_test_policy.yaml", policy),
    )
    document = {
        "campaignKey": "berlin_recording_services",
        "components": [
            {"path": path, "digest": sha256_json(content), "content": content}
            for path, content in components
        ],
    }
    return canonical_json_bytes(document)


def _profile_payload() -> bytes:
    return json.dumps(_PROFILE, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _plan(provenance_kind: str = "source_record") -> PipelineTransitionPlan:
    action = CandidateIntakeAction(
        candidate_id=UUID("00000000-0000-0000-0000-000000000907"),
        run_id=_RUN_ID,
        source_work_unit_id=_WORK_ID,
        observation_batch_digest=_BATCH_DIGEST,
        provenance=SourceRecordIntakeProvenance(
            source_key="openstreetmap",
            source_binding_key="osm_overpass_berlin",
            campaign_key="berlin_recording_services",
            source_record_id="osm:node:101",
            source_policy_digest=_POLICY_DIGEST,
            dataset_release="osm-overpass-live-v1",
            mapping_revision="osm-overpass-source-record-mapping-v1",
            acquisition_identity="sha256:" + "9" * 64,
            source_record_artifact=ArtifactIdentity(
                artifact_id=_RECORD_ARTIFACT_ID,
                role="source_record",
                content_digest=_RECORD_DIGEST,
                size_bytes=10,
                content_type="application/vnd.collection.osm-source-record+json",
            ),
        ),
        source_artifact_ids=(_RECORD_ARTIFACT_ID, _PROFILE_ARTIFACT_ID),
    )
    plan = PipelineTransitionPlan(
        source_work_unit_id=_WORK_ID,
        transition_key="source-record-observation-batch-candidate-intake-v1",
        disposition=PipelineTransitionDisposition.APPLY,
        plan_digest="sha256:" + "0" * 64,
        blocker=None,
        candidate_intake_actions=(action,),
    )
    return replace(plan, plan_digest=candidate_intake_result_digest(plan))


class _ResultRow:
    """Stands in for a driver result: lookup queries find nothing yet."""

    def one_or_none(self) -> None:
        return None

    def one(self) -> None:
        return None

    def scalar_one_or_none(self) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(())


class _LockedRow:
    """The locked candidate row an existing logical entity resolves to."""

    def __init__(self, entity_kind: str) -> None:
        self._entity_kind = entity_kind

    def one_or_none(self) -> tuple[str]:
        return (self._entity_kind,)


class _Recorder:
    """Minimal connection double: records SQL text instead of executing it."""

    def __init__(self, fail_on: str | None = None, locked_kind: str | None = None) -> None:
        self.statements: list[str] = []
        self._fail_on = fail_on
        self._locked_kind = locked_kind

    def execute(self, statement: Any, parameters: Any = None) -> Any:
        text = str(statement)
        self.statements.append(text)
        if self._fail_on is not None and self._fail_on in text:
            raise RuntimeError("injected intake failure")
        if self._locked_kind is not None and "FOR UPDATE" in text:
            return _LockedRow(self._locked_kind)
        return _ResultRow()


def _patch_intake_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every candidate write so a test can assert intake decisions alone."""

    for name in (
        "_insert_candidate",
        "_insert_revision",
        "_insert_evidence",
        "_insert_review_case",
        "_record_source_entity_revision",
    ):
        monkeypatch.setattr(PostgresCandidateIntakeApplier, name, lambda *a, **k: None)
    monkeypatch.setattr(
        PostgresCandidateIntakeApplier,
        "_source_record_revision",
        lambda *a, **k: (0, True),
    )


def test_source_record_intake_accepts_source_native_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applier = PostgresCandidateIntakeApplier(clock=lambda: _OBSERVED_AT)
    connection = _Recorder()
    captured: dict[str, Any] = {}

    def _materialize(*_: Any, **kwargs: Any) -> tuple[Any, Any]:
        captured.update(kwargs)
        revision = _FakeRevision(candidate_id=kwargs["candidate_id"], revision=kwargs["revision"])
        return revision, None

    monkeypatch.setattr(
        "collection_infrastructure.postgres.candidate_intake_applier.materialize_candidate",
        _materialize,
    )
    _patch_intake_writes(monkeypatch)

    applier(connection, _source(), _plan(), correlation_id="corr-1")

    assert captured["source_key"] == "openstreetmap"
    assert captured["entity_kind"] == "place"
    assert captured["field_projection"] == {
        "display_name": "names",
        "website": "website_urls",
    }


def test_source_record_intake_rejects_corrupted_provenance() -> None:
    connection = _Recorder()

    tampered = b'{"contract":"tampered"}'
    # The succeeded-output contract rejects bytes that disagree with the artifact
    # digest, so a corrupted batch can never be constructed at all: it fails at the
    # owner boundary before intake observes it.
    with pytest.raises(ValueError, match="artifact digest"):
        _source(batch_content=tampered)
    assert connection.statements == []


def test_source_record_intake_rejects_binding_mismatch() -> None:
    applier = PostgresCandidateIntakeApplier()
    source = _source()
    mismatched = source.__class__(
        **{
            **{
                field: getattr(source, field)
                for field in source.__dataclass_fields__  # type: ignore[attr-defined]
            },
            "source_binding_key": "another_binding",
        }
    )

    with pytest.raises(Exception) as excinfo:
        applier(_Recorder(), mismatched, _plan(), correlation_id="c")
    assert "BINDING" in str(excinfo.value).upper()


def test_unresolved_entity_kind_reaches_review_instead_of_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applier = PostgresCandidateIntakeApplier()
    connection = _Recorder()
    captured: dict[str, Any] = {}

    def _materialize(*_: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _FakeRevision(candidate_id=kwargs["candidate_id"], revision=kwargs["revision"]), None

    monkeypatch.setattr(
        "collection_infrastructure.postgres.candidate_intake_applier.materialize_candidate",
        _materialize,
    )
    _patch_intake_writes(monkeypatch)

    def _review(*_: Any, **kwargs: Any) -> None:
        captured["review_reason_codes"] = kwargs.get("extra_reason_codes")

    monkeypatch.setattr(PostgresCandidateIntakeApplier, "_insert_review_case", _review)
    source = _source()
    profile = dict(_PROFILE, entityKind=None)
    unresolved = source.__class__(
        **{
            **{
                field: getattr(source, field)
                for field in source.__dataclass_fields__  # type: ignore[attr-defined]
            },
            "normalization_profile_content": json.dumps(
                profile, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
        }
    )

    applier(connection, unresolved, _plan(), correlation_id="c")

    assert captured["review_reason_codes"] == ("source_entity_kind_unresolved",)


def test_write_failure_propagates_instead_of_reporting_accepted_intake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing write must escape so the caller's transaction rolls back."""

    applier = PostgresCandidateIntakeApplier(clock=lambda: _OBSERVED_AT)
    connection = _Recorder(fail_on="INSERT INTO candidates.candidates")

    monkeypatch.setattr(
        "collection_infrastructure.postgres.candidate_intake_applier.materialize_candidate",
        lambda *a, **k: (
            _FakeRevision(candidate_id=k["candidate_id"], revision=k["revision"]),
            None,
        ),
    )

    with pytest.raises(RuntimeError, match="injected intake failure"):
        applier(connection, _source(), _plan(), correlation_id="corr-fail")

    # The candidate insert was attempted and failed: no later statement may run, so a
    # partially applied intake cannot be committed by a careless caller.
    candidate_inserts = [
        text for text in connection.statements if "INSERT INTO candidates.candidates" in text
    ]
    assert len(candidate_inserts) == 1
    assert not any(
        "INSERT INTO candidates.candidate_revisions" in text for text in connection.statements
    )


def test_replayed_intake_does_not_duplicate_the_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One logical source entity keeps one candidate row across repeated acquisitions."""

    applier = PostgresCandidateIntakeApplier(clock=lambda: _OBSERVED_AT)
    connection = _Recorder(locked_kind="place")

    monkeypatch.setattr(
        "collection_infrastructure.postgres.candidate_intake_applier.materialize_candidate",
        lambda *a, **k: (
            _FakeRevision(candidate_id=k["candidate_id"], revision=k["revision"]),
            None,
        ),
    )
    for name in ("_insert_revision", "_insert_evidence", "_insert_review_case"):
        monkeypatch.setattr(PostgresCandidateIntakeApplier, name, lambda *a, **k: None)

    applier(connection, _source(), _plan(), correlation_id="corr-1")
    first_pass = list(connection.statements)
    connection.statements.clear()
    applier(connection, _source(), _plan(), correlation_id="corr-2")
    replay = list(connection.statements)

    for pass_statements in (first_pass, replay):
        # The insert is guarded by the deterministic primary key, so a second acquisition of
        # the same logical source entity can never create a second candidate row.
        inserts = [text for text in pass_statements if "INSERT INTO candidates.candidates" in text]
        assert len(inserts) == 1
        assert "ON CONFLICT (candidate_id) DO NOTHING" in inserts[0]
        # The row is locked before anything is derived from it, so concurrent acquisitions of
        # one logical entity cannot allocate the same revision.
        lock_index = next(
            index for index, text in enumerate(pass_statements) if "FOR UPDATE" in text
        )
        revision_index = next(
            index
            for index, text in enumerate(pass_statements)
            if "SELECT revision FROM candidates.source_entity_revisions" in text
        )
        assert lock_index < revision_index


class _FakeRevision:
    """Stands in for a materialized revision; re-sealing happens in the real applier."""

    def __init__(self, *, candidate_id: UUID, revision: int) -> None:
        self.candidate_id = candidate_id
        self.revision = revision
        self.normalized_payload: dict[str, Any] = {"projection": {}}
        self.entity_kind = "place"
        self.cluster_id = UUID("00000000-0000-0000-0000-000000000908")
        self.resolution_state = "review"
        self.snapshot_digest = "sha256:" + "0" * 64
        self.source_lineage_digest = "sha256:" + "1" * 64
        self.evidence: tuple[Any, ...] = ()
        self.recorded_at_utc = _OBSERVED_AT
        self.correlation_id = "corr"


def test_verified_point_against_the_run_boundary_reaches_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The candidate's own point is classified against the run's exact boundary."""

    applier = PostgresCandidateIntakeApplier(clock=lambda: _OBSERVED_AT)
    captured: dict[str, Any] = {}

    def _materialize(*_: Any, **kwargs: Any) -> tuple[Any, Any]:
        captured.update(kwargs)
        return _FakeRevision(candidate_id=kwargs["candidate_id"], revision=kwargs["revision"]), None

    monkeypatch.setattr(
        "collection_infrastructure.postgres.candidate_intake_applier.materialize_candidate",
        _materialize,
    )
    _patch_intake_writes(monkeypatch)

    applier(_Recorder(), _source(), _plan(), correlation_id="corr-1")

    geography = captured["geography"]
    assert geography.coverage == GeographyCoverage.INSIDE
    assert geography.latitude is not None
    assert geography.longitude is not None
    # A classified coverage must name the observation that produced it.
    assert geography.observation_ids


def test_a_point_outside_the_run_boundary_is_classified_outside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applier = PostgresCandidateIntakeApplier(clock=lambda: _OBSERVED_AT)
    captured: dict[str, Any] = {}

    def _materialize(*_: Any, **kwargs: Any) -> tuple[Any, Any]:
        captured.update(kwargs)
        return _FakeRevision(candidate_id=kwargs["candidate_id"], revision=kwargs["revision"]), None

    monkeypatch.setattr(
        "collection_infrastructure.postgres.candidate_intake_applier.materialize_candidate",
        _materialize,
    )
    _patch_intake_writes(monkeypatch)

    outside = _batch_with_point(latitude="48.1372", longitude="11.5756")
    applier(
        _Recorder(),
        _source(batch_content=outside, content_digest=_digest(outside)),
        _plan(),
        correlation_id="corr-1",
    )

    assert captured["geography"].coverage == GeographyCoverage.OUTSIDE


def test_research_only_source_keeps_its_restriction_through_intake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Geography never legitimizes redistribution: the source policy decides that.

    OSM is `research_only`, so intake must record the restriction instead of granting
    redistribution rights. The candidate still enters internal review, and the sealed policy
    it carries is what the export boundary refuses.
    """

    applier = PostgresCandidateIntakeApplier(clock=lambda: _OBSERVED_AT)
    research_only = _campaign_bundle_bytes(legal_status="research_only")
    source = replace(
        _source(),
        campaign_bundle_content=research_only,
        config_bundle_digest=_digest(research_only),
    )

    captured: dict[str, Any] = {}

    def _materialize(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _FakeRevision(candidate_id=kwargs["candidate_id"], revision=kwargs["revision"]), None

    monkeypatch.setattr(
        "collection_infrastructure.postgres.candidate_intake_applier.materialize_candidate",
        _materialize,
    )
    _patch_intake_writes(monkeypatch)

    applier(_Recorder(), source, _plan(), correlation_id="corr-1")

    # Intake keeps the candidate for internal work, without upgrading its usage rights.
    assert captured["geography"].coverage == "inside"
    sealed = applier._export_policy(source)
    # `research_only` is not a redistribution grant of any kind: it is recorded verbatim,
    # with no usage policy, so no reader can mistake it for `link_only`.
    assert sealed.legal_status == "research_only"
    assert sealed.usage_policy is None
    assert sealed.source_key == "openstreetmap"
    assert sealed.attribution == "© OpenStreetMap contributors"
