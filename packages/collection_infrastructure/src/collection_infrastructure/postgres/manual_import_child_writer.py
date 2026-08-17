from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from collection_application import (
    RetryPolicy,
    WorkInputArtifact,
    WorkUnitSpec,
)
from collection_application.manual_import_admission import (
    MANUAL_RECORD_CAPABILITY,
    MANUAL_RECORD_STAGE,
    AdmitManualImportPlan,
    ManualImportChildWork,
)
from collection_infrastructure.postgres.manual_import_admission import (
    ManualImportAdmissionConflict,
)
from collection_infrastructure.postgres.work_engine import PostgresWorkEngine
from collection_infrastructure.postgres.work_metadata import stage_runs
from sqlalchemy.engine import Connection, Engine


class PostgresManualImportChildWorkWriter:
    """Enqueues canonical manual-record work in the admission transaction."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._work_engine = PostgresWorkEngine(engine, clock=self._clock)

    def enqueue(
        self,
        connection: Connection,
        command: AdmitManualImportPlan,
        children: Sequence[ManualImportChildWork],
    ) -> tuple[UUID, ...]:
        stage_run_id = _stage_run_id(connection, command)
        available_at_utc = self._now_utc()
        for child in children:
            artifacts = (
                WorkInputArtifact(
                    artifact_id=command.plan.source_artifact_id,
                    role=command.plan.source_artifact_role,
                ),
                WorkInputArtifact(
                    artifact_id=command.plan.plan_artifact_id,
                    role=f"manual_import_plan_record:{child.position}",
                ),
            )
            spec = WorkUnitSpec(
                work_id=child.work_id,
                run_id=command.run_id,
                stage_run_id=stage_run_id,
                stage=MANUAL_RECORD_STAGE,
                capability=MANUAL_RECORD_CAPABILITY,
                source_key=None,
                semantic_key=child.semantic_key,
                input_digest=child.input_digest,
                expected_output_contract=command.target_output_contract,
                priority=0,
                retry_policy=RetryPolicy(
                    max_attempts=3,
                    initial_delay_seconds=30,
                    multiplier=2,
                    max_delay_seconds=900,
                ),
                available_at_utc=available_at_utc,
                correlation_id=command.correlation_id,
                input_artifacts=artifacts,
            )
            self._work_engine.enqueue_work_in_transaction(connection, spec)
        return tuple(child.work_id for child in children)

    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("manual import child-work clock must return UTC")
        return value


def _stage_run_id(
    connection: Connection,
    command: AdmitManualImportPlan,
) -> UUID:
    rows = (
        connection.execute(
            sa.select(stage_runs.c.stage_run_id).where(
                stage_runs.c.run_id == command.run_id,
                stage_runs.c.stage == MANUAL_RECORD_STAGE.value,
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != 1:
        raise _conflict(
            command,
            code="MANUAL_IMPORT_TARGET_STAGE_RUN_UNAVAILABLE",
            message="The discovery stage has no unique stage-run owner.",
        )
    return UUID(str(rows[0]))


def _conflict(
    command: AdmitManualImportPlan,
    *,
    code: str,
    message: str,
) -> ManualImportAdmissionConflict:
    return ManualImportAdmissionConflict(
        code=code,
        message=message,
        context={
            "admissionId": str(command.admission_id),
            "planDigest": command.plan.plan_digest,
        },
        required_action=("Align admission with the canonical discovery Work Engine owner."),
    )
