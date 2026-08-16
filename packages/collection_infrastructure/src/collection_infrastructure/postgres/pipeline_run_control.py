from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from collection_application import WorkStage
from collection_application.pipeline_advancement import PipelineAdvancementState
from collection_application.run_control import (
    CollectionRunStatus,
    RunControlConflict,
    RunControlPort,
    RunCoverageBlocker,
    RunCoverageReport,
    TransitionCollectionRun,
)
from collection_infrastructure.postgres.pipeline_advancement_metadata import (
    pipeline_advancements,
)

_BLOCKING_STATES = (
    PipelineAdvancementState.PENDING.value,
    PipelineAdvancementState.LEASED.value,
    PipelineAdvancementState.BLOCKED.value,
)


class PipelineAwareRunControlRepository:
    """Run Control decorator that exposes durable pipeline advancement blockers."""

    def __init__(self, delegate: RunControlPort, engine: Engine) -> None:
        self._delegate = delegate
        self._engine = engine

    def get(self, run_id: UUID) -> CollectionRunStatus:
        return self._delegate.get(run_id)

    def transition(self, command: TransitionCollectionRun) -> CollectionRunStatus:
        return self._delegate.transition(command)

    def coverage(self, run_id: UUID) -> RunCoverageReport:
        base = self._delegate.coverage(run_id)
        try:
            with self._engine.connect() as connection:
                rows = (
                    connection.execute(
                        sa.select(
                            pipeline_advancements.c.source_stage,
                            pipeline_advancements.c.state,
                            pipeline_advancements.c.blocker_owner,
                            pipeline_advancements.c.blocker_code,
                            pipeline_advancements.c.blocker_message,
                            pipeline_advancements.c.blocker_required_action,
                            pipeline_advancements.c.blocker_context,
                            sa.func.count().label("count"),
                        )
                        .where(pipeline_advancements.c.run_id == run_id)
                        .where(pipeline_advancements.c.state.in_(_BLOCKING_STATES))
                        .group_by(
                            pipeline_advancements.c.source_stage,
                            pipeline_advancements.c.state,
                            pipeline_advancements.c.blocker_owner,
                            pipeline_advancements.c.blocker_code,
                            pipeline_advancements.c.blocker_message,
                            pipeline_advancements.c.blocker_required_action,
                            pipeline_advancements.c.blocker_context,
                        )
                        .order_by(
                            pipeline_advancements.c.source_stage,
                            pipeline_advancements.c.state,
                            pipeline_advancements.c.blocker_code,
                        )
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError as exc:
            raise RunControlConflict(
                code="RUN_COVERAGE_STORAGE_FAILED",
                message="Pipeline advancement coverage could not be read.",
                context={"runId": str(run_id), "causeType": type(exc).__name__},
                required_action="Inspect PostgreSQL pipeline owner state and retry coverage.",
            ) from exc
        pipeline_blockers = tuple(_coverage_blocker(row) for row in rows)
        return RunCoverageReport(
            run_id=base.run_id,
            state=base.state,
            revision=base.revision,
            stages=base.stages,
            blockers=base.blockers + pipeline_blockers,
        )


def _coverage_blocker(row: RowMapping) -> RunCoverageBlocker:
    stage = WorkStage(str(row["source_stage"]))
    state = PipelineAdvancementState(str(row["state"]))
    count = _required_non_negative_int(row, "count")
    if state is PipelineAdvancementState.PENDING:
        return RunCoverageBlocker(
            code="PIPELINE_ADVANCEMENT_PENDING",
            stage=stage,
            count=count,
            message="Successful work is waiting for durable pipeline advancement.",
            required_action="Run the pipeline supervisor until the pending queue is empty.",
        )
    if state is PipelineAdvancementState.LEASED:
        return RunCoverageBlocker(
            code="PIPELINE_ADVANCEMENT_LEASED",
            stage=stage,
            count=count,
            message="Successful work is held by active pipeline advancement leases.",
            required_action=(
                "Wait for the owning execution or reclaim the work after lease expiry."
            ),
        )
    if state is PipelineAdvancementState.BLOCKED:
        code = _required_text(row, "blocker_code")
        return RunCoverageBlocker(
            code=code,
            stage=stage,
            count=count,
            message=_required_text(row, "blocker_message"),
            required_action=_required_text(row, "blocker_required_action"),
        )
    return RunCoverageBlocker(
        code="PIPELINE_ADVANCEMENT_APPLIED",
        stage=stage,
        count=count,
        message="Pipeline advancement completed but is still included in a blocker query.",
        required_action="Repair the coverage query because applied advancement is not a blocker.",
    )


def _required_text(row: RowMapping, key: str) -> str:
    value = row[key]
    if not isinstance(value, str) or not value:
        raise TypeError(f"persisted {key} is not non-empty text")
    return value


def _required_non_negative_int(row: RowMapping, key: str) -> int:
    value = row[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError(f"persisted {key} is not a non-negative integer")
    return value
