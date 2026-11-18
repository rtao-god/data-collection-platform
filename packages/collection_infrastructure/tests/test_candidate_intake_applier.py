from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    ManualRecordIntakeProvenance,
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    SucceededWorkOutput,
    candidate_intake_result_digest,
)
from collection_contracts import (
    ManualRecordLineage,
    canonical_manual_record_lineage_bytes,
    decode_observation_batch,
)
from collection_domain import WorkStage
from collection_infrastructure.postgres.candidate_intake_applier import (
    PostgresCandidateIntakeApplier,
    _candidate_field_projection,
)

_RECORD_DIGEST = "sha256:" + "9" * 64


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _artifact(artifact_id: UUID, role: str, digest: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_id=artifact_id,
        role=role,
        content_digest=digest,
        size_bytes=64,
        content_type="application/json",
    )


def _valid_batch_bytes(record_digest: str) -> bytes:
    from collection_contracts import (
        EvidenceReference,
        FieldObservationPayload,
        ObservationBatchPayload,
        ObservationState,
        TextObservationValue,
        canonical_observation_batch_json,
        seal_field_observation,
        seal_observation_batch,
    )

    observation = seal_field_observation(
        FieldObservationPayload(
            sourceRecordId=record_digest,
            fieldKey="names",
            state=ObservationState.OBSERVED,
            value=TextObservationValue(original="Studio", normalized="Studio"),
            evidence=EvidenceReference(
                rawArtifactDigest="sha256:" + "1" * 64,
                sourceUrl="https://example.test/studio",
                locatorKind="json_pointer",
                locatorValue="/records/1",
                evidenceDigest="sha256:" + "2" * 64,
                observedAtUtc=datetime(2026, 8, 15, tzinfo=UTC),
                extractorRevision="manual-v1",
            ),
            sourcePolicyDigest="sha256:" + "3" * 64,
            normalizerRevision="manual-v1",
            confidence=1,
        )
    )
    batch = seal_observation_batch(
        ObservationBatchPayload(
            sourceRecordId=record_digest,
            extractedRecordDigest="sha256:" + "4" * 64,
            rawArtifactDigest="sha256:" + "1" * 64,
            sourcePolicyDigest="sha256:" + "3" * 64,
            normalizerRevision="manual-v1",
            observations=(observation,),
            issues=(),
        )
    )
    return canonical_observation_batch_json(batch).encode("utf-8")


def _replace_output(
    source: SucceededWorkOutput,
    *,
    digest: str | None = None,
    artifact: ArtifactIdentity | None = None,
    content: bytes | None = None,
    lineage: bytes | object | None = ...,
) -> SucceededWorkOutput:
    return SucceededWorkOutput(
        source_work_unit_id=source.source_work_unit_id,
        run_id=source.run_id,
        stage_run_id=source.stage_run_id,
        stage=source.stage,
        capability=source.capability,
        output_contract=source.output_contract,
        output_digest=digest or source.output_digest,
        output_artifact=artifact or source.output_artifact,
        input_artifacts=source.input_artifacts,
        source_binding_key=source.source_binding_key,
        output_content=content,
        manual_record_lineage_content=(
            source.manual_record_lineage_content if lineage is ... else lineage
        ),
    )


def _source(*, with_batch: bool = True) -> tuple[SucceededWorkOutput, bytes]:
    batch_bytes = _valid_batch_bytes(_RECORD_DIGEST)
    batch_digest = decode_observation_batch(batch_bytes).content_digest
    bytes_digest = _digest(batch_bytes)
    lineage = ManualRecordLineage(
        runId=UUID("00000000-0000-0000-0000-000000000501"),
        manualRecordWorkId=uuid4(),
        manualRecordArtifactId=uuid4(),
        manualRecordArtifactDigest="sha256:" + "1" * 64,
        recordDigest=_RECORD_DIGEST,
        expectedEntityKind="organization",
        manualRecordContentDigest=_RECORD_DIGEST,
        sourceArtifactId=uuid4(),
        sourceArtifactDigest="sha256:" + "3" * 64,
    )
    lineage_bytes = canonical_manual_record_lineage_bytes(lineage)
    source = SucceededWorkOutput(
        source_work_unit_id=uuid4(),
        run_id=lineage.run_id,
        stage_run_id=uuid4(),
        stage=WorkStage.NORMALIZATION,
        capability="normalization",
        output_contract="field-observation-batch@1",
        output_digest=batch_digest,
        output_artifact=_artifact(uuid4(), "field_observation_batch", bytes_digest),
        input_artifacts=(
            _artifact(uuid4(), "extracted_record", "sha256:" + "4" * 64),
            _artifact(uuid4(), "normalization_profile", "sha256:" + "5" * 64),
            _artifact(uuid4(), "manual_record_lineage", _digest(lineage_bytes)),
        ),
        source_binding_key="google_places",
        output_content=batch_bytes,
        manual_record_lineage_content=lineage_bytes,
    )
    if not with_batch:
        source = _replace_output(source, content=None)
    return source, batch_bytes


def _plan(source: SucceededWorkOutput) -> PipelineTransitionPlan:
    from collection_application.pipeline_advancement import (
        CandidateIntakeAction,
        PipelineTransitionRegistry,
    )

    action = CandidateIntakeAction(
        candidate_id=uuid4(),
        run_id=source.run_id,
        source_work_unit_id=source.source_work_unit_id,
        observation_batch_digest=source.output_digest,
        provenance=ManualRecordIntakeProvenance(
            manual_record_artifact_id=uuid4(),
            manual_record_lineage_artifact=source.input_artifacts[2],
        ),
        source_artifact_ids=tuple(item.artifact_id for item in source.input_artifacts),
    )
    return PipelineTransitionPlan(
        source_work_unit_id=source.source_work_unit_id,
        transition_key="field-observation-batch-candidate-intake-v1",
        disposition=PipelineTransitionDisposition.APPLY,
        plan_digest=PipelineTransitionRegistry().plan(source).plan_digest,
        blocker=None,
        candidate_intake_actions=(action,),
    )


def test_empty_plan_fails_before_side_effects() -> None:
    source, _ = _source()
    plan = PipelineTransitionPlan(
        source_work_unit_id=source.source_work_unit_id,
        transition_key="field-observation-batch-candidate-intake-v1",
        disposition=PipelineTransitionDisposition.APPLY,
        plan_digest="sha256:" + "d" * 64,
        blocker=None,
    )
    connection = Mock()
    applier = PostgresCandidateIntakeApplier(clock=lambda: datetime(2026, 8, 15, tzinfo=UTC))

    with pytest.raises(PipelineAdvancementConflict) as caught:
        applier(connection, source, plan, correlation_id="test-correlation")

    assert caught.value.code == "PIPELINE_CANDIDATE_INTAKE_PLAN_INVALID"
    assert connection.mock_calls == []


def test_missing_batch_bytes_fails_closed_without_writes() -> None:
    source, _ = _source(with_batch=False)
    plan = _plan(source)
    connection = Mock()
    applier = PostgresCandidateIntakeApplier(clock=lambda: datetime(2026, 8, 15, tzinfo=UTC))

    with pytest.raises(PipelineAdvancementConflict) as caught:
        applier(connection, source, plan, correlation_id="test-correlation")

    assert caught.value.code == "PIPELINE_CANDIDATE_BATCH_MISSING"
    connection.execute.assert_not_called()


def test_invalid_batch_contract_fails_closed_without_writes() -> None:
    source, _ = _source()
    tampered = b'{"contract":"collector-field-observation-batch"}'
    tampered_source = _replace_output(
        source,
        digest=_digest(tampered),
        artifact=_artifact(
            source.output_artifact.artifact_id, "field_observation_batch", _digest(tampered)
        ),
        content=tampered,
    )
    plan = _plan(tampered_source)
    connection = Mock()
    applier = PostgresCandidateIntakeApplier(clock=lambda: datetime(2026, 8, 15, tzinfo=UTC))

    with pytest.raises(PipelineAdvancementConflict) as caught:
        applier(connection, tampered_source, plan, correlation_id="test-correlation")

    assert caught.value.code == "PIPELINE_CANDIDATE_BATCH_INVALID"
    connection.execute.assert_not_called()


def test_missing_lineage_fails_closed_without_writes() -> None:
    source, _ = _source()
    lineage_free = _replace_output(source, lineage=None, content=source.output_content)
    plan = _plan(lineage_free)
    connection = Mock()
    applier = PostgresCandidateIntakeApplier(clock=lambda: datetime(2026, 8, 15, tzinfo=UTC))

    with pytest.raises(PipelineAdvancementConflict) as caught:
        applier(connection, lineage_free, plan, correlation_id="test-correlation")

    assert caught.value.code == "PIPELINE_CANDIDATE_LINEAGE_MISSING"
    connection.execute.assert_not_called()


def test_projection_requires_explicit_profile_targets() -> None:
    from collection_contracts import (
        BooleanPatternRule,
        NormalizationFieldRule,
        NormalizationProfile,
        ObservationBatch,
    )

    profile = NormalizationProfile(
        normalizerRevision="normalizer-v1",
        fieldRules=(
            NormalizationFieldRule(sourceField="name", targetField="names", valueKind="text"),
            NormalizationFieldRule(sourceField="phone", targetField="phones", valueKind="phone"),
        ),
        booleanPatternRules=(
            BooleanPatternRule(targetField="addresses", positivePatterns=("open",)),
        ),
    )
    batch = Mock(spec=ObservationBatch)
    batch.observations = (
        Mock(field_key="names"),
        Mock(field_key="phones"),
        Mock(field_key="addresses"),
    )

    projection = _candidate_field_projection(batch, profile)

    assert projection["names"].value == "names"
    assert projection["phones"].value == "phones"
    assert projection["addresses"].value == "addresses"


def test_projection_rejects_fields_absent_from_profile() -> None:
    from collection_contracts import NormalizationFieldRule, NormalizationProfile, ObservationBatch

    profile = NormalizationProfile(
        normalizerRevision="normalizer-v1",
        fieldRules=(
            NormalizationFieldRule(sourceField="name", targetField="names", valueKind="text"),
        ),
        booleanPatternRules=(),
    )
    batch = Mock(spec=ObservationBatch)
    batch.observations = (Mock(field_key="unexpected_field"),)

    with pytest.raises(ValueError, match="absent from the exact normalization profile"):
        _candidate_field_projection(batch, profile)


def test_projection_rejects_unknown_candidate_target() -> None:
    from collection_contracts import NormalizationFieldRule, NormalizationProfile, ObservationBatch

    profile = NormalizationProfile(
        normalizerRevision="normalizer-v1",
        fieldRules=(
            NormalizationFieldRule(sourceField="name", targetField="other_field", valueKind="text"),
        ),
        booleanPatternRules=(),
    )
    batch = Mock(spec=ObservationBatch)
    batch.observations = (Mock(field_key="other_field"),)

    with pytest.raises(ValueError, match="has no candidate field"):
        _candidate_field_projection(batch, profile)


def test_result_digest_is_deterministic_for_exact_plan() -> None:
    source, _ = _source()
    plan = _plan(source)

    assert candidate_intake_result_digest(plan) == candidate_intake_result_digest(plan)
