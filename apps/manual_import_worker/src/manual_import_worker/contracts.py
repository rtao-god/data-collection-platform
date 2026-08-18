from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from collection_contracts import ManualImportFormat, ManualImportMode
from manual_import_core import (
    parse_manual_import_plan_record_role,
)
from source_connector_sdk import ArtifactKind, LeaseArtifact, WorkerLease

_MANUAL_CAPABILITIES = frozenset({"manual_import", "manual_record"})
_SOURCE_ROLE = re.compile(
    r"^(?:manual_source|manual_import_source):"
    r"(?P<format>csv|json|jsonl):(?P<mode>atomic|partial)$"
)
type ManualWorkerCapability = Literal["manual_import", "manual_record"]


@dataclass(frozen=True, slots=True)
class ManualWorkerOutputContract:
    capability: ManualWorkerCapability
    output_contract: str
    output_role: str
    artifact_kind: ArtifactKind
    content_type: str


_OUTPUT_CONTRACTS: dict[ManualWorkerCapability, ManualWorkerOutputContract] = {
    "manual_import": ManualWorkerOutputContract(
        capability="manual_import",
        output_contract="manual-import-plan@1",
        output_role="manual_import_plan",
        artifact_kind="diagnostic_artifact",
        content_type="application/vnd.collection.manual-import-plan+json",
    ),
    "manual_record": ManualWorkerOutputContract(
        capability="manual_record",
        output_contract="manual-import-record@1",
        output_role="manual_import_record",
        artifact_kind="derived_artifact",
        content_type="application/vnd.collection.manual-import-record+json",
    ),
}


@dataclass(frozen=True, slots=True)
class ManualWorkerSettings:
    gateway_url: str
    gateway_token: str
    capability: ManualWorkerCapability
    build_identity: str
    resource_profile: str
    poll_interval_seconds: float
    lease_duration_seconds: int
    heartbeat_interval_seconds: int
    transfer_timeout_seconds: float
    max_source_bytes: int
    max_plan_bytes: int

    @classmethod
    def from_environment(cls) -> ManualWorkerSettings:
        capability = _manual_capability_environment("MANUAL_WORKER_CAPABILITY")
        process_name = capability.replace("_", "-") + "-worker"
        return cls(
            gateway_url=_required_environment("WORKER_GATEWAY_URL"),
            gateway_token=_required_environment("WORKER_GATEWAY_TOKEN"),
            capability=capability,
            build_identity=os.getenv("WORKER_BUILD_IDENTITY", process_name),
            resource_profile=os.getenv("WORKER_RESOURCE_PROFILE", capability.replace("_", "-")),
            poll_interval_seconds=_float_environment(
                "WORKER_POLL_INTERVAL_SECONDS", default=2.0, minimum=0.05, maximum=300.0
            ),
            lease_duration_seconds=_int_environment(
                "WORKER_LEASE_DURATION_SECONDS", default=300, minimum=30, maximum=3_600
            ),
            heartbeat_interval_seconds=_int_environment(
                "WORKER_HEARTBEAT_INTERVAL_SECONDS", default=60, minimum=5, maximum=600
            ),
            transfer_timeout_seconds=_float_environment(
                "WORKER_TRANSFER_TIMEOUT_SECONDS", default=60.0, minimum=1.0, maximum=600.0
            ),
            max_source_bytes=_int_environment(
                "MANUAL_IMPORT_MAX_SOURCE_BYTES",
                default=16 * 1024 * 1024,
                minimum=1,
                maximum=64 * 1024 * 1024,
            ),
            max_plan_bytes=_int_environment(
                "MANUAL_IMPORT_MAX_PLAN_BYTES",
                default=64 * 1024 * 1024,
                minimum=1,
                maximum=64 * 1024 * 1024,
            ),
        )

    def __post_init__(self) -> None:
        if self.capability not in _MANUAL_CAPABILITIES:
            raise ValueError("manual worker capability is unsupported")
        if self.heartbeat_interval_seconds >= self.lease_duration_seconds:
            raise ValueError("heartbeat interval must be shorter than lease duration")

    @property
    def output(self) -> ManualWorkerOutputContract:
        return manual_worker_output_contract(self.capability)


@dataclass(frozen=True, slots=True)
class ManualImportSource:
    artifact: LeaseArtifact
    format: ManualImportFormat
    mode: ManualImportMode


@dataclass(frozen=True, slots=True)
class ManualRecordSource:
    source_artifact: LeaseArtifact
    plan_artifact: LeaseArtifact
    format: ManualImportFormat
    mode: ManualImportMode
    plan_record_position: int


class ManualWorkerGateway(Protocol):
    def register(self, settings: ManualWorkerSettings) -> None: ...

    def acquire(self, settings: ManualWorkerSettings) -> WorkerLease | None: ...

    def heartbeat(self, lease: WorkerLease, settings: ManualWorkerSettings) -> WorkerLease: ...

    def read_artifact(
        self,
        lease: WorkerLease,
        artifact: LeaseArtifact,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes: ...

    def publish_output(
        self,
        lease: WorkerLease,
        payload: bytes,
        *,
        content_digest: str,
    ) -> object: ...

    def complete(self, lease: WorkerLease, *, output_digest: str, upload: object) -> None: ...

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: str,
        code: str,
        message: str,
        required_action: str,
    ) -> None: ...


def manual_worker_output_contract(
    capability: ManualWorkerCapability,
) -> ManualWorkerOutputContract:
    try:
        return _OUTPUT_CONTRACTS[capability]
    except KeyError as exc:
        raise ValueError("manual worker capability has no output contract") from exc


def parse_manual_import_source(lease: WorkerLease) -> ManualImportSource:
    _require_lease_contract(
        lease,
        capability="manual_import",
        output_contract="manual-import-plan@1",
        source_permit_required=True,
    )
    if len(lease.input_artifacts) != 1:
        raise ValueError("manual import work requires exactly one source artifact")
    artifact = lease.input_artifacts[0]
    format_value, mode_value = _parse_source_role(artifact.role)
    return ManualImportSource(artifact=artifact, format=format_value, mode=mode_value)


def parse_manual_record_source(lease: WorkerLease) -> ManualRecordSource:
    _require_lease_contract(
        lease,
        capability="manual_record",
        output_contract="manual-import-record@1",
        source_permit_required=False,
    )
    if len(lease.input_artifacts) != 2:
        raise ValueError("manual record work requires one source and one selected plan artifact")

    source_matches: list[tuple[LeaseArtifact, ManualImportFormat, ManualImportMode]] = []
    plan_matches: list[tuple[LeaseArtifact, int]] = []
    for artifact in lease.input_artifacts:
        source_match = _SOURCE_ROLE.fullmatch(artifact.role)
        if source_match is not None:
            format_value, mode_value = _parse_source_role(artifact.role)
            source_matches.append((artifact, format_value, mode_value))
            continue
        if artifact.role.startswith("manual_import_plan_record:"):
            plan_matches.append((artifact, parse_manual_import_plan_record_role(artifact.role)))
            continue
        raise ValueError(f"manual record input artifact role is unsupported: {artifact.role}")

    if len(source_matches) != 1 or len(plan_matches) != 1:
        raise ValueError(
            "manual record work requires exactly one canonical source role and one plan-record role"
        )
    source_artifact, format_value, mode_value = source_matches[0]
    plan_artifact, position = plan_matches[0]
    return ManualRecordSource(
        source_artifact=source_artifact,
        plan_artifact=plan_artifact,
        format=format_value,
        mode=mode_value,
        plan_record_position=position,
    )


def _require_lease_contract(
    lease: WorkerLease,
    *,
    capability: ManualWorkerCapability,
    output_contract: str,
    source_permit_required: bool,
) -> None:
    if lease.stage != "discovery":
        raise ValueError("manual worker lease must belong to the discovery stage")
    if lease.capability != capability:
        raise ValueError("manual worker lease capability differs from the configured owner")
    if lease.expected_output_contract != output_contract:
        raise ValueError("manual worker lease output contract is not canonical")
    if source_permit_required and lease.source_permit is None:
        raise ValueError("manual import work requires a source permit")
    if not source_permit_required and lease.source_permit is not None:
        raise ValueError("manual record work must not receive a source permit")


def _parse_source_role(role: str) -> tuple[ManualImportFormat, ManualImportMode]:
    match = _SOURCE_ROLE.fullmatch(role)
    if match is None:
        raise ValueError(
            "manual import source role must be manual_source:<csv|json|jsonl>:<atomic|partial>"
        )
    format_value = {
        "csv": ManualImportFormat.CSV,
        "json": ManualImportFormat.JSON,
        "jsonl": ManualImportFormat.JSONL,
    }[match.group("format")]
    mode_value = {
        "atomic": ManualImportMode.ATOMIC,
        "partial": ManualImportMode.PARTIAL,
    }[match.group("mode")]
    return format_value, mode_value


def _manual_capability_environment(name: str) -> ManualWorkerCapability:
    raw = _required_environment(name)
    if raw not in _MANUAL_CAPABILITIES:
        raise ValueError(f"{name} must be manual_import or manual_record")
    return cast(ManualWorkerCapability, raw)


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _int_environment(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None else int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float_environment(name: str, *, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    value = default if raw is None else float(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value
