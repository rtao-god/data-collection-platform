from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from collection_application.run_control import (
    CollectionRunStatus,
    RunControlConflict,
    RunCoverageReport,
    StageRunStatus,
    TransitionCollectionRun,
    WorkStateCount,
    coverage_from_status,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from collection_application import (
    CollectionRunLifecycle,
    CollectionRunState,
    InvalidRunTransition,
    StageRunState,
    WorkStage,
    WorkUnitState,
)
from collection_infrastructure.postgres.work_metadata import (
    collection_run_transitions,
    collection_runs,
    stage_runs,
    work_units,
)

_STAGE_ORDER = {stage: position for position, stage in enumerate(WorkStage)}
_WORK_STATE_ORDER = {state: position for position, state in enumerate(WorkUnitState)}


class PostgresRunControlRepository:
    """Canonical PostgreSQL owner for collection-run reads and operator transitions."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4

    def get(self, run_id: UUID) -> CollectionRunStatus:
        try:
            with self._engine.connect() as connection:
                return self._load_status(connection, run_id)
        except RunControlConflict:
            raise
        except SQLAlchemyError as exc:
            raise _storage_conflict(run_id, exc) from exc

    def coverage(self, run_id: UUID) -> RunCoverageReport:
        return coverage_from_status(self.get(run_id))

    def transition(self, command: TransitionCollectionRun) -> CollectionRunStatus:
        now_utc = self._now_utc()
        try:
            with self._engine.begin() as connection:
                current = self._lock_run(connection, command.run_id)
                current_revision = int(current["revision"])
                if current_revision != command.expected_revision:
                    raise RunControlConflict(
                        code="RUN_REVISION_CONFLICT",
                        message="The collection run changed after the operator loaded it.",
                        context={
                            "runId": str(command.run_id),
                            "expectedRevision": command.expected_revision,
                            "actualRevision": current_revision,
                        },
                        required_action=(
                            "Reload the collection run and retry against its current revision."
                        ),
                    )
                lifecycle = CollectionRunLifecycle(
                    state=CollectionRunState(str(current["state"])),
                    revision=current_revision,
                )
                try:
                    next_lifecycle = lifecycle.transition(command.requested_state)
                except InvalidRunTransition as exc:
                    raise RunControlConflict(
                        code="RUN_TRANSITION_INVALID",
                        message=str(exc),
                        context={
                            "runId": str(command.run_id),
                            "fromState": lifecycle.state.value,
                            "toState": command.requested_state.value,
                        },
                        required_action=(
                            "Reload the run and choose a transition allowed by its current state."
                        ),
                    ) from exc
                if command.requested_state is CollectionRunState.RUNNING:
                    self._validate_resume(connection, command.run_id)
                if command.requested_state is CollectionRunState.CANCELLED:
                    self._cancel_pending_work(connection, command, now_utc)
                updated = connection.execute(
                    sa.update(collection_runs)
                    .where(
                        collection_runs.c.run_id == command.run_id,
                        collection_runs.c.revision == command.expected_revision,
                    )
                    .values(
                        state=next_lifecycle.state.value,
                        revision=next_lifecycle.revision,
                        updated_at_utc=now_utc,
                        correlation_id=command.correlation_id,
                    )
                )
                if updated.rowcount != 1:
                    raise RunControlConflict(
                        code="RUN_REVISION_CONFLICT",
                        message="The collection run changed during the operator transition.",
                        context={
                            "runId": str(command.run_id),
                            "expectedRevision": command.expected_revision,
                        },
                        required_action=(
                            "Reload the collection run and retry against its current revision."
                        ),
                    )
                connection.execute(
                    sa.insert(collection_run_transitions).values(
                        transition_id=self._uuid_factory(),
                        run_id=command.run_id,
                        from_state=lifecycle.state.value,
                        to_state=next_lifecycle.state.value,
                        from_revision=lifecycle.revision,
                        to_revision=next_lifecycle.revision,
                        actor_id=command.actor_id,
                        reason=command.reason,
                        changed_at_utc=now_utc,
                        correlation_id=command.correlation_id,
                    )
                )
                return self._load_status(connection, command.run_id)
        except RunControlConflict:
            raise
        except SQLAlchemyError as exc:
            raise _storage_conflict(command.run_id, exc) from exc

    def _lock_run(self, connection: Connection, run_id: UUID) -> RowMapping:
        row = (
            connection.execute(
                sa.select(collection_runs)
                .where(collection_runs.c.run_id == run_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise _not_found(run_id)
        return row

    def _load_status(self, connection: Connection, run_id: UUID) -> CollectionRunStatus:
        run = (
            connection.execute(sa.select(collection_runs).where(collection_runs.c.run_id == run_id))
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise _not_found(run_id)
        stage_rows = (
            connection.execute(sa.select(stage_runs).where(stage_runs.c.run_id == run_id))
            .mappings()
            .all()
        )
        count_rows = (
            connection.execute(
                sa.select(
                    work_units.c.stage_run_id,
                    work_units.c.state,
                    sa.func.count().label("count"),
                )
                .where(work_units.c.run_id == run_id)
                .group_by(work_units.c.stage_run_id, work_units.c.state)
            )
            .mappings()
            .all()
        )
        counts_by_stage: dict[UUID, list[WorkStateCount]] = {}
        for row in count_rows:
            stage_run_id = UUID(str(row["stage_run_id"]))
            counts_by_stage.setdefault(stage_run_id, []).append(
                WorkStateCount(
                    state=WorkUnitState(str(row["state"])),
                    count=int(row["count"]),
                )
            )
        stages = tuple(
            StageRunStatus(
                stage_run_id=UUID(str(row["stage_run_id"])),
                stage=WorkStage(str(row["stage"])),
                state=StageRunState(str(row["state"])),
                revision=int(row["revision"]),
                work_counts=tuple(
                    sorted(
                        counts_by_stage.get(UUID(str(row["stage_run_id"])), []),
                        key=lambda item: _WORK_STATE_ORDER[item.state],
                    )
                ),
            )
            for row in sorted(
                stage_rows,
                key=lambda item: _STAGE_ORDER[WorkStage(str(item["stage"]))],
            )
        )
        return CollectionRunStatus(
            run_id=UUID(str(run["run_id"])),
            campaign_key=str(run["campaign_key"]),
            config_bundle_digest=str(run["config_bundle_digest"]),
            state=CollectionRunState(str(run["state"])),
            revision=int(run["revision"]),
            created_at_utc=_datetime(run, "created_at_utc"),
            updated_at_utc=_datetime(run, "updated_at_utc"),
            stages=stages,
        )

    def _validate_resume(self, connection: Connection, run_id: UUID) -> None:
        states = tuple(
            StageRunState(str(value))
            for value in connection.execute(
                sa.select(stage_runs.c.state).where(stage_runs.c.run_id == run_id)
            ).scalars()
        )
        if not states:
            raise RunControlConflict(
                code="RUN_RESUME_BLOCKED",
                message="The paused collection run has no persisted stage owners.",
                context={"runId": str(run_id)},
                required_action="Repair the run stage ownership before resuming it.",
            )
        blocking = sorted(
            {
                state.value
                for state in states
                if state
                in {
                    StageRunState.FAILED,
                    StageRunState.BLOCKED,
                    StageRunState.CANCELLED,
                }
            }
        )
        resumable = any(state in {StageRunState.PENDING, StageRunState.RUNNING} for state in states)
        if blocking or not resumable:
            raise RunControlConflict(
                code="RUN_RESUME_BLOCKED",
                message="The paused collection run does not have a resumable stage state.",
                context={"runId": str(run_id), "blockingStageStates": blocking},
                required_action=(
                    "Resolve blocked or failed stage ownership, or complete the run instead of "
                    "resuming it."
                ),
            )

    def _cancel_pending_work(
        self,
        connection: Connection,
        command: TransitionCollectionRun,
        now_utc: datetime,
    ) -> None:
        connection.execute(
            sa.update(work_units)
            .where(
                work_units.c.run_id == command.run_id,
                work_units.c.state.in_(
                    (WorkUnitState.PENDING.value, WorkUnitState.RETRY_WAIT.value)
                ),
            )
            .values(
                state=WorkUnitState.CANCELLED.value,
                revision=work_units.c.revision + 1,
                updated_at_utc=now_utc,
                correlation_id=command.correlation_id,
            )
        )
        active_lease_exists = sa.exists(
            sa.select(sa.literal(1)).where(
                work_units.c.stage_run_id == stage_runs.c.stage_run_id,
                work_units.c.state == WorkUnitState.LEASED.value,
            )
        )
        connection.execute(
            sa.update(stage_runs)
            .where(
                stage_runs.c.run_id == command.run_id,
                stage_runs.c.state.in_((StageRunState.PENDING.value, StageRunState.RUNNING.value)),
                ~active_lease_exists,
            )
            .values(
                state=StageRunState.CANCELLED.value,
                revision=stage_runs.c.revision + 1,
                updated_at_utc=now_utc,
                correlation_id=command.correlation_id,
            )
        )

    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Run Control clock must return timezone-aware UTC")
        return value


def _not_found(run_id: UUID) -> RunControlConflict:
    return RunControlConflict(
        code="RUN_NOT_FOUND",
        message="The requested collection run does not exist.",
        context={"runId": str(run_id)},
        required_action="Refresh run state and select an existing collection run.",
    )


def _storage_conflict(run_id: UUID, exc: SQLAlchemyError) -> RunControlConflict:
    return RunControlConflict(
        code="RUN_STORAGE_FAILED",
        message="The Run Control database operation did not complete.",
        context={"runId": str(run_id), "causeType": type(exc).__name__},
        required_action="Inspect the PostgreSQL owner state and retry the exact operation.",
    )


def _datetime(row: RowMapping, key: str) -> datetime:
    value = row[key]
    if not isinstance(value, datetime):
        raise TypeError(f"persisted {key} is not a datetime")
    return value
