from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from unittest.mock import Mock
from uuid import uuid4

import pytest

from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    SourceRecordMappingAction,
    SucceededWorkOutput,
)
from collection_domain import WorkCapability, WorkStage
from collection_infrastructure.postgres.source_record_mapping_applier import (
    PostgresSourceRecordMappingApplier,
)

_PROFILE_PAYLOAD = b'{"mappingRevision":"osm-overpass-source-record-mapping-v1"}'
_RECORD_PAYLOAD = b'{"record":"osm:node/101"}'


def _source_and_plan() -> tuple[SucceededWorkOutput, PipelineTransitionPlan]:
    work_id = uuid4()
    record_artifact = ArtifactIdentity(
        artifact_id=uuid4(),
        role="source_record",
        content_digest="sha256:" + sha256(_RECORD_PAYLOAD).hexdigest(),
        size_bytes=len(_RECORD_PAYLOAD),
        content_type="application/vnd.collection.osm-observations+json",
    )
    source = SucceededWorkOutput(
        source_work_unit_id=work_id,
        run_id=uuid4(),
        stage_run_id=uuid4(),
        stage=WorkStage.DISCOVERY,
        capability="osm_query",
        output_contract="osm-overpass-result@1",
        output_digest="sha256:" + "2" * 64,
        output_artifact=record_artifact,
        input_artifacts=(),
        source_binding_key="osm_overpass_berlin",
        config_bundle_digest="sha256:" + "3" * 64,
    )
    action = SourceRecordMappingAction(
        work_id=uuid4(),
        source_key="openstreetmap",
        source_binding_key="osm_overpass_berlin",
        source_policy_digest="sha256:" + "4" * 64,
        source_record_id="osm:node/101",
        dataset_release="osm-overpass-live-v1",
        source_record_artifact=record_artifact,
        source_record_payload=_RECORD_PAYLOAD,
        raw_response_artifact=ArtifactIdentity(
            artifact_id=uuid4(),
            role="osm_observations",
            content_digest="sha256:" + "6" * 64,
            size_bytes=10,
            content_type="application/vnd.collection.osm-observations+json",
        ),
        mapping_profile_payload=_PROFILE_PAYLOAD,
        mapping_profile_digest="sha256:" + sha256(_PROFILE_PAYLOAD).hexdigest(),
        semantic_key="sha256:" + "5" * 64,
        input_digest="sha256:" + "5" * 64,
    )
    plan = PipelineTransitionPlan(
        source_work_unit_id=work_id,
        transition_key="osm-overpass-result-source-record-mapping-v1",
        disposition=PipelineTransitionDisposition.APPLY,
        plan_digest="sha256:" + "6" * 64,
        blocker=None,
        source_record_mapping_actions=(action,),
    )
    return source, plan


def test_source_record_mapping_action_carries_no_manual_lineage() -> None:
    _source, plan = _source_and_plan()
    action = plan.source_record_mapping_actions[0]

    assert not hasattr(action, "manual_record_lineage_artifact")
    assert action.source_record_artifact.role == "source_record"
    assert action.source_policy_digest
    assert action.dataset_release


def test_mapping_action_rejects_non_source_record_role() -> None:
    _source, plan = _source_and_plan()
    action = plan.source_record_mapping_actions[0]

    with pytest.raises(ValueError, match="source_record artifact role"):
        replace(
            action,
            source_record_artifact=replace(action.source_record_artifact, role="extracted_record"),
        )


def test_mapping_action_rejects_profile_payload_digest_mismatch() -> None:
    _source, plan = _source_and_plan()
    action = plan.source_record_mapping_actions[0]

    with pytest.raises(ValueError, match="mapping profile payload"):
        replace(action, mapping_profile_payload=b'{"other":true}')


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s, p: (s, p),
        lambda s, p: (replace(s, capability="http_fetch"), p),
        lambda s, p: (replace(s, output_contract="extracted-record@1"), p),
        lambda s, p: (replace(s, stage=WorkStage.NORMALIZATION), p),
        lambda s, p: (s, replace(p, transition_key="extracted-record-normalization-v1")),
        lambda s, p: (s, replace(p, source_record_mapping_actions=())),
        lambda s, p: (
            s,
            replace(
                p,
                normalization_actions=(
                    object(),  # type: ignore[arg-type]
                ),
            ),
        ),
    ],
)
def test_same_owner_plan_validity_is_an_exact_match(mutate: object) -> None:
    source, plan = _source_and_plan()
    mutated_source, mutated_plan = mutate(source, plan)  # type: ignore[operator]

    expected = mutated_source is source and mutated_plan is plan
    assert PostgresSourceRecordMappingApplier._valid_plan(mutated_source, mutated_plan) is expected


def test_invalid_plan_fails_before_any_side_effect() -> None:
    source, plan = _source_and_plan()
    work_engine = Mock()
    publisher = Mock()
    applier = PostgresSourceRecordMappingApplier(work_engine, publisher)

    with pytest.raises(PipelineAdvancementConflict) as caught:
        applier(
            Mock(),
            replace(source, capability="http_fetch"),
            plan,
            correlation_id="test-correlation",
        )

    assert caught.value.code == "PIPELINE_SOURCE_RECORD_MAPPING_PLAN_INVALID"
    assert work_engine.mock_calls == []
    assert publisher.mock_calls == []


def test_capability_is_process_only_and_never_requires_source_permit() -> None:
    from collection_domain import capability_requires_source_permit

    assert not capability_requires_source_permit(WorkCapability.SOURCE_RECORD_MAPPING)
