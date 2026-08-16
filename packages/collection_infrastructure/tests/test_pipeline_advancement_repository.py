from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from collection_application.pipeline_advancement import (
    ApplyPipelineAdvancement,
    ArtifactIdentity,
    ClaimPipelineAdvancement,
    PipelineAdvancementState,
    PipelineTransitionRegistry,
    SucceededWorkOutput,
)
from collection_domain import WorkStage
from collection_infrastructure.postgres.pipeline_advancement import (
    PostgresPipelineAdvancementRepository,
)

_RUN_ID = UUID("00000000-0000-0000-0000-000000000301")
_STAGE_RUN_ID = UUID("00000000-0000-0000-0000-000000000302")
_WORK_ID = UUID("00000000-0000-0000-0000-000000000303")
_OUTPUT_ID = UUID("00000000-0000-0000-0000-000000000304")
_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000305")
_ADVANCEMENT_ID = UUID("00000000-0000-0000-0000-000000000306")
_LEASE_ID = UUID("00000000-0000-0000-0000-000000000307")
_EVENT_IDS = iter(
    (
        UUID("00000000-0000-0000-0000-000000000308"),
        UUID("00000000-0000-0000-0000-000000000309"),
    )
)
_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
_OUTPUT_DIGEST = "sha256:" + "a" * 64
_RESULT_DIGEST = "sha256:" + "b" * 64


def _artifact(artifact_id: UUID, role: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_id=artifact_id,
        role=role,
        content_digest=_OUTPUT_DIGEST,
        size_bytes=123,
        content_type="application/json",
    )


def _source() -> SucceededWorkOutput:
    return SucceededWorkOutput(
        source_work_unit_id=_WORK_ID,
        run_id=_RUN_ID,
        stage_run_id=_STAGE_RUN_ID,
        stage=WorkStage.DISCOVERY,
        capability="manual_import",
        output_contract="manual-import-plan@1",
        output_digest=_OUTPUT_DIGEST,
        output_artifact=_artifact(_OUTPUT_ID, "manual_import_plan"),
        input_artifacts=(_artifact(_SOURCE_ID, "manual_source:csv:accept_valid"),),
    )


class FakeResult:
    def __init__(
        self,
        *,
        one: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
        rowcount: int = 1,
    ) -> None:
        self._one = one
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return None if self._one is None else dict(self._one)

    def all(self) -> list[dict[str, object]]:
        return [dict(row) for row in self._rows]


class FakeConnection:
    def __init__(self) -> None:
        self.row: dict[str, object] | None = None
        self.events: list[dict[str, object]] = []

    def execute(self, statement: Any) -> FakeResult:
        if isinstance(statement, sa.sql.dml.Insert):
            values = _statement_values(statement)
            if statement.table.name == "pipeline_advancements":
                self.row = values
            else:
                self.events.append(values)
            return FakeResult(rowcount=1)
        if isinstance(statement, sa.sql.dml.Update):
            assert self.row is not None
            self.row.update(_statement_values(statement))
            return FakeResult(rowcount=1)
        assert isinstance(statement, sa.sql.selectable.Select)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "lease_expires_at_utc <=" in sql:
            return FakeResult(rows=[])
        if "source_work_unit_id =" in sql:
            return FakeResult(one=None if self.row is None else self.row)
        if " LIMIT " in sql and "state =" in sql:
            if self.row is not None and self.row["state"] == "pending":
                return FakeResult(one=self.row)
            return FakeResult(one=None)
        return FakeResult(one=self.row)


class FakeBegin(AbstractContextManager[FakeConnection]):
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> FakeConnection:
        return self._connection

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class FakeEngine:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def begin(self) -> FakeBegin:
        return FakeBegin(self.connection)


def _statement_values(statement: Any) -> dict[str, object]:
    compiled = statement.compile(dialect=postgresql.dialect())
    columns = {column.name for column in statement.table.columns}
    return {key: value for key, value in compiled.params.items() if key in columns}


def test_repository_register_claim_and_apply_share_one_durable_checkpoint() -> None:
    engine = FakeEngine()
    source = _source()
    plan = PipelineTransitionRegistry().plan(source)
    seen_connections: list[FakeConnection] = []

    def source_reader(connection, source_work_unit_id):
        assert source_work_unit_id == _WORK_ID
        seen_connections.append(connection)
        return source

    def applier(connection, actual_source, actual_plan, *, correlation_id):
        assert actual_source == source
        assert actual_plan.plan_digest == plan.plan_digest
        assert correlation_id == "pipeline-test"
        seen_connections.append(connection)
        return _RESULT_DIGEST

    identifiers = iter((_ADVANCEMENT_ID, _LEASE_ID, *_EVENT_IDS))
    repository = PostgresPipelineAdvancementRepository(
        engine,  # type: ignore[arg-type]
        source_reader=source_reader,
        transition_appliers={"manual-import-plan-admission": applier},
        clock=lambda: _NOW,
        uuid_factory=lambda: next(identifiers),
        lease_token_factory=lambda: "pipeline-lease-token",
    )

    registered = repository.register(source, plan, correlation_id="pipeline-test")
    lease = repository.claim(
        ClaimPipelineAdvancement(
            worker_id="pipeline-supervisor-1",
            dagster_execution_id="dagster-run-1",
            dagster_build_id="build-1",
            lease_duration=timedelta(minutes=5),
            correlation_id="pipeline-test",
        )
    )
    assert lease is not None
    applied = repository.apply(
        ApplyPipelineAdvancement(
            advancement_id=lease.advancement_id,
            expected_revision=lease.revision,
            lease_id=lease.lease_id,
            lease_token=lease.lease_token,
            dagster_execution_id=lease.dagster_execution_id,
            transition_plan_digest=lease.transition_plan_digest,
            result_digest=_RESULT_DIGEST,
            correlation_id="pipeline-test",
        )
    )

    assert registered.state is PipelineAdvancementState.PENDING
    assert lease.source_work_unit_id == _WORK_ID
    assert applied.state is PipelineAdvancementState.APPLIED
    assert applied.result_digest == _RESULT_DIGEST
    assert applied.revision == 2
    assert [event["event_kind"] for event in engine.connection.events] == ["leased", "applied"]
    assert seen_connections == [
        engine.connection,
        engine.connection,
        engine.connection,
    ]
