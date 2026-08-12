from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid5

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_CHILD_NAMESPACE = UUID("bd46bc6f-bd7b-5c60-b4d4-d4eb106e5417")


@dataclass(frozen=True, slots=True)
class ManualImportRecord:
    position: int
    locator_kind: str
    locator_value: str
    record_digest: str
    values: Mapping[str, str | None]

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("manual import record position cannot be negative")
        _require_token("locator_kind", self.locator_kind)
        if not self.locator_value or len(self.locator_value) > 500:
            raise ValueError("manual import record locator value is invalid")
        _require_digest("record_digest", self.record_digest)
        normalized = dict(sorted(self.values.items()))
        for key, value in normalized.items():
            _require_token("record field", key)
            if value is not None and len(value) > 100_000:
                raise ValueError("manual import record field exceeds the value limit")
        object.__setattr__(self, "values", normalized)


@dataclass(frozen=True, slots=True)
class ManualImportPlanForAdmission:
    plan_artifact_id: UUID
    source_artifact_id: UUID
    plan_digest: str
    source_digest: str
    mode: str
    status: str
    total_record_count: int
    accepted_record_count: int
    rejected_record_count: int
    records: tuple[ManualImportRecord, ...]

    def __post_init__(self) -> None:
        _require_digest("plan_digest", self.plan_digest)
        _require_digest("source_digest", self.source_digest)
        if self.mode not in {"atomic", "partial", "reject_all", "accept_valid"}:
            raise ValueError("manual import plan mode is unsupported")
        if self.status not in {"ready", "blocked"}:
            raise ValueError("manual import plan status is unsupported")
        counts = (
            self.total_record_count,
            self.accepted_record_count,
            self.rejected_record_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("manual import plan counts cannot be negative")
        if self.accepted_record_count + self.rejected_record_count != self.total_record_count:
            raise ValueError("manual import plan counts are inconsistent")
        if len(self.records) != self.accepted_record_count:
            raise ValueError("manual import accepted count does not match the record list")
        positions = tuple(record.position for record in self.records)
        if len(set(positions)) != len(positions):
            raise ValueError("manual import record positions must be unique")
        digests = tuple(record.record_digest for record in self.records)
        if len(set(digests)) != len(digests):
            raise ValueError("manual import record digests must be unique within one plan")
        if self.status == "blocked" and self.records:
            raise ValueError("blocked manual import plan cannot expose accepted records")


@dataclass(frozen=True, slots=True)
class AdmitManualImportPlan:
    admission_id: UUID
    parent_work_id: UUID
    run_id: UUID
    stage_name: str
    target_stage: str
    target_capability: str
    target_output_contract: str
    correlation_id: str
    plan: ManualImportPlanForAdmission

    def __post_init__(self) -> None:
        for name, value in (
            ("stage_name", self.stage_name),
            ("target_stage", self.target_stage),
            ("target_capability", self.target_capability),
            ("target_output_contract", self.target_output_contract),
            ("correlation_id", self.correlation_id),
        ):
            _require_token(name, value)


@dataclass(frozen=True, slots=True)
class ManualImportChildWork:
    work_id: UUID
    semantic_key: str
    input_digest: str
    input_payload: bytes
    record: ManualImportRecord


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


class ManualImportPlanBlocked(RuntimeError):
    def __init__(self, plan_digest: str) -> None:
        self.code = "MANUAL_IMPORT_PLAN_BLOCKED"
        self.context = {"planDigest": plan_digest}
        super().__init__("Blocked manual import plan cannot create child work units.")


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
        if command.plan.status == "blocked":
            raise ManualImportPlanBlocked(command.plan.plan_digest)
        children = tuple(_child_work(command, record) for record in command.plan.records)
        return self._store.admit(command, children)


def _child_work(
    command: AdmitManualImportPlan, record: ManualImportRecord
) -> ManualImportChildWork:
    semantic_key = (
        f"manual-import:{command.plan.plan_digest}:"
        f"{record.position}:{record.record_digest}"
    )
    work_id = uuid5(_CHILD_NAMESPACE, f"{command.run_id}:{semantic_key}")
    payload = _canonical_bytes(
        {
            "contract": "manual-import-record-input@1",
            "parentWorkId": str(command.parent_work_id),
            "planArtifactId": str(command.plan.plan_artifact_id),
            "planDigest": command.plan.plan_digest,
            "sourceArtifactId": str(command.plan.source_artifact_id),
            "sourceDigest": command.plan.source_digest,
            "position": record.position,
            "locator": {
                "kind": record.locator_kind,
                "value": record.locator_value,
            },
            "recordDigest": record.record_digest,
            "record": dict(record.values),
            "targetStage": command.target_stage,
            "targetCapability": command.target_capability,
            "targetOutputContract": command.target_output_contract,
        }
    )
    return ManualImportChildWork(
        work_id=work_id,
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
