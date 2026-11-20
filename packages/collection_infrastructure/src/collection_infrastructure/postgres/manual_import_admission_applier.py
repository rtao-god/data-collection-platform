"""Pipeline transition that admits a verified manual-import plan.

The transition is a transport only: the plan is decoded by `manual_import_plan_document`, the
admission identity comes from the admission owner, the admission itself is written by
`ManualImportAdmissionService` over `PostgresManualImportAdmissionStore`, and the child work
units are enqueued by that store's child writer. Nothing here re-derives the plan, allocates a
second identity, or admits an unverified document.

The applier runs on the connection the advancement repository already holds, so the admission,
its child work units and the advancement result commit or roll back as one unit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy.engine import Connection

from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportAdmissionResult,
    ManualImportAdmissionService,
    ManualImportChildWork,
    ManualImportPlanRejected,
    manual_import_admission_summary,
)
from collection_application.manual_import_plan_document import manual_import_admission_command
from collection_application.pipeline_advancement import (
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    SucceededWorkOutput,
)
from collection_infrastructure.postgres.manual_import_admission import (
    ManualImportAdmissionConflict,
    PostgresManualImportAdmissionStore,
)

_TRANSITION_KEY = "manual-import-plan-admission"
_PLAN_ARTIFACT_ROLE = "manual_import_plan"


class _ConnectionBoundAdmissionStore:
    """Adapts the owner store to the transaction the advancement already owns."""

    def __init__(
        self,
        store: PostgresManualImportAdmissionStore,
        connection: Connection,
    ) -> None:
        self._store = store
        self._connection = connection

    def admit(
        self,
        command: AdmitManualImportPlan,
        children: Sequence[ManualImportChildWork],
    ) -> ManualImportAdmissionResult:
        return self._store.admit_on_connection(self._connection, command, children)


class PostgresManualImportAdmissionApplier:
    """Admits the exact plan of one succeeded manual-import work unit."""

    def __init__(self, store: PostgresManualImportAdmissionStore) -> None:
        self._store = store

    def __call__(
        self,
        connection: Connection,
        source: SucceededWorkOutput,
        plan: PipelineTransitionPlan,
        *,
        correlation_id: str,
    ) -> str:
        if not self._valid_plan(source, plan):
            raise PipelineAdvancementConflict(
                code="PIPELINE_MANUAL_IMPORT_ADMISSION_PLAN_INVALID",
                message="The manual-import admission applier received an unrelated plan.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "transitionKey": plan.transition_key,
                    "disposition": plan.disposition.value,
                },
                required_action=(
                    "Recompute the admission from the exact succeeded manual-import plan."
                ),
            )
        registered = plan.manual_import_admission
        command = self._command(source, correlation_id=correlation_id)
        derived = manual_import_admission_summary(
            admission_id=command.admission_id,
            parent_work_id=command.parent_work_id,
            run_id=command.run_id,
            plan=command.plan,
        )
        if derived != registered:
            raise PipelineAdvancementConflict(
                code="PIPELINE_MANUAL_IMPORT_ADMISSION_IDENTITY_CHANGED",
                message="The registered admission identity no longer matches the exact plan.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "registeredAdmissionId": str(registered.admission_id),
                    "derivedAdmissionId": str(derived.admission_id),
                    "registeredPlanDigest": registered.document_plan_digest,
                    "derivedPlanDigest": derived.document_plan_digest,
                },
                required_action=(
                    "Review the changed plan artifacts and register the canonical plan again."
                ),
            )
        try:
            result = ManualImportAdmissionService(
                _ConnectionBoundAdmissionStore(self._store, connection)
            ).admit(command)
        except ManualImportPlanRejected as exc:
            raise PipelineAdvancementConflict(
                code=exc.code,
                message=str(exc),
                context=dict(exc.context),
                required_action=(
                    "Repair the authored manual source and re-run the manual import work."
                ),
            ) from exc
        except ManualImportAdmissionConflict as exc:
            raise PipelineAdvancementConflict(
                code=exc.code,
                message=str(exc),
                context=dict(exc.context),
                required_action=exc.required_action,
            ) from exc
        if (
            result.admission_id != registered.admission_id
            or result.child_work_ids != registered.child_work_ids
            or result.result_digest != registered.result_digest
        ):
            raise PipelineAdvancementConflict(
                code="PIPELINE_MANUAL_IMPORT_ADMISSION_RESULT_CONFLICT",
                message="The persisted admission does not match the registered admission.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "admissionId": str(result.admission_id),
                    "status": result.status,
                    "childWorkCount": str(len(result.child_work_ids)),
                },
                required_action=(
                    "Inspect the persisted admission and child work before retrying the "
                    "transition."
                ),
            )
        return result.result_digest

    @staticmethod
    def _valid_plan(source: SucceededWorkOutput, plan: PipelineTransitionPlan) -> bool:
        return (
            plan.transition_key == _TRANSITION_KEY
            and plan.disposition is PipelineTransitionDisposition.APPLY
            and plan.blocker is None
            and plan.manual_import_admission is not None
            and plan.source_work_unit_id == source.source_work_unit_id
            and source.capability == "manual_import"
            and source.output_contract == "manual-import-plan@1"
            and source.output_artifact.role == _PLAN_ARTIFACT_ROLE
        )

    @staticmethod
    def _command(source: SucceededWorkOutput, *, correlation_id: str) -> AdmitManualImportPlan:
        if source.output_content is None:
            raise PipelineAdvancementConflict(
                code="MANUAL_IMPORT_PLAN_ARTIFACT_UNREADABLE",
                message="The succeeded manual-import plan bytes are not available.",
                context={"sourceWorkUnitId": str(source.source_work_unit_id)},
                required_action="Restore the exact plan artifact before admitting the plan.",
            )
        parents = tuple(
            artifact
            for artifact in source.input_artifacts
            if artifact.role.startswith(("manual_source:", "manual_import_source:"))
        )
        if len(parents) != 1:
            raise PipelineAdvancementConflict(
                code="PIPELINE_MANUAL_IMPORT_SOURCE_BINDING_INVALID",
                message="The admission needs exactly one canonical manual source artifact.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "matchingArtifactIds": [str(item.artifact_id) for item in parents],
                },
                required_action="Bind exactly one canonical manual source artifact.",
            )
        parent = parents[0]
        try:
            return manual_import_admission_command(
                source.output_content,
                parent_work_id=source.source_work_unit_id,
                run_id=source.run_id,
                correlation_id=correlation_id,
                plan_artifact_id=source.output_artifact.artifact_id,
                plan_artifact_digest=source.output_artifact.content_digest,
                plan_digest=source.output_digest,
                source_artifact_id=parent.artifact_id,
                source_artifact_role=parent.role,
                source_digest=parent.content_digest,
            )
        except (ManualImportPlanRejected, ValueError) as exc:
            raise PipelineAdvancementConflict(
                code=getattr(exc, "code", "MANUAL_IMPORT_PLAN_CONTRACT_INVALID"),
                message=str(exc),
                context=dict(getattr(exc, "context", {})),
                required_action=(
                    "Re-run the exact manual import work so its plan matches the canonical "
                    "plan contract and its source artifacts."
                ),
            ) from exc


def manual_import_admission_transition_appliers(
    store: PostgresManualImportAdmissionStore,
) -> Mapping[str, PostgresManualImportAdmissionApplier]:
    """Registers the one transition key this applier owns."""

    return {_TRANSITION_KEY: PostgresManualImportAdmissionApplier(store)}
