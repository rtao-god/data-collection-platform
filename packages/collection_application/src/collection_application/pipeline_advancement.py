from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, TypeVar
from uuid import UUID

from collection_contracts import owner_error
from collection_domain import WorkStage

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,199}$")
_MANUAL_SOURCE_ROLES = (
    re.compile(r"^manual_source:(csv|json|jsonl):(reject_all|accept_valid)$"),
    re.compile(r"^manual_import_source:(csv|json|jsonl):(reject_all|accept_valid)$"),
)
_ResultT = TypeVar("_ResultT")


class PipelineAdvancementState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    APPLIED = "applied"
    BLOCKED = "blocked"


class PipelineTransitionDisposition(StrEnum):
    APPLY = "apply"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    artifact_id: UUID
    role: str
    content_digest: str
    size_bytes: int
    content_type: str

    def __post_init__(self) -> None:
        _require_token("artifact role", self.role)
        _require_sha256("artifact content digest", self.content_digest)
        if self.size_bytes < 0:
            raise ValueError("artifact size cannot be negative")
        _require_plain_text("artifact content type", self.content_type, maximum=200)


@dataclass(frozen=True, slots=True)
class SucceededWorkOutput:
    source_work_unit_id: UUID
    run_id: UUID
    stage_run_id: UUID
    stage: WorkStage
    capability: str
    output_contract: str
    output_digest: str
    output_artifact: ArtifactIdentity
    input_artifacts: tuple[ArtifactIdentity, ...]

    def __post_init__(self) -> None:
        _require_token("work capability", self.capability)
        _require_token("work output contract", self.output_contract)
        _require_sha256("work output digest", self.output_digest)
        artifact_ids = tuple(item.artifact_id for item in self.input_artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("work input artifacts contain duplicate artifact identities")
        roles = tuple(item.role for item in self.input_artifacts)
        if len(roles) != len(set(roles)):
            raise ValueError("work input artifacts contain duplicate roles")


@dataclass(frozen=True, slots=True)
class PipelineBlocker:
    owner: str
    code: str
    message: str
    required_action: str
    context: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_token("pipeline blocker owner", self.owner)
        if _CODE_PATTERN.fullmatch(self.code) is None:
            raise ValueError("pipeline blocker code must use canonical upper snake case")
        _require_plain_text("pipeline blocker message", self.message, maximum=1_000)
        _require_plain_text(
            "pipeline blocker required action",
            self.required_action,
            maximum=1_000,
        )
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))


@dataclass(frozen=True, slots=True)
class PipelineTransitionPlan:
    source_work_unit_id: UUID
    transition_key: str
    disposition: PipelineTransitionDisposition
    plan_digest: str
    blocker: PipelineBlocker | None

    def __post_init__(self) -> None:
        _require_token("pipeline transition key", self.transition_key)
        _require_sha256("pipeline transition plan digest", self.plan_digest)
        if self.disposition is PipelineTransitionDisposition.APPLY and self.blocker is not None:
            raise ValueError("applicable pipeline transition cannot contain a blocker")
        if self.disposition is PipelineTransitionDisposition.BLOCK and self.blocker is None:
            raise ValueError("blocked pipeline transition requires a blocker")


@dataclass(frozen=True, slots=True)
class PipelineAdvancementLease:
    advancement_id: UUID
    source_work_unit_id: UUID
    lease_id: UUID
    lease_token: str
    worker_id: str
    dagster_execution_id: str
    dagster_build_id: str
    transition_key: str
    transition_plan_digest: str
    revision: int
    attempt_number: int
    issued_at_utc: datetime
    expires_at_utc: datetime

    def __post_init__(self) -> None:
        _require_token("pipeline lease token", self.lease_token)
        _require_token("pipeline worker id", self.worker_id)
        _require_token("Dagster execution id", self.dagster_execution_id)
        _require_token("Dagster build id", self.dagster_build_id)
        _require_token("pipeline transition key", self.transition_key)
        _require_sha256("pipeline transition plan digest", self.transition_plan_digest)
        if self.revision < 0:
            raise ValueError("pipeline advancement revision cannot be negative")
        if self.attempt_number < 1:
            raise ValueError("pipeline advancement attempt number must be positive")
        _require_aware_utc("pipeline lease issued_at_utc", self.issued_at_utc)
        _require_aware_utc("pipeline lease expires_at_utc", self.expires_at_utc)
        if self.expires_at_utc <= self.issued_at_utc:
            raise ValueError("pipeline lease expiry must follow issuance")


@dataclass(frozen=True, slots=True)
class PipelineAdvancementStatus:
    advancement_id: UUID
    source_work_unit_id: UUID
    run_id: UUID
    state: PipelineAdvancementState
    transition_key: str
    transition_plan_digest: str
    revision: int
    attempt_count: int
    result_digest: str | None
    blocker: PipelineBlocker | None
    created_at_utc: datetime
    updated_at_utc: datetime

    def __post_init__(self) -> None:
        _require_token("pipeline transition key", self.transition_key)
        _require_sha256("pipeline transition plan digest", self.transition_plan_digest)
        if self.revision < 0:
            raise ValueError("pipeline advancement revision cannot be negative")
        if self.attempt_count < 0:
            raise ValueError("pipeline advancement attempt count cannot be negative")
        if self.result_digest is not None:
            _require_sha256("pipeline advancement result digest", self.result_digest)
        _require_aware_utc("pipeline advancement created_at_utc", self.created_at_utc)
        _require_aware_utc("pipeline advancement updated_at_utc", self.updated_at_utc)
        if self.updated_at_utc < self.created_at_utc:
            raise ValueError("pipeline advancement update cannot precede creation")
        if self.state is PipelineAdvancementState.APPLIED:
            if self.result_digest is None or self.blocker is not None:
                raise ValueError("applied pipeline advancement requires only a result digest")
        elif self.state is PipelineAdvancementState.BLOCKED:
            if self.blocker is None or self.result_digest is not None:
                raise ValueError("blocked pipeline advancement requires only a blocker")
        elif self.result_digest is not None or self.blocker is not None:
            raise ValueError("non-terminal pipeline advancement cannot contain a terminal result")


@dataclass(frozen=True, slots=True)
class ClaimPipelineAdvancement:
    worker_id: str
    dagster_execution_id: str
    dagster_build_id: str
    lease_duration: timedelta
    correlation_id: str

    def __post_init__(self) -> None:
        _require_token("pipeline worker id", self.worker_id)
        _require_token("Dagster execution id", self.dagster_execution_id)
        _require_token("Dagster build id", self.dagster_build_id)
        _require_token("correlation_id", self.correlation_id)
        if not timedelta(seconds=30) <= self.lease_duration <= timedelta(minutes=30):
            raise ValueError("pipeline lease duration must be between 30 seconds and 30 minutes")


@dataclass(frozen=True, slots=True)
class ApplyPipelineAdvancement:
    advancement_id: UUID
    expected_revision: int
    lease_id: UUID
    lease_token: str
    dagster_execution_id: str
    transition_plan_digest: str
    result_digest: str
    correlation_id: str

    def __post_init__(self) -> None:
        if self.expected_revision < 0:
            raise ValueError("expected pipeline advancement revision cannot be negative")
        _require_token("pipeline lease token", self.lease_token)
        _require_token("Dagster execution id", self.dagster_execution_id)
        _require_sha256("pipeline transition plan digest", self.transition_plan_digest)
        _require_sha256("pipeline advancement result digest", self.result_digest)
        _require_token("correlation_id", self.correlation_id)


@dataclass(frozen=True, slots=True)
class BlockPipelineAdvancement:
    advancement_id: UUID
    expected_revision: int
    lease_id: UUID
    lease_token: str
    dagster_execution_id: str
    transition_plan_digest: str
    blocker: PipelineBlocker
    correlation_id: str

    def __post_init__(self) -> None:
        if self.expected_revision < 0:
            raise ValueError("expected pipeline advancement revision cannot be negative")
        _require_token("pipeline lease token", self.lease_token)
        _require_token("Dagster execution id", self.dagster_execution_id)
        _require_sha256("pipeline transition plan digest", self.transition_plan_digest)
        _require_token("correlation_id", self.correlation_id)


class PipelineAdvancementConflict(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: Mapping[str, object],
        required_action: str,
    ) -> None:
        if _CODE_PATTERN.fullmatch(code) is None:
            raise ValueError("pipeline conflict code must use canonical upper snake case")
        self.code = code
        self.message = message
        self.context = dict(context)
        self.required_action = required_action
        super().__init__(message)


class PipelineAdvancementPort(Protocol):
    def register(
        self,
        source: SucceededWorkOutput,
        plan: PipelineTransitionPlan,
        *,
        correlation_id: str,
    ) -> PipelineAdvancementStatus: ...

    def claim(self, command: ClaimPipelineAdvancement) -> PipelineAdvancementLease | None: ...

    def apply(self, command: ApplyPipelineAdvancement) -> PipelineAdvancementStatus: ...

    def block(self, command: BlockPipelineAdvancement) -> PipelineAdvancementStatus: ...


class PipelineTransitionRegistry:
    def plan(self, source: SucceededWorkOutput) -> PipelineTransitionPlan:
        if (
            source.stage is WorkStage.DISCOVERY
            and source.capability == "manual_import"
            and source.output_contract == "manual-import-plan@1"
        ):
            return self._manual_import_plan(source)
        if (
            source.stage is WorkStage.DISCOVERY
            and source.capability == "manual_record"
            and source.output_contract == "manual-import-record@1"
        ):
            return self._manual_record(source)
        return self._blocked(
            source,
            transition_key="unsupported-transition",
            code="PIPELINE_TRANSITION_UNSUPPORTED",
            message="No pipeline transition owns the successful work output contract.",
            required_action=(
                "Implement and register an exact owner transition before this output may advance."
            ),
            context={
                "stage": source.stage.value,
                "capability": source.capability,
                "outputContract": source.output_contract,
                "outputRole": source.output_artifact.role,
            },
        )

    def _manual_import_plan(self, source: SucceededWorkOutput) -> PipelineTransitionPlan:
        if source.output_artifact.role != "manual_import_plan":
            return self._blocked(
                source,
                transition_key="manual-import-plan-admission",
                code="PIPELINE_OUTPUT_ARTIFACT_CONFLICT",
                message="The manual-import plan output role does not match its contract.",
                required_action=(
                    "Re-run the exact manual-import work with the canonical output role."
                ),
                context={
                    "expectedRole": "manual_import_plan",
                    "actualRole": source.output_artifact.role,
                },
            )
        parents = tuple(
            artifact
            for artifact in source.input_artifacts
            if any(pattern.fullmatch(artifact.role) is not None for pattern in _MANUAL_SOURCE_ROLES)
        )
        if not parents:
            return self._blocked(
                source,
                transition_key="manual-import-plan-admission",
                code="PIPELINE_INPUT_ARTIFACT_MISSING",
                message="The manual-import plan has no canonical source artifact binding.",
                required_action=(
                    "Bind exactly one canonical manual source artifact and re-run the source work."
                ),
                context={"requiredRolePattern": "manual[_import]_source:<format>:<mode>"},
            )
        if len(parents) != 1:
            return self._blocked(
                source,
                transition_key="manual-import-plan-admission",
                code="PIPELINE_INPUT_ARTIFACT_CONFLICT",
                message="The manual-import plan has multiple canonical source artifact bindings.",
                required_action=(
                    "Repair the source work so exactly one canonical "
                    "manual source artifact is bound."
                ),
                context={"matchingArtifactIds": [str(item.artifact_id) for item in parents]},
            )
        return self._plan(
            source,
            transition_key="manual-import-plan-admission",
            disposition=PipelineTransitionDisposition.APPLY,
            blocker=None,
        )

    def _manual_record(self, source: SucceededWorkOutput) -> PipelineTransitionPlan:
        if source.output_artifact.role != "manual_import_record":
            return self._blocked(
                source,
                transition_key="manual-record-routing",
                code="PIPELINE_OUTPUT_ARTIFACT_CONFLICT",
                message="The manual-record output role does not match its contract.",
                required_action=(
                    "Re-run the exact manual-record work with the canonical output role."
                ),
                context={
                    "expectedRole": "manual_import_record",
                    "actualRole": source.output_artifact.role,
                },
            )
        return self._blocked(
            source,
            transition_key="manual-record-routing",
            code="MANUAL_RECORD_DOWNSTREAM_SOURCE_UNAVAILABLE",
            message="The manual record has no approved downstream source owner in this campaign.",
            required_action=(
                "Publish exact website or OSM source bindings in a new campaign snapshot before "
                "creating acquisition work."
            ),
            context={"campaignRunId": str(source.run_id)},
        )

    def _blocked(
        self,
        source: SucceededWorkOutput,
        *,
        transition_key: str,
        code: str,
        message: str,
        required_action: str,
        context: Mapping[str, object],
    ) -> PipelineTransitionPlan:
        blocker = PipelineBlocker(
            owner="PipelineAdvancement",
            code=code,
            message=message,
            required_action=required_action,
            context={"sourceWorkUnitId": str(source.source_work_unit_id), **dict(context)},
        )
        return self._plan(
            source,
            transition_key=transition_key,
            disposition=PipelineTransitionDisposition.BLOCK,
            blocker=blocker,
        )

    @staticmethod
    def _plan(
        source: SucceededWorkOutput,
        *,
        transition_key: str,
        disposition: PipelineTransitionDisposition,
        blocker: PipelineBlocker | None,
    ) -> PipelineTransitionPlan:
        digest = _transition_plan_digest(
            source,
            transition_key=transition_key,
            disposition=disposition,
            blocker=blocker,
        )
        return PipelineTransitionPlan(
            source_work_unit_id=source.source_work_unit_id,
            transition_key=transition_key,
            disposition=disposition,
            plan_digest=digest,
            blocker=blocker,
        )


class PipelineAdvancementService:
    def __init__(
        self,
        port: PipelineAdvancementPort,
        registry: PipelineTransitionRegistry | None = None,
    ) -> None:
        self._port = port
        self._registry = registry or PipelineTransitionRegistry()

    def register(
        self,
        source: SucceededWorkOutput,
        *,
        correlation_id: str,
    ) -> PipelineAdvancementStatus:
        _require_token("correlation_id", correlation_id)
        plan = self._registry.plan(source)
        return self._invoke(
            correlation_id,
            lambda: self._port.register(source, plan, correlation_id=correlation_id),
        )

    def claim(self, command: ClaimPipelineAdvancement) -> PipelineAdvancementLease | None:
        return self._invoke(command.correlation_id, lambda: self._port.claim(command))

    def apply(self, command: ApplyPipelineAdvancement) -> PipelineAdvancementStatus:
        return self._invoke(command.correlation_id, lambda: self._port.apply(command))

    def block(self, command: BlockPipelineAdvancement) -> PipelineAdvancementStatus:
        return self._invoke(command.correlation_id, lambda: self._port.block(command))

    @staticmethod
    def _invoke(correlation_id: str, operation: Callable[[], _ResultT]) -> _ResultT:
        _require_token("correlation_id", correlation_id)
        try:
            return operation()
        except PipelineAdvancementConflict as exc:
            raise owner_error(
                error_type=f"collection/{exc.code.lower().replace('_', '-')}",
                owner="PipelineAdvancement",
                code=exc.code,
                message=exc.message,
                context=exc.context,
                required_action=exc.required_action,
                correlation_id=correlation_id,
            ) from exc


def _transition_plan_digest(
    source: SucceededWorkOutput,
    *,
    transition_key: str,
    disposition: PipelineTransitionDisposition,
    blocker: PipelineBlocker | None,
) -> str:
    payload = {
        "sourceWorkUnitId": str(source.source_work_unit_id),
        "runId": str(source.run_id),
        "stageRunId": str(source.stage_run_id),
        "stage": source.stage.value,
        "capability": source.capability,
        "outputContract": source.output_contract,
        "outputDigest": source.output_digest,
        "outputArtifact": _artifact_payload(source.output_artifact),
        "inputArtifacts": [
            _artifact_payload(item)
            for item in sorted(
                source.input_artifacts,
                key=lambda value: (value.role, str(value.artifact_id)),
            )
        ],
        "transitionKey": transition_key,
        "disposition": disposition.value,
        "blocker": (
            None
            if blocker is None
            else {
                "owner": blocker.owner,
                "code": blocker.code,
                "message": blocker.message,
                "requiredAction": blocker.required_action,
                "context": _canonical_mapping(blocker.context),
            }
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _artifact_payload(value: ArtifactIdentity) -> dict[str, object]:
    return {
        "artifactId": str(value.artifact_id),
        "role": value.role,
        "contentDigest": value.content_digest,
        "sizeBytes": value.size_bytes,
        "contentType": value.content_type,
    }


def _canonical_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {key: _canonical_value(item) for key, item in sorted(value.items())}


def _canonical_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return _canonical_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical pipeline value: {type(value).__name__}")


def _require_token(name: str, value: str) -> None:
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid token format")


def _require_sha256(name: str, value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be canonical SHA-256")


def _require_plain_text(name: str, value: str, *, maximum: int) -> None:
    if not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} must contain between 1 and {maximum} characters")
    if "<" in value or ">" in value:
        raise ValueError(f"{name} must not contain markup delimiters")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ValueError(f"{name} contains a forbidden control character")


def _require_aware_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
