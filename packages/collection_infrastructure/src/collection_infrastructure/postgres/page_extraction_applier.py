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
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    SucceededWorkOutput,
    extraction_work_result_digest,
)
from collection_application.work_engine import WorkEngineConflict
from collection_infrastructure.postgres.owned_artifact_publisher import (
    PostgresOwnedArtifactPublisher,
)
from collection_infrastructure.postgres.stage_start import start_scheduled_stage
from collection_infrastructure.postgres.work_engine import PostgresWorkEngine
from collection_infrastructure.postgres.work_metadata import stage_runs

_PAGE_EXTRACTION_TRANSITION_KEY = "official-http-page-extraction-v1"
_ARTIFACT_NAMESPACE = UUID("ac9c3662-cc95-4a3f-8f96-6521847e46e3")
_PRODUCER_IDENTITY = "pipeline-page-extraction"
_RAW_ROLE = "raw_source_document"
_REQUEST_ROLE = "extraction_request"
_RAW_OUTPUT_ROLE = "http_raw_response"
_REQUEST_CONTENT_TYPE = "application/vnd.collection.extraction-request+json"
_OUTPUT_CONTRACT = "extracted-record@1"


class PostgresPageExtractionApplier:
    """Atomically records a validated page body and its deterministic extraction work."""

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
        raw_artifacts = tuple(
            item
            for item in (
                *source.related_output_artifacts,
                *source.input_artifacts,
            )
            if item.role in {_RAW_OUTPUT_ROLE, "previous_raw_artifact"}
        )
        if (
            plan.source_work_unit_id != source.source_work_unit_id
            or plan.transition_key != _PAGE_EXTRACTION_TRANSITION_KEY
            or source.stage is not WorkStage.ACQUISITION
            or source.capability != WorkCapability.HTTP_FETCH.value
            or source.output_contract != "official-http-acquisition@1"
            or source.output_artifact.role != "http_acquisition_manifest"
            or source.output_content is None
            or source.source_binding_key is None
            or source.config_bundle_digest is None
            or plan.disposition is not PipelineTransitionDisposition.APPLY
            or plan.blocker is not None
            or len(plan.extraction_actions) != 1
            or plan.website_actions
            or len(raw_artifacts) != 1
            or raw_artifacts[0].artifact_id != plan.extraction_actions[0].raw_artifact_id
        ):
            raise PipelineAdvancementConflict(
                code="PIPELINE_PAGE_EXTRACTION_PLAN_INVALID",
                message="The page extraction applier received unrelated or incomplete evidence.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "transitionKey": plan.transition_key,
                    "actionCount": len(plan.extraction_actions),
                },
                required_action="Recompute and apply the exact successful page extraction plan.",
            )

        action = plan.extraction_actions[0]
        if action.source_binding_key != source.source_binding_key:
            raise PipelineAdvancementConflict(
                code="PIPELINE_PAGE_EXTRACTION_BINDING_CONFLICT",
                message="The extraction action differs from the page's source binding provenance.",
                context={"workId": str(action.work_id)},
                required_action="Recompute extraction from the exact page route and run snapshot.",
            )

        stage_run_id = self._extraction_stage_run_id(connection, source.run_id)
        now_utc = self._now_utc()
        start_scheduled_stage(
            connection,
            stage_run_id=stage_run_id,
            stage_name="extraction",
            code_prefix="PIPELINE_PAGE_EXTRACTION",
            now_utc=now_utc,
            correlation_id=correlation_id,
        )
        request_artifact_id = self._publish_request(
            connection,
            work_id=action.work_id,
            semantic_key=action.semantic_key,
            content=action.request_payload,
            source_policy_digest=action.source_policy_digest,
            correlation_id=correlation_id,
        )
        try:
            spec = WorkUnitSpec(
                work_id=action.work_id,
                run_id=source.run_id,
                stage_run_id=stage_run_id,
                stage=WorkStage.EXTRACTION,
                capability=WorkCapability.EXTRACTION,
                source_key=None,
                source_binding_key=action.source_binding_key,
                semantic_key=action.semantic_key,
                input_digest=action.input_digest,
                expected_output_contract=_OUTPUT_CONTRACT,
                priority=0,
                retry_policy=RetryPolicy(
                    max_attempts=action.retry_budget + 1,
                    initial_delay_seconds=30,
                    multiplier=2,
                    max_delay_seconds=900,
                ),
                available_at_utc=now_utc,
                correlation_id=correlation_id,
                input_artifacts=(
                    WorkInputArtifact(
                        artifact_id=action.raw_artifact_id,
                        role=_RAW_ROLE,
                    ),
                    WorkInputArtifact(
                        artifact_id=request_artifact_id,
                        role=_REQUEST_ROLE,
                    ),
                    WorkInputArtifact(
                        artifact_id=action.manual_record_lineage_artifact.artifact_id,
                        role="manual_record_lineage",
                    ),
                ),
            )
        except ValueError as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_PAGE_EXTRACTION_WORK_INVALID",
                message="The page extraction plan cannot form a valid extraction work unit.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "workId": str(action.work_id),
                    "causeType": type(exc).__name__,
                },
                required_action="Correct the exact page evidence or extraction contract.",
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
        return extraction_work_result_digest(plan)

    def _publish_request(
        self,
        connection: Connection,
        *,
        work_id: UUID,
        semantic_key: str,
        content: bytes,
        source_policy_digest: str,
        correlation_id: str,
    ) -> UUID:
        role = _REQUEST_ROLE
        operation_id = uuid5(_ARTIFACT_NAMESPACE, f"{semantic_key}:{role}")
        artifact_id = uuid5(_ARTIFACT_NAMESPACE, f"artifact:{operation_id}")
        try:
            published = self._artifact_publisher.publish_in_transaction(
                connection,
                PublishOwnedArtifact(
                    artifact_id=artifact_id,
                    operation_id=operation_id,
                    producer_identity=_PRODUCER_IDENTITY,
                    artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                    content=content,
                    content_type=_REQUEST_CONTENT_TYPE,
                    source_policy_digest=source_policy_digest,
                    correlation_id=correlation_id,
                ),
            )
        except OwnedArtifactPublishConflict as exc:
            raise PipelineAdvancementConflict(
                code=exc.code,
                message=exc.message,
                context={"workId": str(work_id), "artifactRole": role, **exc.context},
                required_action=exc.required_action,
            ) from exc
        return published.artifact_id

    @staticmethod
    def _extraction_stage_run_id(connection: Connection, run_id: UUID) -> UUID:
        values = tuple(
            connection.execute(
                sa.select(stage_runs.c.stage_run_id).where(
                    stage_runs.c.run_id == run_id,
                    stage_runs.c.stage == WorkStage.EXTRACTION.value,
                )
            )
            .scalars()
            .all()
        )
        if len(values) != 1 or not isinstance(values[0], UUID):
            raise PipelineAdvancementConflict(
                code="PIPELINE_EXTRACTION_STAGE_OWNER_INVALID",
                message="The run must have exactly one canonical extraction stage owner.",
                context={"runId": str(run_id), "stageRunCount": len(values)},
                required_action="Repair the run's extraction stage ownership before dispatch.",
            )
        return values[0]

    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("page extraction applier clock must return timezone-aware UTC")
        return value


def page_extraction_transition_appliers(
    work_engine: PostgresWorkEngine,
    artifact_publisher: PostgresOwnedArtifactPublisher,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, PostgresPageExtractionApplier]:
    """Build the page-to-extraction applier registration for the Postgres composition root."""
    return {
        _PAGE_EXTRACTION_TRANSITION_KEY: PostgresPageExtractionApplier(
            work_engine,
            artifact_publisher,
            clock=clock,
        ),
    }
