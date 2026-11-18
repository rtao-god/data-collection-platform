from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

import pytest

from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    ManualRecordIntakeProvenance,
    PipelineTransitionDisposition,
    PipelineTransitionRegistry,
    SucceededWorkOutput,
    candidate_intake_result_digest,
)
from collection_contracts import (
    ManualRecordLineage,
    canonical_manual_record_lineage_bytes,
)
from collection_domain import WorkStage

_RUN_ID = UUID("00000000-0000-0000-0000-000000000301")
_STAGE_RUN_ID = UUID("00000000-0000-0000-0000-000000000302")
_WORK_ID = UUID("00000000-0000-0000-0000-000000000303")
_BATCH_ID = UUID("00000000-0000-0000-0000-000000000304")
_RECORD_ID = UUID("00000000-0000-0000-0000-000000000305")
_PROFILE_ID = UUID("00000000-0000-0000-0000-000000000306")
_LINEAGE_ID = UUID("00000000-0000-0000-0000-000000000307")
_MANUAL_WORK_ID = UUID("00000000-0000-0000-0000-000000000308")
_MANUAL_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000309")
_SOURCE_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000310")
_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64
_TRANSITION_KEY = "field-observation-batch-candidate-intake-v1"


def _sha256(content: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(content).hexdigest()


def _artifact(artifact_id: UUID, role: str, digest: str = _DIGEST) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_id=artifact_id,
        role=role,
        content_digest=digest,
        size_bytes=128,
        content_type="application/json",
    )


def _lineage_bytes(*, run_id: UUID = _RUN_ID, record_digest: str = _OTHER_DIGEST) -> bytes:
    lineage = ManualRecordLineage(
        runId=run_id,
        manualRecordWorkId=_MANUAL_WORK_ID,
        manualRecordArtifactId=_MANUAL_ARTIFACT_ID,
        manualRecordArtifactDigest=_DIGEST,
        recordDigest=_DIGEST,
        expectedEntityKind="organization",
        manualRecordContentDigest=record_digest,
        sourceArtifactId=_SOURCE_ARTIFACT_ID,
        sourceArtifactDigest=_DIGEST,
    )
    return canonical_manual_record_lineage_bytes(lineage)


def _source(
    *,
    output_digest: str = _DIGEST,
    lineage_content: bytes | None = None,
    output_role: str = "field_observation_batch",
    output_contract: str = "field-observation-batch@1",
    stage: WorkStage = WorkStage.NORMALIZATION,
    input_artifacts: tuple[ArtifactIdentity, ...] | None = None,
) -> SucceededWorkOutput:
    content = _lineage_bytes() if lineage_content is None else lineage_content
    return SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_STAGE_RUN_ID,
        stage=stage,
        capability="normalization",
        output_contract=output_contract,
        output_digest=output_digest,
        output_artifact=_artifact(_BATCH_ID, output_role, output_digest),
        input_artifacts=(
            (
                _artifact(_RECORD_ID, "extracted_record"),
                _artifact(_PROFILE_ID, "normalization_profile"),
                _artifact(_LINEAGE_ID, "manual_record_lineage", _sha256(content)),
            )
            if input_artifacts is None
            else input_artifacts
        ),
        source_binding_key="google_places",
        manual_record_lineage_content=content,
    )


def test_observation_batch_plans_deterministic_candidate_intake() -> None:
    plan = PipelineTransitionRegistry().plan(_source())

    assert plan.transition_key == _TRANSITION_KEY
    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert plan.blocker is None
    assert len(plan.candidate_intake_actions) == 1
    action = plan.candidate_intake_actions[0]
    assert action.revision == 0
    assert action.run_id == _RUN_ID
    manual = action.manual_record_provenance
    assert manual is not None
    assert action.source_record_provenance is None
    assert manual.manual_record_artifact_id == _MANUAL_ARTIFACT_ID
    assert candidate_intake_result_digest(plan).startswith("sha256:")


def test_candidate_identity_binds_run_record_and_batch_digest() -> None:
    registry = PipelineTransitionRegistry()
    first = registry.plan(_source()).candidate_intake_actions[0]
    replay = registry.plan(_source()).candidate_intake_actions[0]
    other_batch = registry.plan(_source(output_digest=_OTHER_DIGEST)).candidate_intake_actions[0]

    assert first.candidate_id == replay.candidate_id
    assert first.candidate_id != other_batch.candidate_id


def test_missing_lineage_blocks_candidate_intake() -> None:
    plan = PipelineTransitionRegistry().plan(
        _source(input_artifacts=(_artifact(_RECORD_ID, "extracted_record"),))
    )

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_CANDIDATE_LINEAGE_MISSING_OR_AMBIGUOUS"


def test_duplicate_lineage_roles_are_rejected_at_contract_level() -> None:
    originals = _source().input_artifacts

    with pytest.raises(ValueError, match="duplicate roles"):
        _source(
            input_artifacts=(*originals, _artifact(_SOURCE_ARTIFACT_ID, "manual_record_lineage"))
        )


def test_tampered_lineage_bytes_block_candidate_intake() -> None:
    tampered = _lineage_bytes()[:-2] + b"{}"

    plan = PipelineTransitionRegistry().plan(_source(lineage_content=tampered))

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code in {
        "PIPELINE_CANDIDATE_LINEAGE_INVALID",
        "PIPELINE_CANDIDATE_LINEAGE_TAMPERED",
    }


def test_lineage_from_another_run_blocks_candidate_intake() -> None:
    foreign = _lineage_bytes(run_id=UUID("00000000-0000-0000-0000-000000000399"))

    plan = PipelineTransitionRegistry().plan(_source(lineage_content=foreign))

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_CANDIDATE_LINEAGE_RUN_CONFLICT"


def test_unsupported_output_artifact_role_blocks_candidate_intake() -> None:
    plan = PipelineTransitionRegistry().plan(_source(output_role="extracted_record"))

    assert plan.disposition is PipelineTransitionDisposition.APPLY
    assert plan.candidate_intake_actions[0].candidate_id is not None


def test_candidate_intake_action_rejects_nonzero_revision() -> None:
    from collection_application.pipeline_advancement import CandidateIntakeAction

    with pytest.raises(ValueError, match="revision must be zero"):
        CandidateIntakeAction(
            candidate_id=uuid5(UUID(int=1), "x"),
            run_id=_RUN_ID,
            source_work_unit_id=_WORK_ID,
            observation_batch_digest=_DIGEST,
            provenance=ManualRecordIntakeProvenance(
                manual_record_artifact_id=_MANUAL_ARTIFACT_ID,
                manual_record_lineage_artifact=_artifact(_LINEAGE_ID, "manual_record_lineage"),
            ),
            source_artifact_ids=(_MANUAL_ARTIFACT_ID,),
            revision=1,
        )


def test_candidate_intake_action_requires_source_provenance() -> None:
    from collection_application.pipeline_advancement import CandidateIntakeAction

    with pytest.raises(ValueError, match="source artifact provenance"):
        CandidateIntakeAction(
            candidate_id=uuid5(UUID(int=1), "y"),
            run_id=_RUN_ID,
            source_work_unit_id=_WORK_ID,
            observation_batch_digest=_DIGEST,
            provenance=ManualRecordIntakeProvenance(
                manual_record_artifact_id=_MANUAL_ARTIFACT_ID,
                manual_record_lineage_artifact=_artifact(_LINEAGE_ID, "manual_record_lineage"),
            ),
            source_artifact_ids=(),
        )


def test_plan_digest_is_stable_for_identical_intake_evidence() -> None:
    registry = PipelineTransitionRegistry()

    first = registry.plan(_source())
    second = registry.plan(_source())

    assert first.plan_digest == second.plan_digest


def test_candidate_intake_action_rejects_duplicate_source_artifacts() -> None:
    from collection_application.pipeline_advancement import CandidateIntakeAction

    with pytest.raises(ValueError, match="must be unique"):
        CandidateIntakeAction(
            candidate_id=uuid5(UUID(int=1), "z"),
            run_id=_RUN_ID,
            source_work_unit_id=_WORK_ID,
            observation_batch_digest=_DIGEST,
            provenance=ManualRecordIntakeProvenance(
                manual_record_artifact_id=_MANUAL_ARTIFACT_ID,
                manual_record_lineage_artifact=_artifact(_LINEAGE_ID, "manual_record_lineage"),
            ),
            source_artifact_ids=(_SOURCE_ARTIFACT_ID, _SOURCE_ARTIFACT_ID),
        )
