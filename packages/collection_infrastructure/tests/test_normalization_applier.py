from __future__ import annotations

from unittest.mock import Mock
from uuid import uuid4

import pytest

from collection_application import WorkStage
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
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
