from __future__ import annotations

from uuid import UUID

from sqlalchemy import create_engine

from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportChildWork,
    ManualImportPlanForAdmission,
    ManualImportRecord,
)
from collection_infrastructure.postgres.manual_import_child_writer import (
    PostgresManualImportChildWorkWriter,
)


def test_child_writer_resolves_the_canonical_work_engine_contract() -> None:
    engine = create_engine(
        "postgresql+psycopg://collection:collection@localhost:5432/collection"
    )
    writer = PostgresManualImportChildWorkWriter(engine)
    command = _command()
    child = _child()

    enqueue_command = writer._build_command(command, child)

    assert getattr(enqueue_command, "work_id") == child.work_id
    assert getattr(enqueue_command, "run_id") == command.run_id
    assert getattr(enqueue_command, "semantic_key") == child.semantic_key
    assert getattr(enqueue_command, "input_digest") == child.input_digest


def _command() -> AdmitManualImportPlan:
    record = _record()
    return AdmitManualImportPlan(
        admission_id=UUID("00000000-0000-0000-0000-000000000201"),
        parent_work_id=UUID("00000000-0000-0000-0000-000000000202"),
        run_id=UUID("00000000-0000-0000-0000-000000000203"),
        stage_name="manual_import_admission",
        target_stage="normalization",
        target_capability="normalization",
        target_output_contract="normalized-observation@1",
        correlation_id="manual-import-child-writer-test",
        plan=ManualImportPlanForAdmission(
            plan_artifact_id=UUID("00000000-0000-0000-0000-000000000204"),
            source_artifact_id=UUID("00000000-0000-0000-0000-000000000205"),
            plan_digest="sha256:" + "1" * 64,
            source_digest="sha256:" + "2" * 64,
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
        record_digest="sha256:" + "3" * 64,
        values={"name": "Studio"},
    )


def _child() -> ManualImportChildWork:
    record = _record()
    return ManualImportChildWork(
        work_id=UUID("00000000-0000-0000-0000-000000000206"),
        semantic_key="manual-import:plan:0:record",
        input_digest="sha256:" + "4" * 64,
        input_payload=b'{"contract":"manual-import-record-input@1"}',
        record=record,
    )
