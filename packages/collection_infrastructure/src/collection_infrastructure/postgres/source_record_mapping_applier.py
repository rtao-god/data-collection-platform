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
    StageRunState,
    WorkCapability,
    WorkInputArtifact,
    WorkStage,
    WorkUnitSpec,
)
from collection_application.pipeline_advancement import (
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    SourceRecordMappingAction,
    SucceededWorkOutput,
    source_record_mapping_result_digest,
)
from collection_application.work_engine import WorkEngineConflict
from collection_contracts.wikidata_entity_result import RESULT_CONTRACT as WIKIDATA_RESULT_CONTRACT
from collection_infrastructure.postgres.owned_artifact_publisher import (
    PostgresOwnedArtifactPublisher,
)
from collection_infrastructure.postgres.work_engine import PostgresWorkEngine
from collection_infrastructure.postgres.work_metadata import stage_runs

_OSM_RESULT_TRANSITION_KEY = "osm-overpass-result-source-record-mapping-v1"
_WIKIDATA_RESULT_TRANSITION_KEY = "wikidata-entity-result-source-record-mapping-v1"
# Every transition key this applier owns, with the exact acquisition output it consumes.
_SOURCE_RECORD_MAPPING_SOURCES = {
    _OSM_RESULT_TRANSITION_KEY: ("osm_query", "osm-overpass-result@1"),
    _WIKIDATA_RESULT_TRANSITION_KEY: ("wikidata_entity_query", WIKIDATA_RESULT_CONTRACT),
}
_ARTIFACT_NAMESPACE = UUID("4d8c2f17-9b63-5a0e-8c47-1f2e6a9b3d58")
_SOURCE_RECORD_ROLE = "source_record"
_PROFILE_ROLE = "source_mapping_profile"
_PROFILE_CONTENT_TYPE = "application/vnd.collection.source-mapping-profile+json"
_OUTPUT_CONTRACT = "source-record-observation-batch@1"
_PRODUCER_IDENTITY = "collection.infra.source-record-mapping"


class PostgresSourceRecordMappingApplier:
    """Schedules bounded source-native mapping work from a verified acquisition result.

    Owns no review state: the mapping worker produces an observation batch and the
    separate candidate-intake transition consumes it.
    """

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
                code="PIPELINE_SOURCE_RECORD_MAPPING_PLAN_INVALID",
                message="The source-record mapping applier received unrelated evidence.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "transitionKey": plan.transition_key,
                    "actionCount": len(plan.source_record_mapping_actions),
                },
                required_action=(
                    "Recompute source-record mapping from the exact acquisition result and "
                    "run snapshot."
                ),
            )
        stage_run_id = self._normalization_stage_run_id(connection, source.run_id)
        now_utc = self._now_utc()
        self._ensure_stage_running(
            connection,
            stage_run_id,
            now_utc=now_utc,
            correlation_id=correlation_id,
        )
        # One acquisition result fans out into one bounded mapping unit per source record,
        # so every unit is enqueued in the same transaction as its advancement.
        for action in plan.source_record_mapping_actions:
            self._enqueue_action(
                connection,
                source,
                action,
                stage_run_id=stage_run_id,
                now_utc=now_utc,
                correlation_id=correlation_id,
            )
        return source_record_mapping_result_digest(plan)

    def _enqueue_action(
        self,
        connection: Connection,
        source: SucceededWorkOutput,
        action: SourceRecordMappingAction,
        *,
        stage_run_id: UUID,
        now_utc: datetime,
        correlation_id: str,
    ) -> None:
        # The unit reads two artifacts that must exist before it can be leased: the exact
        # source-record document and the profile that says how to read it.
        record_artifact_id = self._publish_record(
            connection,
            action=action,
            correlation_id=correlation_id,
        )
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
                capability=WorkCapability.SOURCE_RECORD_MAPPING,
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
                        artifact_id=record_artifact_id,
                        role=_SOURCE_RECORD_ROLE,
                    ),
                    WorkInputArtifact(
                        artifact_id=profile_artifact_id,
                        role=_PROFILE_ROLE,
                    ),
                ),
            )
        except ValueError as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_MAPPING_WORK_INVALID",
                message="The mapping plan cannot form a valid source-record mapping work unit.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "workId": str(action.work_id),
                    "causeType": type(exc).__name__,
                },
                required_action="Correct the exact source record or mapping profile contract.",
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

    @staticmethod
    def _valid_plan(source: SucceededWorkOutput, plan: PipelineTransitionPlan) -> bool:
        actions = plan.source_record_mapping_actions
        if not actions:
            return False
        expected = _SOURCE_RECORD_MAPPING_SOURCES.get(plan.transition_key)
        if expected is None:
            return False
        capability, output_contract = expected
        return all(
            (
                plan.source_work_unit_id == source.source_work_unit_id,
                source.stage is WorkStage.DISCOVERY,
                source.capability == capability,
                source.output_contract == output_contract,
                source.source_binding_key is not None,
                all(action.source_binding_key == source.source_binding_key for action in actions),
                len({action.work_id for action in actions}) == len(actions),
                plan.disposition is PipelineTransitionDisposition.APPLY,
                plan.blocker is None,
                not plan.website_actions,
                not plan.extraction_actions,
                not plan.normalization_actions,
                not plan.candidate_intake_actions,
            )
        )

    def _publish_record(
        self,
        connection: Connection,
        *,
        action: SourceRecordMappingAction,
        correlation_id: str,
    ) -> UUID:
        """Persist the exact record document the mapping unit must read.

        The document is the planner's own single-record projection of the acquisition
        result, so its declared identity is published as-is: the worker then reads bytes
        that are already proven to belong to this record.
        """

        operation_id = uuid5(_ARTIFACT_NAMESPACE, f"{action.semantic_key}:{_SOURCE_RECORD_ROLE}")
        return self._publish(
            connection,
            artifact_id=action.source_record_artifact.artifact_id,
            operation_id=operation_id,
            role=_SOURCE_RECORD_ROLE,
            content=action.source_record_payload,
            content_type=action.source_record_artifact.content_type,
            action=action,
            correlation_id=correlation_id,
        )

    def _publish_profile(
        self,
        connection: Connection,
        *,
        action: SourceRecordMappingAction,
        correlation_id: str,
    ) -> UUID:
        operation_id = uuid5(_ARTIFACT_NAMESPACE, f"{action.semantic_key}:{_PROFILE_ROLE}")
        return self._publish(
            connection,
            artifact_id=uuid5(_ARTIFACT_NAMESPACE, f"artifact:{operation_id}"),
            operation_id=operation_id,
            role=_PROFILE_ROLE,
            content=action.mapping_profile_payload,
            content_type=_PROFILE_CONTENT_TYPE,
            action=action,
            correlation_id=correlation_id,
        )

    def _publish(
        self,
        connection: Connection,
        *,
        artifact_id: UUID,
        operation_id: UUID,
        role: str,
        content: bytes,
        content_type: str,
        action: SourceRecordMappingAction,
        correlation_id: str,
    ) -> UUID:
        try:
            published = self._artifact_publisher.publish_in_transaction(
                connection,
                PublishOwnedArtifact(
                    artifact_id=artifact_id,
                    operation_id=operation_id,
                    producer_identity=_PRODUCER_IDENTITY,
                    artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                    content=content,
                    content_type=content_type,
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
                    "artifactRole": role,
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
                code="PIPELINE_SOURCE_RECORD_MAPPING_STAGE_OWNER_INVALID",
                message="The run must have exactly one canonical normalization stage owner.",
                context={"runId": str(run_id), "stageRunCount": len(values)},
                required_action=(
                    "Repair the run's normalization stage ownership before mapping dispatch."
                ),
            )
        return values[0]

    @staticmethod
    def _ensure_stage_running(
        connection: Connection,
        stage_run_id: UUID,
        *,
        now_utc: datetime,
        correlation_id: str,
    ) -> None:
        """Make the stage that receives the new units operational.

        Bootstrap starts only DISCOVERY; a later stage stays pending until work is
        scheduled into it. Scheduling that work and leaving its stage pending would
        produce units no worker can lease, so the same transition starts the stage.
        A stage that is already running is left untouched; a finished or blocked stage
        is refused instead of being reopened by a scheduler.
        """

        state = connection.execute(
            sa.select(stage_runs.c.state).where(stage_runs.c.stage_run_id == stage_run_id)
        ).scalar_one_or_none()
        if state is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_MAPPING_STAGE_OWNER_INVALID",
                message="The scheduled stage no longer exists.",
                context={"stageRunId": str(stage_run_id)},
                required_action="Repair the run's stage ownership before mapping dispatch.",
            )
        if state == StageRunState.RUNNING.value:
            return
        if state != StageRunState.PENDING.value:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_MAPPING_STAGE_NOT_STARTABLE",
                message="The scheduled stage is not pending, so new work cannot start it.",
                context={"stageRunId": str(stage_run_id), "stageState": str(state)},
                required_action=(
                    "Resolve the stage's own state before scheduling mapping work into it."
                ),
            )
        connection.execute(
            sa.update(stage_runs)
            .where(
                stage_runs.c.stage_run_id == stage_run_id,
                stage_runs.c.state == StageRunState.PENDING.value,
            )
            .values(
                state=StageRunState.RUNNING.value,
                revision=stage_runs.c.revision + 1,
                updated_at_utc=now_utc,
                correlation_id=correlation_id,
            )
        )

    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("source-record mapping applier clock must return UTC")
        return value


def source_record_mapping_transition_appliers(
    work_engine: PostgresWorkEngine,
    artifact_publisher: PostgresOwnedArtifactPublisher,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, PostgresSourceRecordMappingApplier]:
    applier = PostgresSourceRecordMappingApplier(
        work_engine,
        artifact_publisher,
        clock=clock,
    )
    return dict.fromkeys(_SOURCE_RECORD_MAPPING_SOURCES, applier)
