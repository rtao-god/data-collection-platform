from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportAdmissionService,
    ManualImportChildWork,
    ManualImportPlanForAdmission,
)
from collection_contracts import ManualImportFormat, ManualImportMode
from collection_domain import WorkCapability, WorkStage
from collection_infrastructure.postgres.manual_import_child_writer import (
    PostgresManualImportChildWorkWriter,
)
from manual_import_core import (
    build_manual_import_plan,
    canonical_manual_import_plan_json,
)
from sqlalchemy import create_engine

_STAGE_RUN_ID = UUID("00000000-0000-0000-0000-000000000207")


class _ScalarResult:
    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[UUID]:
        return [_STAGE_RUN_ID]


class _Connection:
    def execute(self, statement):
        del statement
        return _ScalarResult()


class _RecordingWorkEngine:
    def __init__(self) -> None:
        self.specs = []

    def enqueue_work_in_transaction(self, connection, spec) -> None:
        del connection
        self.specs.append(spec)


class _ChildStore:
    def __init__(self) -> None:
        self.children: tuple[ManualImportChildWork, ...] = ()

    def admit(self, command, children):
        del command
        self.children = tuple(children)
        raise _ChildrenCaptured


class _ChildrenCaptured(Exception):
    pass


def test_child_writer_uses_fixed_discovery_owner_and_selected_plan_role() -> None:
    engine = create_engine("postgresql+psycopg://collection:collection@localhost:5432/collection")
    writer = PostgresManualImportChildWorkWriter(
        engine,
        clock=lambda: datetime(2026, 8, 17, tzinfo=UTC),
    )
    recording = _RecordingWorkEngine()
    writer._work_engine = recording  # type: ignore[assignment]
    command = _command()
    child = _child(command)

    result = writer.enqueue(_Connection(), command, (child,))  # type: ignore[arg-type]

    assert result == (child.work_id,)
    assert len(recording.specs) == 1
    spec = recording.specs[0]
    assert spec.stage is WorkStage.DISCOVERY
    assert spec.capability is WorkCapability.MANUAL_RECORD
    assert spec.source_key is None
    assert spec.expected_output_contract == "manual-import-record@1"
    assert tuple(binding.role for binding in spec.input_artifacts) == (
        "manual_import_source:json:atomic",
        "manual_import_plan_record:0",
    )


def _command() -> AdmitManualImportPlan:
    plan = _plan()
    payload = canonical_manual_import_plan_json(plan).encode("utf-8")
    return AdmitManualImportPlan(
        admission_id=UUID("00000000-0000-0000-0000-000000000201"),
        parent_work_id=UUID("00000000-0000-0000-0000-000000000202"),
        run_id=UUID("00000000-0000-0000-0000-000000000203"),
        correlation_id="manual-import-child-writer-test",
        plan=ManualImportPlanForAdmission(
            plan_artifact_id=UUID("00000000-0000-0000-0000-000000000204"),
            plan_artifact_digest=f"sha256:{sha256(payload).hexdigest()}",
            source_artifact_id=UUID("00000000-0000-0000-0000-000000000205"),
            source_artifact_role="manual_import_source:json:atomic",
            plan=plan,
        ),
    )


def _child(command: AdmitManualImportPlan) -> ManualImportChildWork:
    store = _ChildStore()
    with suppress(_ChildrenCaptured):
        ManualImportAdmissionService(store).admit(command)
    assert len(store.children) == 1
    return store.children[0]


def _plan():
    source = json.dumps(
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
    return build_manual_import_plan(
        source,
        format=ManualImportFormat.JSON,
        mode=ManualImportMode.ATOMIC,
    )
