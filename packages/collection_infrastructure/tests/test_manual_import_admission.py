from __future__ import annotations

import json
from contextlib import AbstractContextManager, suppress
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportAdmissionService,
    ManualImportChildWork,
    ManualImportPlanForAdmission,
    admission_result_digest,
)
from collection_contracts import ManualImportFormat, ManualImportMode
from collection_infrastructure.postgres.manual_import_admission import (
    ManualImportAdmissionConflict,
    PostgresManualImportAdmissionStore,
)
from manual_import_core import (
    build_manual_import_plan,
    canonical_manual_import_plan_json,
)
from sqlalchemy.dialects import postgresql

_ADMISSION_ID = UUID("00000000-0000-0000-0000-000000000401")
_PARENT_WORK_ID = UUID("00000000-0000-0000-0000-000000000402")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000403")
_PLAN_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000404")
_SOURCE_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000405")
_OTHER_DIGEST = "sha256:" + "9" * 64
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
        rows = self._rows
        if not rows and self._one is not _UNSET and self._one is not None:
            assert isinstance(self._one, dict)
            rows = (self._one,)
        return iter(dict(row) for row in rows)


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


class _CaptureStore:
    def __init__(self) -> None:
        self.children: tuple[ManualImportChildWork, ...] = ()

    def admit(self, command, children):
        del command
        self.children = tuple(children)
        raise _Captured


class _Captured(Exception):
    pass


def test_exact_replay_returns_persisted_canonical_child_identities() -> None:
    child = _child()
    engine = FakeEngine(
        (
            FakeResult(),
            FakeResult(one=_existing_row(child)),
            FakeResult(rows=(_existing_item(child),)),
        )
    )
    writer = RecordingChildWriter()
    store = PostgresManualImportAdmissionStore(engine, writer)  # type: ignore[arg-type]

    result = store.admit(_command(), (child,))

    assert result.status == "already_applied"
    assert result.child_work_ids == (child.work_id,)
    assert writer.calls == []
    assert engine.connection.executed_count == 3


def test_crossed_persisted_identities_fail_as_explicit_conflict() -> None:
    child = _child()
    first = _existing_row(child)
    second = dict(first)
    second["admission_id"] = UUID("00000000-0000-0000-0000-000000000499")
    engine = FakeEngine(
        (
            FakeResult(),
            FakeResult(rows=(first, second)),
        )
    )
    writer = RecordingChildWriter()
    store = PostgresManualImportAdmissionStore(engine, writer)  # type: ignore[arg-type]

    with pytest.raises(ManualImportAdmissionConflict) as error:
        store.admit(_command(), (child,))

    assert error.value.code == "MANUAL_IMPORT_ADMISSION_IDENTITY_CONFLICT"
    assert error.value.context["mismatches"] == [
        "admission_id",
        "parent_plan_identity",
    ]
    assert writer.calls == []


def test_parent_must_own_the_exact_succeeded_semantic_plan_digest() -> None:
    child = _child()
    engine = FakeEngine(
        (
            FakeResult(),
            FakeResult(one=None),
            FakeResult(
                one={
                    "run_id": _RUN_ID,
                    "capability": "manual_import",
                    "state": "succeeded",
                    "output_contract": "manual-import-plan@1",
                    "output_digest": _OTHER_DIGEST,
                }
            ),
        )
    )
    writer = RecordingChildWriter()
    store = PostgresManualImportAdmissionStore(engine, writer)  # type: ignore[arg-type]

    with pytest.raises(ManualImportAdmissionConflict) as error:
        store.admit(_command(), (child,))

    assert error.value.code == "MANUAL_IMPORT_PARENT_OUTPUT_MISMATCH"
    assert writer.calls == []


@pytest.mark.parametrize(
    ("plan_digest", "source_digest", "source_size", "expected_mismatch"),
    (
        (_OTHER_DIGEST, None, None, "plan_artifact_digest"),
        (None, _OTHER_DIGEST, None, "source_artifact_digest"),
        (None, None, 999, "source_artifact_size"),
    ),
)
def test_artifact_identity_separates_semantic_plan_and_artifact_digests(
    plan_digest: str | None,
    source_digest: str | None,
    source_size: int | None,
    expected_mismatch: str,
) -> None:
    child = _child()
    engine = FakeEngine(
        (
            FakeResult(),
            FakeResult(one=None),
            FakeResult(one=_parent_row()),
            FakeResult(
                rows=_artifact_rows(
                    plan_digest=plan_digest,
                    source_digest=source_digest,
                    source_size=source_size,
                )
            ),
        )
    )
    writer = RecordingChildWriter()
    store = PostgresManualImportAdmissionStore(engine, writer)  # type: ignore[arg-type]

    with pytest.raises(ManualImportAdmissionConflict) as error:
        store.admit(_command(), (child,))

    assert error.value.code == "MANUAL_IMPORT_ARTIFACT_IDENTITY_MISMATCH"
    assert error.value.context["mismatches"] == [expected_mismatch]
    artifact_sql = str(engine.connection.statements[3][0].compile(dialect=postgresql.dialect()))
    assert "JOIN sources.artifact_objects" in artifact_sql
    assert writer.calls == []


def test_verified_exact_provenance_enqueues_children_in_admission_transaction() -> None:
    child = _child()
    engine = FakeEngine(
        (
            FakeResult(),
            FakeResult(one=None),
            FakeResult(one=_parent_row()),
            FakeResult(rows=_artifact_rows()),
            FakeResult(scalar=True),
            FakeResult(scalar=True),
            FakeResult(),
            FakeResult(),
        )
    )
    writer = RecordingChildWriter()
    store = PostgresManualImportAdmissionStore(engine, writer)  # type: ignore[arg-type]

    result = store.admit(_command(), (child,))

    assert result.status == "applied"
    assert result.child_work_ids == (child.work_id,)
    assert writer.calls == [(engine.connection, (child,))]
    inserted = engine.connection.statements[6][0]
    compiled = str(inserted.compile(dialect=postgresql.dialect()))
    assert "manual_import.plan_admissions" in compiled


def _command() -> AdmitManualImportPlan:
    plan = _plan()
    payload = canonical_manual_import_plan_json(plan).encode("utf-8")
    return AdmitManualImportPlan(
        admission_id=_ADMISSION_ID,
        parent_work_id=_PARENT_WORK_ID,
        run_id=_RUN_ID,
        correlation_id="manual-import-admission-store-test",
        plan=ManualImportPlanForAdmission(
            plan_artifact_id=_PLAN_ARTIFACT_ID,
            plan_artifact_digest=_digest(payload),
            source_artifact_id=_SOURCE_ARTIFACT_ID,
            source_artifact_role="manual_import_source:json:atomic",
            plan=plan,
        ),
    )


def _child() -> ManualImportChildWork:
    capture = _CaptureStore()
    with suppress(_Captured):
        ManualImportAdmissionService(capture).admit(_command())
    assert len(capture.children) == 1
    return capture.children[0]


def _existing_row(child: ManualImportChildWork) -> dict[str, object]:
    command = _command()
    plan = command.plan.plan
    return {
        "admission_id": command.admission_id,
        "parent_work_id": command.parent_work_id,
        "run_id": command.run_id,
        "plan_artifact_id": command.plan.plan_artifact_id,
        "source_artifact_id": command.plan.source_artifact_id,
        "source_artifact_role": command.plan.source_artifact_role,
        "plan_digest": plan.plan_digest,
        "source_digest": plan.source_digest,
        "mode": plan.mode.value,
        "plan_disposition": plan.disposition.value,
        "target_stage": command.target_stage,
        "target_capability": command.target_capability,
        "target_output_contract": command.target_output_contract,
        "valid_record_count": plan.valid_record_count,
        "issue_count": plan.issue_count,
        "child_work_count": 1,
        "result_digest": admission_result_digest(
            command.admission_id,
            plan.plan_digest,
            (child.work_id,),
        ),
    }


def _existing_item(child: ManualImportChildWork) -> dict[str, object]:
    return {
        "position": child.position,
        "child_work_id": child.work_id,
        "locator_kind": child.record.locator.kind,
        "locator_value": child.record.locator.pointer,
        "record_digest": child.record.record_digest,
    }


def _parent_row() -> dict[str, object]:
    return {
        "run_id": _RUN_ID,
        "capability": "manual_import",
        "state": "succeeded",
        "output_contract": "manual-import-plan@1",
        "output_digest": _command().plan.plan_digest,
    }


def _artifact_rows(
    *,
    plan_digest: str | None = None,
    source_digest: str | None = None,
    source_size: int | None = None,
) -> tuple[dict[str, object], ...]:
    command = _command()
    return (
        {
            "artifact_id": _PLAN_ARTIFACT_ID,
            "content_digest": plan_digest or command.plan.plan_artifact_digest,
            "size_bytes": len(canonical_manual_import_plan_json(command.plan.plan).encode()),
        },
        {
            "artifact_id": _SOURCE_ARTIFACT_ID,
            "content_digest": source_digest or command.plan.source_digest,
            "size_bytes": (
                command.plan.plan.source_size_bytes if source_size is None else source_size
            ),
        },
    )


def _plan():
    source = _source()
    return build_manual_import_plan(
        source,
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )


def _source() -> bytes:
    return json.dumps(
        [
            {
                "expected_entity_kind": "place",
                "display_name": "Studio A",
                "website": "https://studio.example",
                "osm_id": None,
                "reference_urls": [],
                "note": None,
                "provenance": "manual import test",
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return f"sha256:{sha256(value).hexdigest()}"
