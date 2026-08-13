from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

import collection_application as application

from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportChildWork,
)
from collection_infrastructure.postgres.manual_import_admission import (
    ManualImportAdmissionConflict,
)
from collection_infrastructure.postgres.work_engine import PostgresWorkEngineStore


class PostgresManualImportChildWorkWriter:
    """Uses the Work Engine's transaction-local enqueue owner without opening a second transaction."""

    def __init__(self, engine: Engine) -> None:
        self._store = PostgresWorkEngineStore(engine)
        self._enqueue = _resolve_transactional_enqueue(self._store)
        self._command_type = _resolve_application_type(
            "EnqueueWork", "EnqueueWorkUnit", "CreateWorkUnit"
        )

    def enqueue(
        self,
        connection: Connection,
        command: AdmitManualImportPlan,
        children: Sequence[ManualImportChildWork],
    ) -> tuple[UUID, ...]:
        work_ids: list[UUID] = []
        for child in children:
            enqueue_command = self._build_command(command, child)
            result = self._enqueue(connection, enqueue_command)
            result_work_id = getattr(result, "work_id", child.work_id)
            if UUID(str(result_work_id)) != child.work_id:
                raise _conflict(
                    command,
                    code="MANUAL_IMPORT_CHILD_WORK_ID_MISMATCH",
                    message="The Work Engine returned a different work identity.",
                )
            work_ids.append(child.work_id)
        return tuple(work_ids)

    def _build_command(
        self,
        command: AdmitManualImportPlan,
        child: ManualImportChildWork,
    ) -> object:
        payload = json.loads(child.input_payload)
        stage = _enum_value("WorkStage", command.target_stage)
        capability = _enum_value("WorkCapability", command.target_capability)
        artifacts = _input_artifacts(command)
        retry_policy = _retry_policy()
        values: dict[str, object] = {
            "work_id": child.work_id,
            "run_id": command.run_id,
            "stage_name": command.stage_name,
            "stage": stage,
            "work_stage": stage,
            "capability": capability,
            "required_capability": capability,
            "semantic_key": child.semantic_key,
            "semantic_idempotency_key": child.semantic_key,
            "input_digest": child.input_digest,
            "expected_output_contract": command.target_output_contract,
            "output_contract": command.target_output_contract,
            "source_key": None,
            "policy_digest": None,
            "priority": 0,
            "available_at_utc": datetime.now(UTC),
            "correlation_id": command.correlation_id,
            "input_payload": payload,
            "input_artifacts": artifacts,
            "artifacts": artifacts,
            "retry_policy": retry_policy,
            "max_attempts": 3,
            "initial_retry_delay_seconds": 30,
            "retry_multiplier": 2.0,
            "maximum_retry_delay_seconds": 900,
            "max_retry_delay_seconds": 900,
        }
        signature = inspect.signature(self._command_type)
        arguments: dict[str, object] = {}
        missing: list[str] = []
        for name, parameter in signature.parameters.items():
            if name in values:
                arguments[name] = values[name]
            elif parameter.default is inspect.Parameter.empty:
                missing.append(name)
        if missing:
            raise _conflict(
                command,
                code="MANUAL_IMPORT_WORK_COMMAND_CONTRACT_MISMATCH",
                message=(
                    "The Work Engine enqueue command has unsupported required fields: "
                    + ", ".join(sorted(missing))
                ),
            )
        return self._command_type(**arguments)


def _resolve_transactional_enqueue(
    store: PostgresWorkEngineStore,
) -> Callable[[Connection, object], object]:
    candidates: list[tuple[str, Callable[..., object]]] = []
    for name in dir(store):
        if not name.startswith("_") or "enqueue" not in name:
            continue
        value = getattr(store, name)
        if not callable(value):
            continue
        method = cast(Callable[..., object], value)
        parameters = tuple(inspect.signature(method).parameters.values())
        if len(parameters) < 2:
            continue
        if parameters[0].name not in {"connection", "conn"}:
            continue
        candidates.append((name, method))
    if len(candidates) != 1:
        names = [name for name, _ in candidates]
        raise RuntimeError(
            f"Work Engine must expose exactly one transaction-local enqueue method; found {names}"
        )
    method = candidates[0][1]

    def invoke(connection: Connection, command: object) -> object:
        return method(connection, command)

    return invoke


def _resolve_application_type(*names: str) -> type[object]:
    for name in names:
        value = getattr(application, name, None)
        if isinstance(value, type):
            return cast(type[object], value)
    raise RuntimeError(f"Application contract was not exported under any of {names}")


def _enum_value(name: str, value: str) -> object:
    enum_type = getattr(application, name, None)
    if isinstance(enum_type, type):
        try:
            return enum_type(value)
        except ValueError as exc:
            raise RuntimeError(f"{name} does not support {value!r}") from exc
    return value


def _input_artifacts(command: AdmitManualImportPlan) -> tuple[object, ...]:
    artifact_type = None
    for name in ("WorkInputArtifact", "InputArtifactBinding", "EnqueueInputArtifact"):
        candidate = getattr(application, name, None)
        if isinstance(candidate, type):
            artifact_type = cast(type[object], candidate)
            break
    definitions = (
        (command.plan.source_artifact_id, "manual_import_source", 0),
        (command.plan.plan_artifact_id, "manual_import_plan", 1),
    )
    if artifact_type is None:
        return tuple(
            {"artifactId": str(artifact_id), "role": role, "position": position}
            for artifact_id, role, position in definitions
        )
    bindings: list[object] = []
    signature = inspect.signature(artifact_type)
    for artifact_id, role, position in definitions:
        values: Mapping[str, object] = {
            "artifact_id": artifact_id,
            "role": role,
            "position": position,
        }
        arguments = {
            name: values[name]
            for name, parameter in signature.parameters.items()
            if name in values and parameter.kind is not inspect.Parameter.VAR_KEYWORD
        }
        bindings.append(artifact_type(**arguments))
    return tuple(bindings)


def _retry_policy() -> object | None:
    for name in ("RetryPolicy", "WorkRetryPolicy"):
        candidate = getattr(application, name, None)
        if not isinstance(candidate, type):
            continue
        policy_type = cast(type[object], candidate)
        values: Mapping[str, object] = {
            "max_attempts": 3,
            "initial_delay_seconds": 30,
            "initial_retry_delay_seconds": 30,
            "multiplier": 2.0,
            "retry_multiplier": 2.0,
            "maximum_delay_seconds": 900,
            "maximum_retry_delay_seconds": 900,
            "max_retry_delay_seconds": 900,
        }
        signature = inspect.signature(policy_type)
        arguments = {
            name: values[name]
            for name, parameter in signature.parameters.items()
            if name in values and parameter.kind is not inspect.Parameter.VAR_KEYWORD
        }
        return policy_type(**arguments)
    return None


def _conflict(
    command: AdmitManualImportPlan, *, code: str, message: str
) -> ManualImportAdmissionConflict:
    return ManualImportAdmissionConflict(
        code=code,
        message=message,
        context={
            "admissionId": str(command.admission_id),
            "planDigest": command.plan.plan_digest,
        },
        required_action=(
            "Align the manual import admission adapter with the canonical Work Engine contract."
        ),
    )
