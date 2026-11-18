from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from unittest.mock import Mock
from uuid import uuid4

import pytest

from collection_application import WorkStage
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    NormalizationWorkAction,
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    SucceededWorkOutput,
)
from collection_infrastructure.postgres.normalization_applier import PostgresNormalizationApplier


def test_empty_normalization_plan_fails_before_stage_lookup_or_side_effects() -> None:
    source = SucceededWorkOutput(
        source_work_unit_id=uuid4(),
        run_id=uuid4(),
        stage_run_id=uuid4(),
        stage=WorkStage.EXTRACTION,
        capability="extraction",
        output_contract="extracted-record@1",
        output_digest="sha256:" + "a" * 64,
        output_artifact=ArtifactIdentity(
            artifact_id=uuid4(),
            role="extracted_record",
            content_digest="sha256:" + "b" * 64,
            size_bytes=1,
            content_type="application/vnd.collection.extracted-record+json",
        ),
        input_artifacts=(),
        source_binding_key="binding",
        config_bundle_digest="sha256:" + "c" * 64,
    )
    plan = PipelineTransitionPlan(
        source_work_unit_id=source.source_work_unit_id,
        transition_key="extracted-record-normalization-v1",
        disposition=PipelineTransitionDisposition.APPLY,
        plan_digest="sha256:" + "d" * 64,
        blocker=None,
    )
    work_engine = Mock()
    publisher = Mock()
    applier = PostgresNormalizationApplier(work_engine, publisher)

    with pytest.raises(PipelineAdvancementConflict) as caught:
        applier(Mock(), source, plan, correlation_id="test-correlation")

    assert caught.value.code == "PIPELINE_NORMALIZATION_PLAN_INVALID"
    assert work_engine.mock_calls == []
    assert publisher.mock_calls == []


def _normalization_plan_source() -> tuple[SucceededWorkOutput, PipelineTransitionPlan]:
    work_id = uuid4()
    source_artifacts = (
        ArtifactIdentity(
            artifact_id=uuid4(),
            role="raw_source_document",
            content_digest="sha256:" + "1" * 64,
            size_bytes=1,
            content_type="text/html",
        ),
        ArtifactIdentity(
            artifact_id=uuid4(),
            role="extraction_request",
            content_digest="sha256:" + "2" * 64,
            size_bytes=1,
            content_type="application/vnd.collection.extraction-request+json",
        ),
        ArtifactIdentity(
            artifact_id=uuid4(),
            role="manual_record_lineage",
            content_digest="sha256:" + "3" * 64,
            size_bytes=1,
            content_type="application/vnd.collection.manual-record-lineage+json",
        ),
    )
    source = SucceededWorkOutput(
        source_work_unit_id=work_id,
        run_id=uuid4(),
        stage_run_id=uuid4(),
        stage=WorkStage.EXTRACTION,
        capability="extraction",
        output_contract="extracted-record@1",
        output_digest="sha256:" + "a" * 64,
        output_artifact=ArtifactIdentity(
            artifact_id=uuid4(),
            role="extracted_record",
            content_digest="sha256:" + "b" * 64,
            size_bytes=1,
            content_type="application/vnd.collection.extracted-record+json",
        ),
        input_artifacts=source_artifacts,
        source_binding_key="binding",
        config_bundle_digest="sha256:" + "c" * 64,
    )
    profile_payload = b"{}"
    action = NormalizationWorkAction(
        source_binding_key="binding",
        source_policy_digest="sha256:" + "d" * 64,
        work_id=uuid4(),
        semantic_key="sha256:" + "e" * 64,
        input_digest="sha256:" + "f" * 64,
        extracted_record_artifact_id=source.output_artifact.artifact_id,
        profile_payload=profile_payload,
        profile_digest="sha256:" + sha256(profile_payload).hexdigest(),
        manual_record_lineage_artifact=source_artifacts[2],
    )
    plan = PipelineTransitionPlan(
        source_work_unit_id=work_id,
        transition_key="extracted-record-normalization-v1",
        disposition=PipelineTransitionDisposition.APPLY,
        plan_digest="sha256:" + "9" * 64,
        blocker=None,
        normalization_actions=(action,),
    )
    return source, plan


def test_valid_normalization_plan_accepts_exact_extraction_lineage_inputs() -> None:
    source, plan = _normalization_plan_source()

    assert PostgresNormalizationApplier._valid_plan(source, plan)


@pytest.mark.parametrize("lineage_binding", ["missing", "substituted"])
def test_valid_normalization_plan_rejects_missing_or_substituted_lineage(
    lineage_binding: str,
) -> None:
    source, plan = _normalization_plan_source()
    action = plan.normalization_actions[0]
    if lineage_binding == "missing":
        source = replace(source, input_artifacts=source.input_artifacts[:-1])
    else:
        action = replace(
            action,
            manual_record_lineage_artifact=ArtifactIdentity(
                artifact_id=uuid4(),
                role="manual_record_lineage",
                content_digest="sha256:" + "8" * 64,
                size_bytes=1,
                content_type="application/vnd.collection.manual-record-lineage+json",
            ),
        )
        plan = PipelineTransitionPlan(
            source_work_unit_id=plan.source_work_unit_id,
            transition_key=plan.transition_key,
            disposition=plan.disposition,
            plan_digest=plan.plan_digest,
            blocker=None,
            normalization_actions=(action,),
        )

    assert not PostgresNormalizationApplier._valid_plan(source, plan)


def test_legacy_two_input_artifact_plan_is_rejected_before_lineage_lookup() -> None:
    """Regression: normalization intake requires exactly three binding roles.

    Before this contract the applier accepted two inputs, which let an extraction
    success advance without its verified manual-record lineage. The plan must be
    rejected deterministically and without touching the work engine or publisher.
    """

    source, plan = _normalization_plan_source()
    source = replace(source, input_artifacts=source.input_artifacts[:2])
    work_engine = Mock()
    publisher = Mock()
    applier = PostgresNormalizationApplier(work_engine, publisher)

    assert not PostgresNormalizationApplier._valid_plan(source, plan)
    with pytest.raises(PipelineAdvancementConflict) as caught:
        applier(Mock(), source, plan, correlation_id="test-correlation")

    assert caught.value.code == "PIPELINE_NORMALIZATION_PLAN_INVALID"
    assert caught.value.required_action
    assert work_engine.mock_calls == []
    assert publisher.mock_calls == []


def test_normalization_plan_rejects_extra_fourth_input_role() -> None:
    source, plan = _normalization_plan_source()
    source = replace(
        source,
        input_artifacts=(
            *source.input_artifacts,
            ArtifactIdentity(
                artifact_id=uuid4(),
                role="field_observation_batch",
                content_digest="sha256:" + "7" * 64,
                size_bytes=1,
                content_type="application/vnd.collection.field-observation-batch+json",
            ),
        ),
    )

    assert not PostgresNormalizationApplier._valid_plan(source, plan)


def _extraction_input_roles(source: SucceededWorkOutput) -> set[str]:
    return {item.role for item in source.input_artifacts}


def test_extraction_normalization_input_roles_are_the_exact_three_role_set() -> None:
    """Normalization intake: extraction -> normalization roles are an exact named set.

    `raw_source_document`, `extraction_request`, `manual_record_lineage` must all be
    present, and nothing else. Source-native mapping is a different capability and
    must not be able to borrow this shape.
    """

    source, _plan = _normalization_plan_source()
    assert _extraction_input_roles(source) == {
        "raw_source_document",
        "extraction_request",
        "manual_record_lineage",
    }

    for dropped in sorted(_extraction_input_roles(source)):
        reduced = replace(
            source,
            input_artifacts=tuple(item for item in source.input_artifacts if item.role != dropped),
        )
        assert _extraction_input_roles(reduced) != _extraction_input_roles(source)


def test_source_record_mapping_output_is_not_a_normalization_input_role() -> None:
    """A source-native batch must never be accepted as extraction lineage evidence."""

    source, _plan = _normalization_plan_source()
    assert "source_record_observation_batch" not in _extraction_input_roles(source)
