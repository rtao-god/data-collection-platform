from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

import sqlalchemy as sa
from collection_application.pipeline_advancement import (
    ApplyPipelineAdvancement,
    ArtifactIdentity,
    BlockPipelineAdvancement,
    ClaimPipelineAdvancement,
    PipelineAdvancementConflict,
    PipelineAdvancementLease,
    PipelineAdvancementState,
    PipelineAdvancementStatus,
    PipelineBlocker,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    SucceededWorkOutput,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from collection_infrastructure.postgres.pipeline_advancement_metadata import (
    pipeline_advancement_attempts,
    pipeline_advancements,
)


class CanonicalSucceededWorkReader(Protocol):
    def __call__(
        self,
        connection: Connection,
        source_work_unit_id: UUID,
    ) -> SucceededWorkOutput: ...


class PipelineTransitionApplier(Protocol):
    def __call__(
        self,
        connection: Connection,
        source: SucceededWorkOutput,
        plan: PipelineTransitionPlan,
        *,
        correlation_id: str,
    ) -> str: ...


class PostgresPipelineAdvancementRepository:
    """Sole durable checkpoint owner for successful-work pipeline advancement."""

    def __init__(
        self,
        engine: Engine,
        *,
        source_reader: CanonicalSucceededWorkReader,
        transition_appliers: Mapping[str, PipelineTransitionApplier],
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], UUID] | None = None,
        lease_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._engine = engine
        self._source_reader = source_reader
        self._transition_appliers = dict(transition_appliers)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory or uuid4
        self._lease_token_factory = lease_token_factory or (lambda: secrets.token_urlsafe(32))

    def register(
        self,
        source: SucceededWorkOutput,
        plan: PipelineTransitionPlan,
        *,
        correlation_id: str,
    ) -> PipelineAdvancementStatus:
        if source.source_work_unit_id != plan.source_work_unit_id:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_PLAN_CONFLICT",
                message="The transition plan belongs to a different source work unit.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "planSourceWorkUnitId": str(plan.source_work_unit_id),
                },
                required_action="Recompute the transition plan from the exact successful output.",
            )
        now_utc = self._now_utc()
        try:
            with self._engine.begin() as connection:
                canonical = self._source_reader(connection, source.source_work_unit_id)
                _assert_source_equal(source, canonical)
                existing = self._find_by_source(
                    connection,
                    source.source_work_unit_id,
                    for_update=True,
                )
                if existing is not None:
                    _assert_registration_equal(existing, canonical, plan)
                    return _status(existing)
                advancement_id = self._uuid_factory()
                state = (
                    PipelineAdvancementState.BLOCKED
                    if plan.disposition is PipelineTransitionDisposition.BLOCK
                    else PipelineAdvancementState.PENDING
                )
                blocker_values = _blocker_values(plan.blocker)
                connection.execute(
                    sa.insert(pipeline_advancements).values(
                        advancement_id=advancement_id,
                        **_source_values(canonical),
                        transition_key=plan.transition_key,
                        transition_plan_digest=plan.plan_digest,
                        state=state.value,
                        revision=0,
                        attempt_count=0,
                        result_digest=None,
                        **blocker_values,
                        active_lease_id=None,
                        active_lease_token_digest=None,
                        leased_by_worker_id=None,
                        dagster_execution_id=None,
                        dagster_build_id=None,
                        lease_issued_at_utc=None,
                        lease_expires_at_utc=None,
                        created_at_utc=now_utc,
                        updated_at_utc=now_utc,
                        correlation_id=correlation_id,
                    )
                )
                if state is PipelineAdvancementState.BLOCKED:
                    self._insert_event(
                        connection,
                        advancement_id=advancement_id,
                        attempt_number=0,
                        event_kind="registered_block",
                        lease_id=None,
                        lease_token_digest=None,
                        worker_id=None,
                        dagster_execution_id=None,
                        dagster_build_id=None,
                        transition_plan_digest=plan.plan_digest,
                        lease_expires_at_utc=None,
                        result_digest=None,
                        blocker=plan.blocker,
                        occurred_at_utc=now_utc,
                        correlation_id=correlation_id,
                    )
                row = self._get_locked(connection, advancement_id)
                return _status(row)
        except PipelineAdvancementConflict:
            raise
        except SQLAlchemyError as exc:
            raise _storage_conflict(source.source_work_unit_id, exc) from exc

    def claim(self, command: ClaimPipelineAdvancement) -> PipelineAdvancementLease | None:
        now_utc = self._now_utc()
        try:
            with self._engine.begin() as connection:
                self._expire_leases(connection, now_utc, command.correlation_id)
                row = (
                    connection.execute(
                        sa.select(pipeline_advancements)
                        .where(
                            pipeline_advancements.c.state == PipelineAdvancementState.PENDING.value
                        )
                        .order_by(
                            pipeline_advancements.c.created_at_utc,
                            pipeline_advancements.c.advancement_id,
                        )
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    return None
                advancement_id = _uuid(row, "advancement_id")
                source_work_unit_id = _uuid(row, "source_work_unit_id")
                lease_id = self._uuid_factory()
                lease_token = self._lease_token_factory()
                token_digest = _secret_digest(lease_token)
                attempt_number = int(row["attempt_count"]) + 1
                next_revision = int(row["revision"]) + 1
                expires_at_utc = now_utc + command.lease_duration
                updated = connection.execute(
                    sa.update(pipeline_advancements)
                    .where(
                        pipeline_advancements.c.advancement_id == advancement_id,
                        pipeline_advancements.c.revision == int(row["revision"]),
                        pipeline_advancements.c.state == PipelineAdvancementState.PENDING.value,
                    )
                    .values(
                        state=PipelineAdvancementState.LEASED.value,
                        revision=next_revision,
                        attempt_count=attempt_number,
                        active_lease_id=lease_id,
                        active_lease_token_digest=token_digest,
                        leased_by_worker_id=command.worker_id,
                        dagster_execution_id=command.dagster_execution_id,
                        dagster_build_id=command.dagster_build_id,
                        lease_issued_at_utc=now_utc,
                        lease_expires_at_utc=expires_at_utc,
                        updated_at_utc=now_utc,
                        correlation_id=command.correlation_id,
                    )
                )
                if updated.rowcount != 1:
                    raise PipelineAdvancementConflict(
                        code="PIPELINE_CLAIM_CONFLICT",
                        message="The pipeline advancement changed during lease acquisition.",
                        context={"advancementId": str(advancement_id)},
                        required_action="Retry claim from the canonical pending queue.",
                    )
                self._insert_event(
                    connection,
                    advancement_id=advancement_id,
                    attempt_number=attempt_number,
                    event_kind="leased",
                    lease_id=lease_id,
                    lease_token_digest=token_digest,
                    worker_id=command.worker_id,
                    dagster_execution_id=command.dagster_execution_id,
                    dagster_build_id=command.dagster_build_id,
                    transition_plan_digest=str(row["transition_plan_digest"]),
                    lease_expires_at_utc=expires_at_utc,
                    result_digest=None,
                    blocker=None,
                    occurred_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
                return PipelineAdvancementLease(
                    advancement_id=advancement_id,
                    source_work_unit_id=source_work_unit_id,
                    lease_id=lease_id,
                    lease_token=lease_token,
                    worker_id=command.worker_id,
                    dagster_execution_id=command.dagster_execution_id,
                    dagster_build_id=command.dagster_build_id,
                    transition_key=str(row["transition_key"]),
                    transition_plan_digest=str(row["transition_plan_digest"]),
                    revision=next_revision,
                    attempt_number=attempt_number,
                    issued_at_utc=now_utc,
                    expires_at_utc=expires_at_utc,
                )
        except PipelineAdvancementConflict:
            raise
        except SQLAlchemyError as exc:
            raise _storage_conflict(None, exc) from exc

    def apply(self, command: ApplyPipelineAdvancement) -> PipelineAdvancementStatus:
        now_utc = self._now_utc()
        try:
            with self._engine.begin() as connection:
                row = self._get_locked(connection, command.advancement_id)
                self._validate_lease(
                    row,
                    expected_revision=command.expected_revision,
                    lease_id=command.lease_id,
                    lease_token=command.lease_token,
                    dagster_execution_id=command.dagster_execution_id,
                    transition_plan_digest=command.transition_plan_digest,
                    now_utc=now_utc,
                )
                source = self._source_reader(connection, _uuid(row, "source_work_unit_id"))
                _assert_source_row_equal(row, source)
                plan = PipelineTransitionPlan(
                    source_work_unit_id=source.source_work_unit_id,
                    transition_key=str(row["transition_key"]),
                    disposition=PipelineTransitionDisposition.APPLY,
                    plan_digest=str(row["transition_plan_digest"]),
                    blocker=None,
                )
                applier = self._transition_appliers.get(plan.transition_key)
                if applier is None:
                    raise PipelineAdvancementConflict(
                        code="PIPELINE_APPLIER_UNAVAILABLE",
                        message="The registered pipeline transition has no runtime applier.",
                        context={
                            "advancementId": str(command.advancement_id),
                            "transitionKey": plan.transition_key,
                        },
                        required_action=(
                            "Install the exact transition applier before leasing this advancement."
                        ),
                    )
                actual_result_digest = applier(
                    connection,
                    source,
                    plan,
                    correlation_id=command.correlation_id,
                )
                if actual_result_digest != command.result_digest:
                    raise PipelineAdvancementConflict(
                        code="PIPELINE_RESULT_DIGEST_CONFLICT",
                        message=(
                            "The transition result digest differs from the requested completion."
                        ),
                        context={
                            "advancementId": str(command.advancement_id),
                            "requestedDigest": command.result_digest,
                            "actualDigest": actual_result_digest,
                        },
                        required_action=(
                            "Re-read the exact transition result and complete "
                            "with its canonical digest."
                        ),
                    )
                next_revision = command.expected_revision + 1
                self._update_terminal(
                    connection,
                    row,
                    state=PipelineAdvancementState.APPLIED,
                    next_revision=next_revision,
                    result_digest=actual_result_digest,
                    blocker=None,
                    now_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
                self._insert_event(
                    connection,
                    advancement_id=command.advancement_id,
                    attempt_number=int(row["attempt_count"]),
                    event_kind="applied",
                    lease_id=command.lease_id,
                    lease_token_digest=str(row["active_lease_token_digest"]),
                    worker_id=str(row["leased_by_worker_id"]),
                    dagster_execution_id=str(row["dagster_execution_id"]),
                    dagster_build_id=str(row["dagster_build_id"]),
                    transition_plan_digest=command.transition_plan_digest,
                    lease_expires_at_utc=_datetime(row, "lease_expires_at_utc"),
                    result_digest=actual_result_digest,
                    blocker=None,
                    occurred_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
                return _status(self._get_locked(connection, command.advancement_id))
        except PipelineAdvancementConflict:
            raise
        except SQLAlchemyError as exc:
            raise _storage_conflict(command.advancement_id, exc) from exc

    def block(self, command: BlockPipelineAdvancement) -> PipelineAdvancementStatus:
        now_utc = self._now_utc()
        try:
            with self._engine.begin() as connection:
                row = self._get_locked(connection, command.advancement_id)
                self._validate_lease(
                    row,
                    expected_revision=command.expected_revision,
                    lease_id=command.lease_id,
                    lease_token=command.lease_token,
                    dagster_execution_id=command.dagster_execution_id,
                    transition_plan_digest=command.transition_plan_digest,
                    now_utc=now_utc,
                )
                source = self._source_reader(connection, _uuid(row, "source_work_unit_id"))
                _assert_source_row_equal(row, source)
                next_revision = command.expected_revision + 1
                self._update_terminal(
                    connection,
                    row,
                    state=PipelineAdvancementState.BLOCKED,
                    next_revision=next_revision,
                    result_digest=None,
                    blocker=command.blocker,
                    now_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
                self._insert_event(
                    connection,
                    advancement_id=command.advancement_id,
                    attempt_number=int(row["attempt_count"]),
                    event_kind="blocked",
                    lease_id=command.lease_id,
                    lease_token_digest=str(row["active_lease_token_digest"]),
                    worker_id=str(row["leased_by_worker_id"]),
                    dagster_execution_id=str(row["dagster_execution_id"]),
                    dagster_build_id=str(row["dagster_build_id"]),
                    transition_plan_digest=command.transition_plan_digest,
                    lease_expires_at_utc=_datetime(row, "lease_expires_at_utc"),
                    result_digest=None,
                    blocker=command.blocker,
                    occurred_at_utc=now_utc,
                    correlation_id=command.correlation_id,
                )
                return _status(self._get_locked(connection, command.advancement_id))
        except PipelineAdvancementConflict:
            raise
        except SQLAlchemyError as exc:
            raise _storage_conflict(command.advancement_id, exc) from exc

    def _expire_leases(
        self,
        connection: Connection,
        now_utc: datetime,
        correlation_id: str,
    ) -> None:
        rows = (
            connection.execute(
                sa.select(pipeline_advancements)
                .where(
                    pipeline_advancements.c.state == PipelineAdvancementState.LEASED.value,
                    pipeline_advancements.c.lease_expires_at_utc <= now_utc,
                )
                .order_by(pipeline_advancements.c.lease_expires_at_utc)
                .with_for_update(skip_locked=True)
            )
            .mappings()
            .all()
        )
        for row in rows:
            advancement_id = _uuid(row, "advancement_id")
            lease_id = _uuid(row, "active_lease_id")
            self._insert_event(
                connection,
                advancement_id=advancement_id,
                attempt_number=int(row["attempt_count"]),
                event_kind="expired",
                lease_id=lease_id,
                lease_token_digest=str(row["active_lease_token_digest"]),
                worker_id=str(row["leased_by_worker_id"]),
                dagster_execution_id=str(row["dagster_execution_id"]),
                dagster_build_id=str(row["dagster_build_id"]),
                transition_plan_digest=str(row["transition_plan_digest"]),
                lease_expires_at_utc=_datetime(row, "lease_expires_at_utc"),
                result_digest=None,
                blocker=None,
                occurred_at_utc=now_utc,
                correlation_id=correlation_id,
            )
            connection.execute(
                sa.update(pipeline_advancements)
                .where(
                    pipeline_advancements.c.advancement_id == advancement_id,
                    pipeline_advancements.c.revision == int(row["revision"]),
                    pipeline_advancements.c.active_lease_id == lease_id,
                )
                .values(
                    state=PipelineAdvancementState.PENDING.value,
                    revision=int(row["revision"]) + 1,
                    active_lease_id=None,
                    active_lease_token_digest=None,
                    leased_by_worker_id=None,
                    dagster_execution_id=None,
                    dagster_build_id=None,
                    lease_issued_at_utc=None,
                    lease_expires_at_utc=None,
                    updated_at_utc=now_utc,
                    correlation_id=correlation_id,
                )
            )

    def _validate_lease(
        self,
        row: RowMapping,
        *,
        expected_revision: int,
        lease_id: UUID,
        lease_token: str,
        dagster_execution_id: str,
        transition_plan_digest: str,
        now_utc: datetime,
    ) -> None:
        advancement_id = _uuid(row, "advancement_id")
        if str(row["state"]) != PipelineAdvancementState.LEASED.value:
            raise PipelineAdvancementConflict(
                code="PIPELINE_LEASE_STALE",
                message="The pipeline advancement no longer has an active lease.",
                context={
                    "advancementId": str(advancement_id),
                    "state": str(row["state"]),
                },
                required_action="Discard the stale completion and claim pending work again.",
            )
        if int(row["revision"]) != expected_revision:
            raise PipelineAdvancementConflict(
                code="PIPELINE_REVISION_CONFLICT",
                message="The pipeline advancement changed after it was leased.",
                context={
                    "advancementId": str(advancement_id),
                    "expectedRevision": expected_revision,
                    "actualRevision": int(row["revision"]),
                },
                required_action="Discard the stale completion and reload canonical state.",
            )
        if _uuid(row, "active_lease_id") != lease_id:
            raise PipelineAdvancementConflict(
                code="PIPELINE_LEASE_STALE",
                message="The pipeline lease identity is stale.",
                context={"advancementId": str(advancement_id)},
                required_action="Discard the stale completion and use the current lease only.",
            )
        actual_token_digest = str(row["active_lease_token_digest"])
        if not secrets.compare_digest(actual_token_digest, _secret_digest(lease_token)):
            raise PipelineAdvancementConflict(
                code="PIPELINE_LEASE_TOKEN_INVALID",
                message="The pipeline lease token is invalid.",
                context={"advancementId": str(advancement_id)},
                required_action="Use only the token issued with the current lease.",
            )
        if str(row["dagster_execution_id"]) != dagster_execution_id:
            raise PipelineAdvancementConflict(
                code="PIPELINE_DAGSTER_EXECUTION_CONFLICT",
                message="The completion belongs to a different Dagster execution.",
                context={
                    "advancementId": str(advancement_id),
                    "expectedExecutionId": str(row["dagster_execution_id"]),
                    "actualExecutionId": dagster_execution_id,
                },
                required_action="Complete only from the execution that owns the current lease.",
            )
        if str(row["transition_plan_digest"]) != transition_plan_digest:
            raise PipelineAdvancementConflict(
                code="PIPELINE_PLAN_DIGEST_CONFLICT",
                message="The pipeline transition plan changed or was substituted.",
                context={
                    "advancementId": str(advancement_id),
                    "expectedDigest": str(row["transition_plan_digest"]),
                    "actualDigest": transition_plan_digest,
                },
                required_action="Reload and execute the exact registered transition plan.",
            )
        if _datetime(row, "lease_expires_at_utc") <= now_utc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_LEASE_EXPIRED",
                message="The pipeline advancement lease expired before completion.",
                context={"advancementId": str(advancement_id)},
                required_action="Discard the result and claim the advancement again.",
            )

    def _update_terminal(
        self,
        connection: Connection,
        row: RowMapping,
        *,
        state: PipelineAdvancementState,
        next_revision: int,
        result_digest: str | None,
        blocker: PipelineBlocker | None,
        now_utc: datetime,
        correlation_id: str,
    ) -> None:
        advancement_id = _uuid(row, "advancement_id")
        updated = connection.execute(
            sa.update(pipeline_advancements)
            .where(
                pipeline_advancements.c.advancement_id == advancement_id,
                pipeline_advancements.c.revision == int(row["revision"]),
                pipeline_advancements.c.active_lease_id == _uuid(row, "active_lease_id"),
            )
            .values(
                state=state.value,
                revision=next_revision,
                result_digest=result_digest,
                **_blocker_values(blocker),
                active_lease_id=None,
                active_lease_token_digest=None,
                leased_by_worker_id=None,
                dagster_execution_id=None,
                dagster_build_id=None,
                lease_issued_at_utc=None,
                lease_expires_at_utc=None,
                updated_at_utc=now_utc,
                correlation_id=correlation_id,
            )
        )
        if updated.rowcount != 1:
            raise PipelineAdvancementConflict(
                code="PIPELINE_REVISION_CONFLICT",
                message="The pipeline advancement changed during terminal completion.",
                context={"advancementId": str(advancement_id)},
                required_action="Discard the stale result and reload canonical state.",
            )

    def _insert_event(
        self,
        connection: Connection,
        *,
        advancement_id: UUID,
        attempt_number: int,
        event_kind: str,
        lease_id: UUID | None,
        lease_token_digest: str | None,
        worker_id: str | None,
        dagster_execution_id: str | None,
        dagster_build_id: str | None,
        transition_plan_digest: str,
        lease_expires_at_utc: datetime | None,
        result_digest: str | None,
        blocker: PipelineBlocker | None,
        occurred_at_utc: datetime,
        correlation_id: str,
    ) -> None:
        connection.execute(
            sa.insert(pipeline_advancement_attempts).values(
                event_id=self._uuid_factory(),
                advancement_id=advancement_id,
                attempt_number=attempt_number,
                event_kind=event_kind,
                lease_id=lease_id,
                lease_token_digest=lease_token_digest,
                worker_id=worker_id,
                dagster_execution_id=dagster_execution_id,
                dagster_build_id=dagster_build_id,
                transition_plan_digest=transition_plan_digest,
                lease_expires_at_utc=lease_expires_at_utc,
                result_digest=result_digest,
                **_blocker_values(blocker),
                occurred_at_utc=occurred_at_utc,
                correlation_id=correlation_id,
            )
        )

    @staticmethod
    def _find_by_source(
        connection: Connection,
        source_work_unit_id: UUID,
        *,
        for_update: bool,
    ) -> RowMapping | None:
        statement = sa.select(pipeline_advancements).where(
            pipeline_advancements.c.source_work_unit_id == source_work_unit_id
        )
        if for_update:
            statement = statement.with_for_update()
        return connection.execute(statement).mappings().one_or_none()

    @staticmethod
    def _get_locked(connection: Connection, advancement_id: UUID) -> RowMapping:
        row = (
            connection.execute(
                sa.select(pipeline_advancements)
                .where(pipeline_advancements.c.advancement_id == advancement_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_ADVANCEMENT_NOT_FOUND",
                message="The requested pipeline advancement does not exist.",
                context={"advancementId": str(advancement_id)},
                required_action="Refresh pipeline state and select an existing advancement.",
            )
        return row

    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Pipeline Advancement clock must return timezone-aware UTC")
        return value


def _source_values(source: SucceededWorkOutput) -> dict[str, object]:
    return {
        "source_work_unit_id": source.source_work_unit_id,
        "run_id": source.run_id,
        "stage_run_id": source.stage_run_id,
        "source_stage": source.stage.value,
        "source_capability": source.capability,
        "source_output_contract": source.output_contract,
        "source_output_digest": source.output_digest,
        "source_output_artifact_id": source.output_artifact.artifact_id,
        "source_output_artifact_role": source.output_artifact.role,
        "source_output_artifact_digest": source.output_artifact.content_digest,
        "source_output_artifact_size_bytes": source.output_artifact.size_bytes,
        "source_output_artifact_content_type": source.output_artifact.content_type,
        "source_input_artifacts": [
            _artifact_payload(item)
            for item in sorted(
                source.input_artifacts,
                key=lambda value: (value.role, str(value.artifact_id)),
            )
        ],
    }


def _source_identity(source: SucceededWorkOutput) -> tuple[object, ...]:
    values = _source_values(source)
    return tuple(values[key] for key in sorted(values))


def _row_source_identity(row: RowMapping) -> tuple[object, ...]:
    values: dict[str, object] = {
        "source_work_unit_id": _uuid(row, "source_work_unit_id"),
        "run_id": _uuid(row, "run_id"),
        "stage_run_id": _uuid(row, "stage_run_id"),
        "source_stage": str(row["source_stage"]),
        "source_capability": str(row["source_capability"]),
        "source_output_contract": str(row["source_output_contract"]),
        "source_output_digest": str(row["source_output_digest"]),
        "source_output_artifact_id": _uuid(row, "source_output_artifact_id"),
        "source_output_artifact_role": str(row["source_output_artifact_role"]),
        "source_output_artifact_digest": str(row["source_output_artifact_digest"]),
        "source_output_artifact_size_bytes": int(row["source_output_artifact_size_bytes"]),
        "source_output_artifact_content_type": str(row["source_output_artifact_content_type"]),
        "source_input_artifacts": _json_array(row["source_input_artifacts"]),
    }
    return tuple(values[key] for key in sorted(values))


def _assert_source_equal(
    requested: SucceededWorkOutput,
    canonical: SucceededWorkOutput,
) -> None:
    if _source_identity(requested) != _source_identity(canonical):
        raise PipelineAdvancementConflict(
            code="PIPELINE_SOURCE_OUTPUT_CONFLICT",
            message="The requested successful output differs from canonical work evidence.",
            context={"sourceWorkUnitId": str(requested.source_work_unit_id)},
            required_action="Reload the exact successful work output and recompute its transition.",
        )


def _assert_source_row_equal(row: RowMapping, source: SucceededWorkOutput) -> None:
    if _row_source_identity(row) != _source_identity(source):
        raise PipelineAdvancementConflict(
            code="PIPELINE_SOURCE_OUTPUT_CONFLICT",
            message="Canonical successful work evidence changed after registration.",
            context={"sourceWorkUnitId": str(source.source_work_unit_id)},
            required_action="Stop advancement and repair immutable source evidence ownership.",
        )


def _assert_registration_equal(
    row: RowMapping,
    source: SucceededWorkOutput,
    plan: PipelineTransitionPlan,
) -> None:
    _assert_source_row_equal(row, source)
    actual = (
        str(row["transition_key"]),
        str(row["transition_plan_digest"]),
    )
    expected = (plan.transition_key, plan.plan_digest)
    if actual != expected:
        raise PipelineAdvancementConflict(
            code="PIPELINE_REGISTRATION_CONFLICT",
            message="The successful work already has a different advancement plan.",
            context={
                "sourceWorkUnitId": str(source.source_work_unit_id),
                "registeredTransitionKey": actual[0],
                "requestedTransitionKey": expected[0],
            },
            required_action="Use the immutable registered plan or repair its owner explicitly.",
        )


def _status(row: RowMapping) -> PipelineAdvancementStatus:
    return PipelineAdvancementStatus(
        advancement_id=_uuid(row, "advancement_id"),
        source_work_unit_id=_uuid(row, "source_work_unit_id"),
        run_id=_uuid(row, "run_id"),
        state=PipelineAdvancementState(str(row["state"])),
        transition_key=str(row["transition_key"]),
        transition_plan_digest=str(row["transition_plan_digest"]),
        revision=int(row["revision"]),
        attempt_count=int(row["attempt_count"]),
        result_digest=(None if row["result_digest"] is None else str(row["result_digest"])),
        blocker=_blocker_from_row(row),
        created_at_utc=_datetime(row, "created_at_utc"),
        updated_at_utc=_datetime(row, "updated_at_utc"),
    )


def _blocker_from_row(row: RowMapping) -> PipelineBlocker | None:
    if row["blocker_code"] is None:
        return None
    return PipelineBlocker(
        owner=str(row["blocker_owner"]),
        code=str(row["blocker_code"]),
        message=str(row["blocker_message"]),
        required_action=str(row["blocker_required_action"]),
        context=_json_mapping(row["blocker_context"]),
    )


def _blocker_values(blocker: PipelineBlocker | None) -> dict[str, object]:
    if blocker is None:
        return {
            "blocker_owner": None,
            "blocker_code": None,
            "blocker_message": None,
            "blocker_required_action": None,
            "blocker_context": None,
        }
    return {
        "blocker_owner": blocker.owner,
        "blocker_code": blocker.code,
        "blocker_message": blocker.message,
        "blocker_required_action": blocker.required_action,
        "blocker_context": _json_value(blocker.context),
    }


def _artifact_payload(value: ArtifactIdentity) -> dict[str, object]:
    return {
        "artifactId": str(value.artifact_id),
        "role": value.role,
        "contentDigest": value.content_digest,
        "sizeBytes": value.size_bytes,
        "contentType": value.content_type,
    }


def _json_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported pipeline JSON value: {type(value).__name__}")


def _json_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("persisted blocker context is not an object")
    return {str(key): item for key, item in value.items()}


def _json_array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("persisted source input artifacts are not an array")
    return value


def _secret_digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _storage_conflict(
    identity: UUID | None,
    exc: SQLAlchemyError,
) -> PipelineAdvancementConflict:
    context: dict[str, object] = {"causeType": type(exc).__name__}
    if identity is not None:
        context["identity"] = str(identity)
    return PipelineAdvancementConflict(
        code="PIPELINE_STORAGE_FAILED",
        message="The Pipeline Advancement database operation did not complete.",
        context=context,
        required_action="Inspect PostgreSQL owner state and retry the exact operation.",
    )


def _uuid(row: RowMapping, key: str) -> UUID:
    value = row[key]
    if value is None:
        raise TypeError(f"persisted {key} is null")
    return value if isinstance(value, UUID) else UUID(str(value))


def _datetime(row: RowMapping, key: str) -> datetime:
    value = row[key]
    if not isinstance(value, datetime):
        raise TypeError(f"persisted {key} is not a datetime")
    return value
