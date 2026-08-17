from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportAdmissionResult,
    ManualImportChildWork,
    admission_result_digest,
)
from collection_infrastructure.postgres import artifact_metadata
from collection_infrastructure.postgres.manual_import_metadata import (
    plan_admission_items,
    plan_admissions,
)
from collection_infrastructure.postgres.work_metadata import work_units


class ManualImportChildWorkWriter(Protocol):
    def enqueue(
        self,
        connection: Connection,
        command: AdmitManualImportPlan,
        children: Sequence[ManualImportChildWork],
    ) -> tuple[UUID, ...]: ...


class ManualImportAdmissionConflict(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: Mapping[str, object],
        required_action: str,
    ) -> None:
        self.code = code
        self.context = dict(context)
        self.required_action = required_action
        super().__init__(message)


class PostgresManualImportAdmissionStore:
    """Persists canonical plan admission and child work in one transaction."""

    def __init__(self, engine: Engine, child_writer: ManualImportChildWorkWriter) -> None:
        self._engine = engine
        self._child_writer = child_writer

    def admit(
        self,
        command: AdmitManualImportPlan,
        children: Sequence[ManualImportChildWork],
    ) -> ManualImportAdmissionResult:
        try:
            with self._engine.begin() as connection:
                self._lock_identity(connection, command)
                existing = self._load_existing(connection, command)
                if existing is not None:
                    child_ids = self._require_existing_identity(
                        connection,
                        existing,
                        command,
                        children,
                    )
                    return _result(
                        existing,
                        child_work_ids=child_ids,
                        status="already_applied",
                    )
                self._require_parent_and_artifacts(connection, command)
                child_ids = self._child_writer.enqueue(connection, command, children)
                if child_ids != tuple(child.work_id for child in children):
                    raise _conflict(
                        code="MANUAL_IMPORT_CHILD_IDENTITY_CONFLICT",
                        message="The Work Engine returned a different child identity.",
                        command=command,
                        required_action=("Inspect the Work Engine identity and retry admission."),
                    )
                result_digest = admission_result_digest(
                    command.admission_id,
                    command.plan.plan_digest,
                    child_ids,
                )
                admitted_at_utc = datetime.now(UTC)
                plan = command.plan.plan
                connection.execute(
                    sa.insert(plan_admissions).values(
                        admission_id=command.admission_id,
                        parent_work_id=command.parent_work_id,
                        run_id=command.run_id,
                        plan_artifact_id=command.plan.plan_artifact_id,
                        source_artifact_id=command.plan.source_artifact_id,
                        source_artifact_role=command.plan.source_artifact_role,
                        plan_digest=plan.plan_digest,
                        source_digest=plan.source_digest,
                        mode=plan.mode.value,
                        plan_disposition=plan.disposition.value,
                        target_stage=command.target_stage,
                        target_capability=command.target_capability,
                        target_output_contract=command.target_output_contract,
                        valid_record_count=plan.valid_record_count,
                        issue_count=plan.issue_count,
                        child_work_count=len(child_ids),
                        result_digest=result_digest,
                        admitted_at_utc=admitted_at_utc,
                        correlation_id=command.correlation_id,
                        revision=0,
                    )
                )
                if children:
                    connection.execute(
                        sa.insert(plan_admission_items),
                        [
                            {
                                "admission_id": command.admission_id,
                                "position": child.position,
                                "child_work_id": child.work_id,
                                "locator_kind": child.record.locator.kind,
                                "locator_value": child.record.locator.pointer,
                                "record_digest": child.record.record_digest,
                            }
                            for child in children
                        ],
                    )
                return ManualImportAdmissionResult(
                    admission_id=command.admission_id,
                    plan_digest=plan.plan_digest,
                    child_work_ids=child_ids,
                    status="applied",
                    result_digest=result_digest,
                )
        except ManualImportAdmissionConflict:
            raise
        except SQLAlchemyError as exc:
            raise _conflict(
                code="MANUAL_IMPORT_ADMISSION_STORAGE_FAILED",
                message="The manual import admission transaction did not complete.",
                command=command,
                required_action=("Inspect admission, artifact, and work rows before retrying."),
                cause_type=type(exc).__name__,
            ) from exc

    @staticmethod
    def _lock_identity(connection: Connection, command: AdmitManualImportPlan) -> None:
        identities = (
            f"manual-import-admission-id:{command.admission_id}",
            (
                "manual-import-admission-source:"
                f"{command.parent_work_id}:{command.plan.plan_artifact_id}"
            ),
        )
        connection.execute(
            sa.text(
                """
                WITH identities(value) AS MATERIALIZED (
                    SELECT value
                    FROM unnest(CAST(:identities AS text[])) AS identity(value)
                    ORDER BY value
                )
                SELECT pg_advisory_xact_lock(hashtextextended(value, 0))
                FROM identities
                """
            ),
            {"identities": identities},
        )

    @staticmethod
    def _load_existing(connection: Connection, command: AdmitManualImportPlan) -> RowMapping | None:
        rows = tuple(
            connection.execute(
                sa.select(plan_admissions)
                .where(
                    sa.or_(
                        plan_admissions.c.admission_id == command.admission_id,
                        sa.and_(
                            plan_admissions.c.parent_work_id == command.parent_work_id,
                            plan_admissions.c.plan_artifact_id == command.plan.plan_artifact_id,
                        ),
                    )
                )
                .with_for_update()
            ).mappings()
        )
        if len(rows) > 1:
            raise _conflict(
                code="MANUAL_IMPORT_ADMISSION_IDENTITY_CONFLICT",
                message="Admission and source identities resolve to different persisted rows.",
                command=command,
                required_action=(
                    "Inspect both immutable admissions and allocate an owner-approved identity."
                ),
                mismatches=("admission_id", "parent_plan_identity"),
            )
        return rows[0] if rows else None

    @staticmethod
    def _require_existing_identity(
        connection: Connection,
        row: RowMapping,
        command: AdmitManualImportPlan,
        children: Sequence[ManualImportChildWork],
    ) -> tuple[UUID, ...]:
        plan = command.plan.plan
        expected = {
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
            "child_work_count": len(children),
        }
        mismatches = [key for key, value in expected.items() if row[key] != value]
        items = tuple(
            connection.execute(
                sa.select(plan_admission_items)
                .where(plan_admission_items.c.admission_id == row["admission_id"])
                .order_by(plan_admission_items.c.position)
            ).mappings()
        )
        expected_items = tuple(
            (
                child.position,
                child.work_id,
                child.record.locator.kind,
                child.record.locator.pointer,
                child.record.record_digest,
            )
            for child in sorted(children, key=lambda value: value.position)
        )
        actual_items = tuple(
            (
                int(item["position"]),
                UUID(str(item["child_work_id"])),
                str(item["locator_kind"]),
                str(item["locator_value"]),
                str(item["record_digest"]),
            )
            for item in items
        )
        if actual_items != expected_items:
            mismatches.append("child_items")
        expected_child_ids = tuple(child.work_id for child in children)
        expected_result_digest = admission_result_digest(
            command.admission_id,
            plan.plan_digest,
            expected_child_ids,
        )
        if str(row["result_digest"]) != expected_result_digest:
            mismatches.append("result_digest")
        if mismatches:
            raise _conflict(
                code="MANUAL_IMPORT_ADMISSION_IDENTITY_CONFLICT",
                message="The admission identity is bound to different immutable input.",
                command=command,
                required_action=("Use the existing admission or allocate a new identity."),
                mismatches=sorted(mismatches),
            )
        return expected_child_ids

    @staticmethod
    def _require_parent_and_artifacts(
        connection: Connection, command: AdmitManualImportPlan
    ) -> None:
        parent = (
            connection.execute(
                sa.select(
                    work_units.c.run_id,
                    work_units.c.capability,
                    work_units.c.state,
                    work_units.c.output_contract,
                    work_units.c.output_digest,
                ).where(work_units.c.work_id == command.parent_work_id)
            )
            .mappings()
            .one_or_none()
        )
        if parent is None:
            raise _conflict(
                code="MANUAL_IMPORT_PARENT_WORK_NOT_FOUND",
                message="The parent manual import work unit does not exist.",
                command=command,
                required_action=("Use the work unit that produced the verified plan artifact."),
            )
        if parent["run_id"] != command.run_id:
            raise _conflict(
                code="MANUAL_IMPORT_PARENT_RUN_MISMATCH",
                message="The parent work unit belongs to a different collection run.",
                command=command,
                required_action="Use the run identity owned by the parent work unit.",
            )
        if parent["capability"] != "manual_import":
            raise _conflict(
                code="MANUAL_IMPORT_PARENT_CAPABILITY_MISMATCH",
                message="The parent work unit is not owned by manual import.",
                command=command,
                required_action=(
                    "Use the manual-import work unit that produced the plan artifact."
                ),
            )
        if (
            parent["state"] != "succeeded"
            or parent["output_contract"] != "manual-import-plan@1"
            or parent["output_digest"] != command.plan.plan_digest
        ):
            raise _conflict(
                code="MANUAL_IMPORT_PARENT_OUTPUT_MISMATCH",
                message="The parent completion does not own the canonical plan.",
                command=command,
                required_action=("Use the exact succeeded plan work and semantic output digest."),
            )

        raw_artifacts = artifact_metadata.artifact_records
        artifact_objects = artifact_metadata.artifact_objects
        artifact_ids = {
            command.plan.plan_artifact_id,
            command.plan.source_artifact_id,
        }
        artifact_rows = tuple(
            connection.execute(
                sa.select(
                    raw_artifacts.c.artifact_id,
                    artifact_objects.c.content_digest,
                    artifact_objects.c.size_bytes,
                )
                .select_from(
                    raw_artifacts.join(
                        artifact_objects,
                        raw_artifacts.c.object_id == artifact_objects.c.object_id,
                    )
                )
                .where(raw_artifacts.c.artifact_id.in_(artifact_ids))
            ).mappings()
        )
        artifacts = {
            UUID(str(row["artifact_id"])): (
                str(row["content_digest"]),
                int(row["size_bytes"]),
            )
            for row in artifact_rows
        }
        if set(artifacts) != artifact_ids:
            raise _conflict(
                code="MANUAL_IMPORT_ARTIFACT_NOT_FOUND",
                message="The plan or source artifact is not verified in metadata.",
                command=command,
                required_action=("Verify both exact artifacts before admitting the plan."),
            )
        plan_digest, _ = artifacts[command.plan.plan_artifact_id]
        source_digest, source_size = artifacts[command.plan.source_artifact_id]
        digest_mismatches: list[str] = []
        if plan_digest != command.plan.plan_artifact_digest:
            digest_mismatches.append("plan_artifact_digest")
        if source_digest != command.plan.source_digest:
            digest_mismatches.append("source_artifact_digest")
        if source_size != command.plan.plan.source_size_bytes:
            digest_mismatches.append("source_artifact_size")
        if digest_mismatches:
            raise _conflict(
                code="MANUAL_IMPORT_ARTIFACT_IDENTITY_MISMATCH",
                message="An artifact identity differs from the admitted plan.",
                command=command,
                required_action=("Use the exact verified plan and source artifact identities."),
                mismatches=digest_mismatches,
            )
        _require_artifact_binding(connection, command, input_binding=True)
        _require_artifact_binding(connection, command, input_binding=False)


def _require_artifact_binding(
    connection: Connection,
    command: AdmitManualImportPlan,
    *,
    input_binding: bool,
) -> None:
    name = "work_input_artifacts" if input_binding else "work_output_artifacts"
    table = getattr(artifact_metadata, name, None)
    if not isinstance(table, sa.Table):
        raise _conflict(
            code="MANUAL_IMPORT_ARTIFACT_BINDING_UNAVAILABLE",
            message="The artifact lineage table is unavailable.",
            command=command,
            required_action=("Apply the artifact lineage migration before admitting plans."),
        )
    artifact_id = (
        command.plan.source_artifact_id if input_binding else command.plan.plan_artifact_id
    )
    expected_role = command.plan.source_artifact_role if input_binding else "manual_import_plan"
    exists = connection.execute(
        sa.select(sa.literal(True)).where(
            sa.exists(
                sa.select(1).where(
                    table.c.work_id == command.parent_work_id,
                    table.c.artifact_id == artifact_id,
                    table.c.role == expected_role,
                )
            )
        )
    ).scalar_one_or_none()
    if exists is not True:
        raise _conflict(
            code="MANUAL_IMPORT_ARTIFACT_LINEAGE_MISMATCH",
            message="The source or plan artifact is not bound to the parent work unit.",
            command=command,
            required_action=("Use the exact source input and plan output of the parent work unit."),
        )


def _result(
    row: RowMapping,
    *,
    child_work_ids: tuple[UUID, ...],
    status: str,
) -> ManualImportAdmissionResult:
    return ManualImportAdmissionResult(
        admission_id=UUID(str(row["admission_id"])),
        plan_digest=str(row["plan_digest"]),
        child_work_ids=child_work_ids,
        status=status,
        result_digest=str(row["result_digest"]),
    )


def _conflict(
    *,
    code: str,
    message: str,
    command: AdmitManualImportPlan,
    required_action: str,
    cause_type: str | None = None,
    mismatches: Sequence[str] = (),
) -> ManualImportAdmissionConflict:
    context: dict[str, object] = {
        "admissionId": str(command.admission_id),
        "parentWorkId": str(command.parent_work_id),
        "planDigest": command.plan.plan_digest,
    }
    if cause_type is not None:
        context["causeType"] = cause_type
    if mismatches:
        context["mismatches"] = list(mismatches)
    return ManualImportAdmissionConflict(
        code=code,
        message=message,
        context=context,
        required_action=required_action,
    )
