from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid5

from collection_contracts import (
    ManualImportDisposition,
    ManualImportMode,
    ManualImportPlan,
    ManualImportRecord,
)
from collection_domain import WorkCapability, WorkStage
from manual_import_core import (
    schedulable_manual_import_records,
    verify_manual_import_plan,
)

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_SOURCE_ROLE = re.compile(
    r"^(?:manual_source|manual_import_source):"
    r"(?P<format>csv|json|jsonl):(?P<mode>atomic|partial)$"
)
_CHILD_NAMESPACE = UUID("bd46bc6f-bd7b-5c60-b4d4-d4eb106e5417")
# An admission identity is derived from the exact plan it admits, so the pipeline transition
# that admits a plan can name its result before writing it and a replay resolves to the same
# immutable admission instead of allocating a second one.
_ADMISSION_NAMESPACE = UUID("8f57cf3a-0d3f-5c2d-9a4b-6c5e71d0b2a9")

MANUAL_IMPORT_ADMISSION_STAGE_NAME = "manual_import_admission"
MANUAL_RECORD_STAGE = WorkStage.DISCOVERY
MANUAL_RECORD_CAPABILITY = WorkCapability.MANUAL_RECORD
MANUAL_RECORD_OUTPUT_CONTRACT = "manual-import-record@1"


@dataclass(frozen=True, slots=True)
class ManualImportPlanForAdmission:
    """Binds the canonical plan to its exact immutable source and plan artifacts."""

    plan_artifact_id: UUID
    plan_artifact_digest: str
    source_artifact_id: UUID
    source_artifact_role: str
    plan: ManualImportPlan

    def __post_init__(self) -> None:
        _require_digest("plan_artifact_digest", self.plan_artifact_digest)
        verify_manual_import_plan(self.plan)
        match = _SOURCE_ROLE.fullmatch(self.source_artifact_role)
        if match is None:
            raise ValueError("manual import source artifact role is not canonical")
        if match.group("format") != self.plan.format.value:
            raise ValueError("manual import source role format differs from the canonical plan")
        if match.group("mode") != self.plan.mode.value:
            raise ValueError("manual import source role mode differs from the canonical plan")

    @property
    def plan_digest(self) -> str:
        return self.plan.plan_digest

    @property
    def source_digest(self) -> str:
        return self.plan.source_digest

    @property
    def mode(self) -> ManualImportMode:
        return self.plan.mode

    @property
    def disposition(self) -> ManualImportDisposition:
        return self.plan.disposition

    @property
    def valid_record_count(self) -> int:
        return self.plan.valid_record_count

    @property
    def issue_count(self) -> int:
        return self.plan.issue_count

    @property
    def records(self) -> tuple[ManualImportRecord, ...]:
        return self.plan.records


@dataclass(frozen=True, slots=True)
class AdmitManualImportPlan:
    admission_id: UUID
    parent_work_id: UUID
    run_id: UUID
    correlation_id: str
    plan: ManualImportPlanForAdmission

    def __post_init__(self) -> None:
        _require_token("correlation_id", self.correlation_id)

    @property
    def stage_name(self) -> str:
        return MANUAL_IMPORT_ADMISSION_STAGE_NAME

    @property
    def target_stage(self) -> str:
        return MANUAL_RECORD_STAGE.value

    @property
    def target_capability(self) -> str:
        return MANUAL_RECORD_CAPABILITY.value

    @property
    def target_output_contract(self) -> str:
        return MANUAL_RECORD_OUTPUT_CONTRACT


@dataclass(frozen=True, slots=True)
class ManualImportChildWork:
    work_id: UUID
    position: int
    semantic_key: str
    input_digest: str
    input_payload: bytes
    record: ManualImportRecord

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("manual import child position cannot be negative")
        _require_digest("semantic_key", self.semantic_key)
        _require_digest("input_digest", self.input_digest)
        if not self.input_payload:
            raise ValueError("manual import child input payload cannot be empty")


@dataclass(frozen=True, slots=True)
class ManualImportAdmissionResult:
    admission_id: UUID
    plan_digest: str
    child_work_ids: tuple[UUID, ...]
    status: str
    result_digest: str

    def __post_init__(self) -> None:
        _require_digest("plan_digest", self.plan_digest)
        _require_digest("result_digest", self.result_digest)
        if self.status not in {"applied", "already_applied"}:
            raise ValueError("manual import admission result status is unsupported")


@dataclass(frozen=True, slots=True)
class ManualImportAdmissionSummary:
    """The immutable identity an admission of one plan resolves to.

    The summary is derived from the exact plan artifacts before anything is written, so the
    transition plan can state the digest the admission will produce. It is not a second copy
    of the admission state: the store still owns the persisted result and refuses any replay
    that would bind these identities to different input.
    """

    admission_id: UUID
    document_plan_digest: str
    child_work_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        _require_digest("document_plan_digest", self.document_plan_digest)
        if len(self.child_work_ids) != len(set(self.child_work_ids)):
            raise ValueError("manual import child work identities must be unique")

    @property
    def result_digest(self) -> str:
        return admission_result_digest(
            self.admission_id,
            self.document_plan_digest,
            self.child_work_ids,
        )


class ManualImportPlanRejected(RuntimeError):
    def __init__(self, plan: ManualImportPlan) -> None:
        self.code = "MANUAL_IMPORT_PLAN_REJECTED"
        self.context = {
            "planDigest": plan.plan_digest,
            "disposition": plan.disposition.value,
            "validRecordCount": plan.valid_record_count,
            "issueCount": plan.issue_count,
        }
        super().__init__("Rejected manual import plan cannot create child work units.")


class ManualImportAdmissionStore(Protocol):
    def admit(
        self,
        command: AdmitManualImportPlan,
        children: Sequence[ManualImportChildWork],
    ) -> ManualImportAdmissionResult: ...


class ManualImportAdmissionService:
    def __init__(self, store: ManualImportAdmissionStore) -> None:
        self._store = store

    def admit(self, command: AdmitManualImportPlan) -> ManualImportAdmissionResult:
        return self._store.admit(command, manual_import_admission_children(command))


def manual_import_admission_id(*, parent_work_id: UUID, plan_artifact_id: UUID) -> UUID:
    """The one admission identity the exact parent plan can ever produce."""

    return uuid5(_ADMISSION_NAMESPACE, f"{parent_work_id}:{plan_artifact_id}")


def manual_import_admission_children(
    command: AdmitManualImportPlan,
) -> tuple[ManualImportChildWork, ...]:
    """The child work an admission would create, derived from the plan alone."""

    return manual_import_child_work_for_plan(
        parent_work_id=command.parent_work_id,
        run_id=command.run_id,
        plan=command.plan,
    )


def manual_import_child_work_for_plan(
    *,
    parent_work_id: UUID,
    run_id: UUID,
    plan: ManualImportPlanForAdmission,
) -> tuple[ManualImportChildWork, ...]:
    """Derive the admission's child work from the immutable plan bindings alone.

    Child identities depend on the exact plan, its artifacts, the run and the declared target
    stage, never on who requests the admission or in which order. The transition planner uses
    this to state the admission result before it is written, and the applier reuses the same
    derivation, so both can only ever name one admission per plan.
    """

    canonical = plan.plan
    verify_manual_import_plan(canonical)
    records = schedulable_manual_import_records(canonical)
    if canonical.disposition is ManualImportDisposition.REJECTED or not records:
        raise ManualImportPlanRejected(canonical)
    return tuple(
        _child_work(
            parent_work_id=parent_work_id,
            run_id=run_id,
            plan=plan,
            position=position,
            record=record,
        )
        for position, record in enumerate(records)
    )


def manual_import_admission_summary(
    *,
    admission_id: UUID,
    parent_work_id: UUID,
    run_id: UUID,
    plan: ManualImportPlanForAdmission,
) -> ManualImportAdmissionSummary:
    """The admission identity and result the exact plan will resolve to."""

    children = manual_import_child_work_for_plan(
        parent_work_id=parent_work_id,
        run_id=run_id,
        plan=plan,
    )
    return ManualImportAdmissionSummary(
        admission_id=admission_id,
        document_plan_digest=plan.plan_digest,
        child_work_ids=tuple(child.work_id for child in children),
    )


def _child_work(
    *,
    parent_work_id: UUID,
    run_id: UUID,
    plan: ManualImportPlanForAdmission,
    position: int,
    record: ManualImportRecord,
) -> ManualImportChildWork:
    target_stage = MANUAL_RECORD_STAGE.value
    target_capability = MANUAL_RECORD_CAPABILITY.value
    target_output_contract = MANUAL_RECORD_OUTPUT_CONTRACT
    semantic_material = _canonical_bytes(
        {
            "contract": "manual-import-record-work-identity",
            "contractRevision": "manual-import-record-work-identity-v1",
            "planArtifactDigest": plan.plan_artifact_digest,
            "planDigest": plan.plan_digest,
            "sourceDigest": plan.source_digest,
            "sourceArtifactRole": plan.source_artifact_role,
            "position": position,
            "recordDigest": record.record_digest,
            "targetStage": target_stage,
            "targetCapability": target_capability,
            "targetOutputContract": target_output_contract,
        }
    )
    semantic_key = f"sha256:{sha256(semantic_material).hexdigest()}"
    work_id = uuid5(_CHILD_NAMESPACE, f"{run_id}:{semantic_key}")
    payload = _canonical_bytes(
        {
            "contract": "manual-import-record-input",
            "contractRevision": "manual-import-record-input-v1",
            "parentWorkId": str(parent_work_id),
            "planArtifactId": str(plan.plan_artifact_id),
            "planArtifactDigest": plan.plan_artifact_digest,
            "planDigest": plan.plan_digest,
            "sourceArtifactId": str(plan.source_artifact_id),
            "sourceArtifactRole": plan.source_artifact_role,
            "sourceDigest": plan.source_digest,
            "position": position,
            "locator": record.locator.model_dump(mode="json", by_alias=True),
            "recordDigest": record.record_digest,
            "record": record.record.model_dump(mode="json", by_alias=True),
            "targetStage": target_stage,
            "targetCapability": target_capability,
            "targetOutputContract": target_output_contract,
        }
    )
    return ManualImportChildWork(
        work_id=work_id,
        position=position,
        semantic_key=semantic_key,
        input_digest=f"sha256:{sha256(payload).hexdigest()}",
        input_payload=payload,
        record=record,
    )


def admission_result_digest(
    admission_id: UUID, plan_digest: str, child_work_ids: Sequence[UUID]
) -> str:
    payload = _canonical_bytes(
        {
            "admissionId": str(admission_id),
            "planDigest": plan_digest,
            "childWorkIds": [str(value) for value in child_work_ids],
        }
    )
    return f"sha256:{sha256(payload).hexdigest()}"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_digest(name: str, value: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 identity")


def _require_token(name: str, value: str) -> None:
    if _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
