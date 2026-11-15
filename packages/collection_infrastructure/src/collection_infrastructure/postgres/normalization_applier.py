from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from collection_application import (
    ArtifactKind,
    OwnedArtifactPublishConflict,
    PublishOwnedArtifact,
    RetryPolicy,
    WorkCapability,
    WorkInputArtifact,
    WorkStage,
    WorkUnitSpec,
)
from collection_application.pipeline_advancement import (
    NormalizationWorkAction,
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    SucceededWorkOutput,
    normalization_work_result_digest,
)
from collection_application.work_engine import WorkEngineConflict
from collection_infrastructure.postgres.owned_artifact_publisher import (
    PostgresOwnedArtifactPublisher,
)
from collection_infrastructure.postgres.work_engine import PostgresWorkEngine
from collection_infrastructure.postgres.work_metadata import stage_runs

_NORMALIZATION_TRANSITION_KEY = "extracted-record-normalization-v1"
_ARTIFACT_NAMESPACE = UUID("337f2651-89d3-56b6-a4ef-371263ee4620")
_PRODUCER_IDENTITY = "pipeline-extracted-record-normalization"
_PROFILE_ROLE = "normalization_profile"
_PROFILE_CONTENT_TYPE = "application/vnd.collection.normalization-profile+json"
_RECORD_ROLE = "extracted_record"
_OUTPUT_CONTRACT = "field-observation-batch@1"


class PostgresNormalizationApplier:
    """Atomically records deterministic normalization work and its immutable profile input."""

    def __init__(
        self,
        work_engine: PostgresWorkEngine,
        artifact_publisher: PostgresOwnedArtifactPublisher,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._work_engine = work_engine
        self._artifact_publisher = artifact_publisher
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(
        self,
        connection: Connection,
        source: SucceededWorkOutput,
        plan: PipelineTransitionPlan,
        *,
        correlation_id: str,
    ) -> str:
        if not self._valid_plan(source, plan):
            raise PipelineAdvancementConflict(
                code="PIPELINE_NORMALIZATION_PLAN_INVALID",
                message="The normalization applier received unrelated or incomplete evidence.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "transitionKey": plan.transition_key,
                    "actionCount": len(plan.normalization_actions),
                },
                required_action=(
                    "Recompute normalization from the exact extracted record and run snapshot."
                ),
            )
        action = plan.normalization_actions[0]
        if action.source_binding_key != source.source_binding_key:
            raise PipelineAdvancementConflict(
                code="PIPELINE_NORMALIZATION_BINDING_CONFLICT",
                message="The normalization action differs from extraction binding provenance.",
                context={"workId": str(action.work_id)},
                required_action="Recompute normalization from the exact run snapshot and binding.",
            )

        stage_run_id = self._normalization_stage_run_id(connection, source.run_id)
        now_utc = self._now_utc()
        profile_artifact_id = self._publish_profile(
            connection,
            action=action,
            correlation_id=correlation_id,
        )
        try:
            spec = WorkUnitSpec(
                work_id=action.work_id,
                run_id=source.run_id,
                stage_run_id=stage_run_id,
                stage=WorkStage.NORMALIZATION,
                capability=WorkCapability.NORMALIZATION,
                source_key=None,
                source_binding_key=action.source_binding_key,
                semantic_key=action.semantic_key,
                input_digest=action.input_digest,
                expected_output_contract=_OUTPUT_CONTRACT,
                priority=0,
                retry_policy=RetryPolicy(
                    max_attempts=3,
                    initial_delay_seconds=30,
                    multiplier=2,
                    max_delay_seconds=900,
                ),
                available_at_utc=now_utc,
                correlation_id=correlation_id,
                input_artifacts=(
                    WorkInputArtifact(
                        artifact_id=action.extracted_record_artifact_id,
                        role=_RECORD_ROLE,
                    ),
                    WorkInputArtifact(
                        artifact_id=profile_artifact_id,
                        role=_PROFILE_ROLE,
                    ),
                    WorkInputArtifact(
                        artifact_id=action.manual_record_lineage_artifact.artifact_id,
                        role="manual_record_lineage",
                    ),
                ),
            )
        except ValueError as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_NORMALIZATION_WORK_INVALID",
                message="The normalization plan cannot form a valid normalization work unit.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "workId": str(action.work_id),
                    "causeType": type(exc).__name__,
                },
                required_action=(
                    "Correct the exact extracted record or normalization profile contract."
                ),
            ) from exc
        try:
            self._work_engine.enqueue_work_in_transaction(connection, spec)
        except WorkEngineConflict as exc:
            raise PipelineAdvancementConflict(
                code=exc.code,
                message=exc.message,
                context={"workId": str(action.work_id), **exc.context},
                required_action=exc.required_action,
            ) from exc
        return normalization_work_result_digest(plan)

    @staticmethod
    def _valid_plan(source: SucceededWorkOutput, plan: PipelineTransitionPlan) -> bool:
        if len(plan.normalization_actions) != 1:
            return False
        action = plan.normalization_actions[0]
        return all(
            (
                plan.source_work_unit_id == source.source_work_unit_id,
                plan.transition_key == _NORMALIZATION_TRANSITION_KEY,
                source.stage is WorkStage.EXTRACTION,
                source.capability == WorkCapability.EXTRACTION.value,
                source.output_contract == "extracted-record@1",
                source.output_artifact.role == _RECORD_ROLE,
                source.source_binding_key is not None,
                source.config_bundle_digest is not None,
                plan.disposition is PipelineTransitionDisposition.APPLY,
                plan.blocker is None,
                not plan.website_actions,
                not plan.extraction_actions,
                len(source.input_artifacts) == 2,
                source.output_artifact.artifact_id == action.extracted_record_artifact_id,
            )
        )

    def _publish_profile(
        self,
        connection: Connection,
        *,
        action: NormalizationWorkAction,
        correlation_id: str,
    ) -> UUID:
        operation_id = uuid5(_ARTIFACT_NAMESPACE, f"{action.semantic_key}:{_PROFILE_ROLE}")
        artifact_id = uuid5(_ARTIFACT_NAMESPACE, f"artifact:{operation_id}")
        try:
            published = self._artifact_publisher.publish_in_transaction(
                connection,
                PublishOwnedArtifact(
                    artifact_id=artifact_id,
                    operation_id=operation_id,
                    producer_identity=_PRODUCER_IDENTITY,
                    artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                    content=action.profile_payload,
                    content_type=_PROFILE_CONTENT_TYPE,
                    source_policy_digest=action.source_policy_digest,
                    correlation_id=correlation_id,
                ),
            )
        except OwnedArtifactPublishConflict as exc:
            raise PipelineAdvancementConflict(
                code=exc.code,
                message=exc.message,
                context={
                    "workId": str(action.work_id),
                    "artifactRole": _PROFILE_ROLE,
                    **exc.context,
                },
                required_action=exc.required_action,
            ) from exc
        return published.artifact_id

    @staticmethod
    def _normalization_stage_run_id(connection: Connection, run_id: UUID) -> UUID:
        values = tuple(
            connection.execute(
                sa.select(stage_runs.c.stage_run_id).where(
                    stage_runs.c.run_id == run_id,
                    stage_runs.c.stage == WorkStage.NORMALIZATION.value,
                )
            )
            .scalars()
            .all()
        )
        if len(values) != 1 or not isinstance(values[0], UUID):
            raise PipelineAdvancementConflict(
                code="PIPELINE_NORMALIZATION_STAGE_OWNER_INVALID",
                message="The run must have exactly one canonical normalization stage owner.",
                context={"runId": str(run_id), "stageRunCount": len(values)},
                required_action="Repair the run's normalization stage ownership before dispatch.",
            )
        return values[0]

    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("normalization applier clock must return timezone-aware UTC")
        return value


def normalization_transition_appliers(
    work_engine: PostgresWorkEngine,
    artifact_publisher: PostgresOwnedArtifactPublisher,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, PostgresNormalizationApplier]:
    """Build the extracted-record-to-normalization applier registration."""
    return {
        _NORMALIZATION_TRANSITION_KEY: PostgresNormalizationApplier(
            work_engine,
            artifact_publisher,
            clock=clock,
        ),
    }
