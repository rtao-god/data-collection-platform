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
    website_route_result_digest,
)
from collection_application.work_engine import WorkEngineConflict
from collection_infrastructure.postgres.owned_artifact_publisher import (
    PostgresOwnedArtifactPublisher,
)
from collection_infrastructure.postgres.stage_start import start_scheduled_stage
from collection_infrastructure.postgres.work_engine import PostgresWorkEngine
from collection_infrastructure.postgres.work_metadata import stage_runs

_ROBOTS_TRANSITION_KEY = "official-http-robots-routing-v1"

_TRANSITION_KEY = "manual-record-website-routing-v1"
_ARTIFACT_NAMESPACE = UUID("fef5ec0d-0782-52ef-bdf9-94e56c5ef040")
_PRODUCER_IDENTITY = "pipeline-website-route"
_REQUEST_ROLE = "http_request"
_CONTEXT_ROLE = "website_route_context"
_REQUEST_CONTENT_TYPE = "application/vnd.collection.official-http-request+json"
_CONTEXT_CONTENT_TYPE = "application/vnd.collection.website-route-context+json"
_MANUAL_RECORD_LINEAGE_ROLE = "manual_record_lineage"
_MANUAL_RECORD_LINEAGE_CONTENT_TYPE = "application/vnd.collection.manual-record-lineage+json"
_OUTPUT_CONTRACT = "official-http-acquisition@1"


class PostgresWebsiteRouteApplier:
    """Atomically records route inputs, HTTP work, and advancement completion."""

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
        manual_route = (
            plan.transition_key == _TRANSITION_KEY
            and source.stage is WorkStage.DISCOVERY
            and source.capability == "manual_record"
            and source.output_contract == "manual-import-record@1"
            and source.campaign_bundle_content is not None
        )
        robots_route = (
            plan.transition_key == _ROBOTS_TRANSITION_KEY
            and source.stage is WorkStage.ACQUISITION
            and source.capability == "http_fetch"
            and source.output_contract == _OUTPUT_CONTRACT
            and source.output_artifact.role == "http_acquisition_manifest"
        )
        if (
            plan.source_work_unit_id != source.source_work_unit_id
            or not (manual_route or robots_route)
            or source.source_binding_key is None
            or source.config_bundle_digest is None
            or source.output_content is None
            or plan.disposition is not PipelineTransitionDisposition.APPLY
            or plan.blocker is not None
            or not plan.website_actions
        ):
            raise PipelineAdvancementConflict(
                code="PIPELINE_WEBSITE_ROUTE_PLAN_INVALID",
                message="The website route applier received a non-applicable or unrelated plan.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "transitionKey": plan.transition_key,
                    "actionCount": len(plan.website_actions),
                },
                required_action="Recompute and apply the exact registered website route plan.",
            )

        stage_run_id = self._acquisition_stage_run_id(connection, source.run_id)
        now_utc = self._now_utc()
        action_ids = tuple(action.work_id for action in plan.website_actions)
        if len(action_ids) != len(set(action_ids)):
            raise PipelineAdvancementConflict(
                code="PIPELINE_WEBSITE_ROUTE_ACTION_DUPLICATE",
                message="The website route plan contains duplicate downstream work identities.",
                context={"sourceWorkUnitId": str(source.source_work_unit_id)},
                required_action="Correct the route planner before retrying advancement.",
            )

        if plan.website_actions:
            start_scheduled_stage(
                connection,
                stage_run_id=stage_run_id,
                stage_name="acquisition",
                code_prefix="PIPELINE_WEBSITE_ROUTE",
                now_utc=now_utc,
                correlation_id=correlation_id,
            )

        for action in plan.website_actions:
            request_artifact_id = self._publish_artifact(
                connection,
                work_id=action.work_id,
                semantic_key=action.semantic_key,
                role=_REQUEST_ROLE,
                content=action.request_payload,
                content_type=_REQUEST_CONTENT_TYPE,
                source_policy_digest=action.source_policy_digest,
                correlation_id=correlation_id,
            )
            context_artifact_id = self._publish_artifact(
                connection,
                work_id=action.work_id,
                semantic_key=action.semantic_key,
                role=_CONTEXT_ROLE,
                content=action.context_payload,
                content_type=_CONTEXT_CONTENT_TYPE,
                source_policy_digest=action.source_policy_digest,
                correlation_id=correlation_id,
            )
            lineage_artifact_id = None
            if action.manual_record_lineage_content is not None:
                lineage_artifact = action.manual_record_lineage_artifact
                if lineage_artifact is None:
                    raise PipelineAdvancementConflict(
                        code="PIPELINE_MANUAL_RECORD_LINEAGE_IDENTITY_MISSING",
                        message=(
                            "Manual-record lineage content has no deterministic artifact identity."
                        ),
                        context={"workId": str(action.work_id)},
                        required_action=(
                            "Recompute route lineage from the exact manual-record artifact."
                        ),
                    )
                lineage_artifact_id = self._publish_artifact(
                    connection,
                    work_id=action.work_id,
                    semantic_key=action.semantic_key,
                    role=_MANUAL_RECORD_LINEAGE_ROLE,
                    content=action.manual_record_lineage_content,
                    content_type=_MANUAL_RECORD_LINEAGE_CONTENT_TYPE,
                    source_policy_digest=action.source_policy_digest,
                    correlation_id=correlation_id,
                    artifact_id=lineage_artifact.artifact_id,
                )
                if lineage_artifact_id != lineage_artifact.artifact_id:
                    raise PipelineAdvancementConflict(
                        code="PIPELINE_MANUAL_RECORD_LINEAGE_IDENTITY_MISMATCH",
                        message=(
                            "Published lineage artifact differs from its planned exact identity."
                        ),
                        context={"workId": str(action.work_id)},
                        required_action=(
                            "Recompute and publish the deterministic lineage artifact."
                        ),
                    )
            elif action.manual_record_lineage_artifact is not None:
                lineage_artifact_id = action.manual_record_lineage_artifact.artifact_id
            try:
                spec = WorkUnitSpec(
                    work_id=action.work_id,
                    run_id=source.run_id,
                    stage_run_id=stage_run_id,
                    stage=WorkStage.ACQUISITION,
                    capability=WorkCapability.HTTP_FETCH,
                    source_key=action.source_key,
                    source_binding_key=action.target_binding_key,
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
                    input_artifacts=tuple(
                        [
                            WorkInputArtifact(
                                artifact_id=request_artifact_id,
                                role=_REQUEST_ROLE,
                            ),
                            WorkInputArtifact(artifact_id=context_artifact_id, role=_CONTEXT_ROLE),
                        ]
                        + (
                            [
                                WorkInputArtifact(
                                    artifact_id=action.robots_decision_artifact_id,
                                    role="robots_decision",
                                )
                            ]
                            if action.robots_decision_artifact_id is not None
                            else []
                        )
                        + (
                            [
                                WorkInputArtifact(
                                    artifact_id=action.robots_raw_artifact_id,
                                    role="robots_raw_response",
                                )
                            ]
                            if action.robots_raw_artifact_id is not None
                            else []
                        )
                        + (
                            [
                                WorkInputArtifact(
                                    artifact_id=lineage_artifact_id,
                                    role=_MANUAL_RECORD_LINEAGE_ROLE,
                                )
                            ]
                            if lineage_artifact_id is not None
                            else []
                        )
                    ),
                )
            except ValueError as exc:
                raise PipelineAdvancementConflict(
                    code="PIPELINE_WEBSITE_ROUTE_WORK_INVALID",
                    message="The website route plan cannot form a valid HTTP work unit.",
                    context={
                        "sourceWorkUnitId": str(source.source_work_unit_id),
                        "workId": str(action.work_id),
                        "causeType": type(exc).__name__,
                    },
                    required_action="Correct the route or source policy contract before retrying.",
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

        return website_route_result_digest(plan)

    def _publish_artifact(
        self,
        connection: Connection,
        *,
        work_id: UUID,
        semantic_key: str,
        role: str,
        content: bytes,
        content_type: str,
        source_policy_digest: str,
        correlation_id: str,
        artifact_id: UUID | None = None,
    ) -> UUID:
        operation_id = uuid5(_ARTIFACT_NAMESPACE, f"{semantic_key}:{role}")
        artifact_id = artifact_id or uuid5(_ARTIFACT_NAMESPACE, f"artifact:{operation_id}")
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
    def _acquisition_stage_run_id(connection: Connection, run_id: UUID) -> UUID:
        values = tuple(
            connection.execute(
                sa.select(stage_runs.c.stage_run_id).where(
                    stage_runs.c.run_id == run_id,
                    stage_runs.c.stage == WorkStage.ACQUISITION.value,
                )
            )
            .scalars()
            .all()
        )
        if len(values) != 1 or not isinstance(values[0], UUID):
            raise PipelineAdvancementConflict(
                code="PIPELINE_ACQUISITION_STAGE_OWNER_INVALID",
                message="The run must have exactly one canonical acquisition stage owner.",
                context={"runId": str(run_id), "stageRunCount": len(values)},
                required_action="Repair the run's stage ownership before creating HTTP work.",
            )
        return values[0]

    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("website route applier clock must return timezone-aware UTC")
        return value


def website_route_transition_appliers(
    work_engine: PostgresWorkEngine,
    artifact_publisher: PostgresOwnedArtifactPublisher,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, PostgresWebsiteRouteApplier]:
    """Build the exact transition-applier registration for a Postgres composition root."""
    return {
        _TRANSITION_KEY: PostgresWebsiteRouteApplier(
            work_engine,
            artifact_publisher,
            clock=clock,
        ),
        _ROBOTS_TRANSITION_KEY: PostgresWebsiteRouteApplier(
            work_engine,
            artifact_publisher,
            clock=clock,
        ),
    }
