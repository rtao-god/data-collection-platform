import json
import os

from dagster import (
    DefaultScheduleStatus,
    Definitions,
    MetadataValue,
    OpExecutionContext,
    RunRequest,
    ScheduleEvaluationContext,
    job,
    op,
    schedule,
)

from collector_orchestrator.gateway_client import run_pipeline_advancement


@op
def run_pipeline_advancement_once(context: OpExecutionContext) -> str:
    build_identity = os.environ.get("COLLECTOR_BUILD_IDENTITY", "").strip()
    if not build_identity:
        raise RuntimeError("COLLECTOR_BUILD_IDENTITY is required for Dagster advancement runs")
    tick = run_pipeline_advancement(
        dagster_execution_id=context.run_id,
        dagster_build_id=build_identity,
    )
    terminal_status = tick.terminal_status
    blocker = terminal_status.blocker if terminal_status is not None else None
    failures = [
        {"sourceWorkUnitId": str(item.source_work_unit_id), "code": item.code}
        for item in tick.discovery_failures
    ]
    status_metadata = (
        {
            "advancementId": str(terminal_status.advancement_id),
            "state": terminal_status.state,
            "blockerCode": blocker.code if blocker is not None else None,
        }
        if terminal_status is not None
        else None
    )
    metadata = {
        "registeredCount": MetadataValue.int(tick.registered_count),
        "claimedAdvancementId": MetadataValue.json(
            {
                "value": (
                    str(tick.claimed_advancement_id)
                    if tick.claimed_advancement_id is not None
                    else None
                )
            }
        ),
        "terminalStatus": MetadataValue.json({"value": status_metadata}),
        "discoveryFailures": MetadataValue.json(failures),
    }
    context.add_output_metadata(metadata)
    return json.dumps(
        {
            "registeredCount": tick.registered_count,
            "claimedAdvancementId": (
                str(tick.claimed_advancement_id)
                if tick.claimed_advancement_id is not None
                else None
            ),
            "terminalState": terminal_status.state if terminal_status is not None else None,
            "discoveryFailureCount": len(tick.discovery_failures),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


@job
def pipeline_advancement_job() -> None:
    run_pipeline_advancement_once()


@schedule(
    job=pipeline_advancement_job,
    cron_schedule="* * * * *",
    default_status=DefaultScheduleStatus.RUNNING,
)
def pipeline_advancement_schedule(_context: ScheduleEvaluationContext) -> RunRequest:
    return RunRequest()


definitions = Definitions(
    jobs=[pipeline_advancement_job],
    schedules=[pipeline_advancement_schedule],
)
