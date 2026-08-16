from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportChildWork,
    ManualImportPlanForAdmission,
    ManualImportRecord,
    admission_result_digest,
)
from collection_infrastructure.postgres.manual_import_admission import (
    ManualImportAdmissionConflict,
    PostgresManualImportAdmissionStore,
)

_PLAN_DIGEST = "sha256:" + "1" * 64
_SOURCE_DIGEST = "sha256:" + "2" * 64
_RECORD_DIGEST = "sha256:" + "3" * 64
_INPUT_DIGEST = "sha256:" + "4" * 64
_SEMANTIC_KEY = "sha256:" + "5" * 64
_OTHER_DIGEST = "sha256:" + "9" * 64
_ADMISSION_ID = UUID("00000000-0000-0000-0000-000000000401")
_PARENT_WORK_ID = UUID("00000000-0000-0000-0000-000000000402")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000403")
_PLAN_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000404")
_SOURCE_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000405")
_CHILD_WORK_ID = UUID("00000000-0000-0000-0000-000000000406")
_UNSET = object()


class FakeResult:
    def __init__(
        self,
        *,
        one: object = _UNSET,
        rows: tuple[dict[str, object], ...] = (),
        scalar: object = _UNSET,
    ) -> None:
        self._one = one
        self._rows = rows
        self._scalar = scalar

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        if self._one is _UNSET:
            raise AssertionError("one_or_none was not expected")
        if self._one is None:
            return None
        assert isinstance(self._one, dict)
        return dict(self._one)

    def scalar_one_or_none(self) -> object | None:
        if self._scalar is _UNSET:
            raise AssertionError("scalar_one_or_none was not expected")
        return self._scalar

    def __iter__(self):
        return iter(dict(row) for row in self._rows)


class FakeConnection:
    def __init__(self, responses: tuple[FakeResult, ...]) -> None:
        self._responses = iter(responses)
        self.statements: list[tuple[Any, object | None]] = []
        self.executed_count = 0

    def execute(self, statement: Any, parameters: object | None = None) -> FakeResult:
        self.statements.append((statement, parameters))
        self.executed_count += 1
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise AssertionError("unexpected SQL statement") from exc


class FakeBegin(AbstractContextManager[FakeConnection]):
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> FakeConnection:
        return self._connection

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class FakeEngine:
    def __init__(self, responses: tuple[FakeResult, ...]) -> None:
        self.connection = FakeConnection(responses)

    def begin(self) -> FakeBegin:
        return FakeBegin(self.connection)


class RecordingChildWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[FakeConnection, tuple[ManualImportChildWork, ...]]] = []

    def enqueue(
        self,
        connection: FakeConnection,
        command: AdmitManualImportPlan,
        children: tuple[ManualImportChildWork, ...],
    ) -> tuple[UUID, ...]:
        assert command == _command()
        self.calls.append((connection, children))
        return tuple(child.work_id for child in children)


def _command() -> AdmitManualImportPlan:
    record = _record()
    return AdmitManualImportPlan(
        admission_id=_ADMISSION_ID,
        parent_work_id=_PARENT_WORK_ID,
        run_id=_RUN_ID,
        stage_name="manual_import_admission",
        target_stage="normalization",
        target_capability="normalization",
        target_output_contract="normalized-observation@1",
        correlation_id="manual-import-admission-store-test",
        plan=ManualImportPlanForAdmission(
            plan_artifact_id=_PLAN_ARTIFACT_ID,
            source_artifact_id=_SOURCE_ARTIFACT_ID,
            plan_digest=_PLAN_DIGEST,
            source_digest=_SOURCE_DIGEST,
            mode="partial",
            status="ready",
            total_record_count=1,
            accepted_record_count=1,
            rejected_record_count=0,
            records=(record,),
        ),
    )


def _record() -> ManualImportRecord:
    return ManualImportRecord(
        position=0,
        locator_kind="line",
        locator_value="1",
        record_digest=_RECORD_DIGEST,
        values={"name": "Studio"},
    )


def _child() -> ManualImportChildWork:
    return ManualImportChildWork(
        work_id=_CHILD_WORK_ID,
        semantic_key=_SEMANTIC_KEY,
        input_digest=_INPUT_DIGEST,
        input_payload=b'{"contract":"manual-import-record-input@1"}',
        record=_record(),
    )


def _existing_row(*, result_digest: str | None = None) -> dict[str, object]:
    command = _command()
    child = _child()
    return {
        "admission_id": command.admission_id,
        "parent_work_id": command.parent_work_id,
        "run_id": command.run_id,
        "plan_artifact_id": command.plan.plan_artifact_id,
        "source_artifact_id": command.plan.source_artifact_id,
        "plan_digest": command.plan.plan_digest,
        "source_digest": command.plan.source_digest,
        "mode": command.plan.mode,
        "plan_status": command.plan.status,
        "target_stage": command.target_stage,
        "target_capability": command.target_capability,
        "target_output_contract": command.target_output_contract,
        "total_record_count": command.plan.total_record_count,
        "accepted_record_count": command.plan.accepted_record_count,
        "rejected_record_count": command.plan.rejected_record_count,
        "child_work_count": 1,
        "result_digest": result_digest
        or admission_result_digest(
            command.admission_id,
            command.plan.plan_digest,
            (child.work_id,),
        ),
    }


def _existing_item() -> dict[str, object]:
    child = _child()
    return {
        "position": child.record.position,
        "child_work_id": child.work_id,
        "locator_kind": child.record.locator_kind,
        "locator_value": child.record.locator_value,
        "record_digest": child.record.record_digest,
    }


def _artifact_rows(
    *,
    plan_digest: str = _PLAN_DIGEST,
    source_digest: str = _SOURCE_DIGEST,
) -> tuple[dict[str, object], ...]:
    return (
        {"artifact_id": _PLAN_ARTIFACT_ID, "content_digest": plan_digest},
        {"artifact_id": _SOURCE_ARTIFACT_ID, "content_digest": source_digest},
    )


def test_exact_replay_returns_the_persisted_child_identities() -> None:
    engine = FakeEngine(
        (
            FakeResult(),
            FakeResult(one=_existing_row()),
            FakeResult(rows=(_existing_item(),)),
        )
    )
    writer = RecordingChildWriter()
    store = PostgresManualImportAdmissionStore(engine, writer)  # type: ignore[arg-type]

    result = store.admit(_command(), (_child(),))

    assert result.status == "already_applied"
    assert result.child_work_ids == (_CHILD_WORK_ID,)
    assert result.result_digest == _existing_row()["result_digest"]
    assert writer.calls == []
    assert engine.connection.executed_count == 3


def test_exact_replay_rejects_a_corrupted_result_digest() -> None:
    engine = FakeEngine(
        (
            FakeResult(),
            FakeResult(one=_existing_row(result_digest=_OTHER_DIGEST)),
            FakeResult(rows=(_existing_item(),)),
        )
    )
    writer = RecordingChildWriter()
    store = PostgresManualImportAdmissionStore(engine, writer)  # type: ignore[arg-type]

    with pytest.raises(ManualImportAdmissionConflict) as error:
        store.admit(_command(), (_child(),))

    assert error.value.code == "MANUAL_IMPORT_ADMISSION_IDENTITY_CONFLICT"
    assert error.value.context["mismatches"] == ["result_digest"]
    assert writer.calls == []
    assert engine.connection.executed_count == 3


def test_parent_must_be_owned_by_manual_import() -> None:
    engine = FakeEngine(
        (
            FakeResult(),
            FakeResult(one=None),
            FakeResult(one={"run_id": _RUN_ID, "capability": "normalization"}),
        )
    )
    writer = RecordingChildWriter()
    store = PostgresManualImportAdmissionStore(engine, writer)  # type: ignore[arg-type]

    with pytest.raises(ManualImportAdmissionConflict) as error:
        store.admit(_command(), (_child(),))

    assert error.value.code == "MANUAL_IMPORT_PARENT_CAPABILITY_MISMATCH"
    assert writer.calls == []
    assert engine.connection.executed_count == 3


@pytest.mark.parametrize(
    ("plan_digest", "source_digest", "expected_mismatch"),
    (
        (_OTHER_DIGEST, _SOURCE_DIGEST, "plan_artifact_digest"),
        (_PLAN_DIGEST, _OTHER_DIGEST, "source_artifact_digest"),
    ),
)
def test_artifact_object_digests_must_match_the_admitted_plan(
    plan_digest: str,
    source_digest: str,
    expected_mismatch: str,
) -> None:
    engine = FakeEngine(
        (
            FakeResult(),
            FakeResult(one=None),
            FakeResult(one={"run_id": _RUN_ID, "capability": "manual_import"}),
            FakeResult(
                rows=_artifact_rows(
                    plan_digest=plan_digest,
                    source_digest=source_digest,
                )
            ),
        )
    )
    writer = RecordingChildWriter()
    store = PostgresManualImportAdmissionStore(engine, writer)  # type: ignore[arg-type]

    with pytest.raises(ManualImportAdmissionConflict) as error:
        store.admit(_command(), (_child(),))

    assert error.value.code == "MANUAL_IMPORT_ARTIFACT_DIGEST_MISMATCH"
    assert error.value.context["mismatches"] == [expected_mismatch]
    artifact_sql = str(engine.connection.statements[3][0].compile(dialect=postgresql.dialect()))
    assert "JOIN sources.artifact_objects" in artifact_sql
    assert writer.calls == []
    assert engine.connection.executed_count == 4


def test_verified_provenance_enqueues_children_in_the_admission_transaction() -> None:
    engine = FakeEngine(
        (
            FakeResult(),
            FakeResult(one=None),
            FakeResult(one={"run_id": _RUN_ID, "capability": "manual_import"}),
            FakeResult(rows=_artifact_rows()),
            FakeResult(scalar=True),
            FakeResult(scalar=True),
            FakeResult(),
            FakeResult(),
        )
    )
    writer = RecordingChildWriter()
    store = PostgresManualImportAdmissionStore(engine, writer)  # type: ignore[arg-type]

    result = store.admit(_command(), (_child(),))

    assert result.status == "applied"
    assert result.child_work_ids == (_CHILD_WORK_ID,)
    assert writer.calls == [(engine.connection, (_child(),))]
    assert engine.connection.executed_count == 8
