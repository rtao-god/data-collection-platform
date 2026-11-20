from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from collection_application import StageRunState
from collection_application.pipeline_advancement import PipelineAdvancementConflict
from collection_infrastructure.postgres.work_metadata import stage_runs


def start_scheduled_stage(
    connection: Connection,
    *,
    stage_run_id: object,
    stage_name: str,
    code_prefix: str,
    now_utc: datetime,
    correlation_id: str,
) -> None:
    """Make the stage that receives newly scheduled work operational.

    Bootstrap starts only DISCOVERY, so scheduling work into a still-pending stage would
    produce work units no worker can lease. A stage that is already running is left
    untouched; a finished or blocked stage is refused instead of being reopened by a
    scheduler.
    """

    state = connection.execute(
        sa.select(stage_runs.c.state).where(stage_runs.c.stage_run_id == stage_run_id)
    ).scalar_one_or_none()
    if state is None:
        raise PipelineAdvancementConflict(
            code=f"{code_prefix}_STAGE_OWNER_INVALID",
            message=f"The scheduled {stage_name} stage no longer exists.",
            context={"stageRunId": str(stage_run_id)},
            required_action=(
                f"Repair the run's stage ownership before scheduling {stage_name} work."
            ),
        )
    if state == StageRunState.RUNNING.value:
        return
    if state != StageRunState.PENDING.value:
        raise PipelineAdvancementConflict(
            code=f"{code_prefix}_STAGE_NOT_STARTABLE",
            message=(
                f"The scheduled {stage_name} stage is not pending, so new work cannot start it."
            ),
            context={"stageRunId": str(stage_run_id), "stageState": str(state)},
            required_action=(
                f"Resolve the {stage_name} stage's own state before scheduling work into it."
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
