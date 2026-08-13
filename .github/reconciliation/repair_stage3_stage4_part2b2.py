from __future__ import annotations

from pathlib import Path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def apply(root: Path) -> None:
    _write(
        root / "packages/collection_infrastructure/src/collection_infrastructure/postgres/manual_import_child_writer.py",
        '''from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from collection_application import (
    RetryPolicy,
    WorkCapability,
    WorkInputArtifact,
    WorkStage,
    WorkUnitSpec,
)
from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportChildWork,
)
from collection_infrastructure.postgres.manual_import_admission import (
    ManualImportAdmissionConflict,
)
from collection_infrastructure.postgres.work_engine import PostgresWorkEngine
from collection_infrastructure.postgres.work_metadata import stage_runs


class PostgresManualImportChildWorkWriter:
    """Enqueues child work through the Work Engine in the admission transaction."""

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
        target_stage = _stage(command)
        target_capability = _capability(command)
        stage_run_id = _stage_run_id(connection, command, target_stage)
        available_at_utc = self._now_utc()
        artifacts = (
            WorkInputArtifact(
                artifact_id=command.plan.source_artifact_id,
                role="manual_import_source",
            ),
            WorkInputArtifact(
                artifact_id=command.plan.plan_artifact_id,
                role="manual_import_plan",
            ),
        )
        for child in children:
            spec = WorkUnitSpec(
                work_id=child.work_id,
                run_id=command.run_id,
                stage_run_id=stage_run_id,
                stage=target_stage,
                capability=target_capability,
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


def _stage(command: AdmitManualImportPlan) -> WorkStage:
    try:
        return WorkStage(command.target_stage)
    except ValueError as exc:
        raise _conflict(
            command,
            code="MANUAL_IMPORT_TARGET_STAGE_INVALID",
            message="The manual import target stage is not supported by the Work Engine.",
        ) from exc


def _capability(command: AdmitManualImportPlan) -> WorkCapability:
    try:
        return WorkCapability(command.target_capability)
    except ValueError as exc:
        raise _conflict(
            command,
            code="MANUAL_IMPORT_TARGET_CAPABILITY_INVALID",
            message="The manual import target capability is not supported by the Work Engine.",
        ) from exc


def _stage_run_id(
    connection: Connection,
    command: AdmitManualImportPlan,
    target_stage: WorkStage,
) -> UUID:
    rows = connection.execute(
        sa.select(stage_runs.c.stage_run_id).where(
            stage_runs.c.run_id == command.run_id,
            stage_runs.c.stage == target_stage.value,
        )
    ).scalars().all()
    if len(rows) != 1:
        raise _conflict(
            command,
            code="MANUAL_IMPORT_TARGET_STAGE_RUN_UNAVAILABLE",
            message="The target stage has no unique stage-run owner.",
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
        required_action=(
            "Align the manual import admission with the canonical Work Engine owner."
        ),
    )
''',
    )
