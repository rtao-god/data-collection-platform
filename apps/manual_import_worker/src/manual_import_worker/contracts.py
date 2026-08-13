from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Protocol

from collection_contracts import ManualImportFormat, ManualImportMode
from source_connector_sdk import LeaseArtifact, WorkerLease

_SOURCE_ROLE = re.compile(
    r"^(?:manual_source|manual_import_source):"
    r"(?P<format>csv|json|jsonl):(?P<mode>atomic|partial)$"
)


@dataclass(frozen=True, slots=True)
class ManualImportWorkerSettings:
    gateway_url: str
    gateway_token: str
    build_identity: str
    resource_profile: str
    poll_interval_seconds: float
    lease_duration_seconds: int
    heartbeat_interval_seconds: int
    transfer_timeout_seconds: float
    max_source_bytes: int

    @classmethod
    def from_environment(cls) -> ManualImportWorkerSettings:
        return cls(
            gateway_url=_required_environment("WORKER_GATEWAY_URL"),
            gateway_token=_required_environment("WORKER_GATEWAY_TOKEN"),
            build_identity=os.getenv("WORKER_BUILD_IDENTITY", "manual-import-worker"),
            resource_profile=os.getenv("WORKER_RESOURCE_PROFILE", "manual-import"),
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
        )

    def __post_init__(self) -> None:
        if self.heartbeat_interval_seconds >= self.lease_duration_seconds:
            raise ValueError("heartbeat interval must be shorter than lease duration")


@dataclass(frozen=True, slots=True)
class ManualImportSource:
    artifact: LeaseArtifact
    format: ManualImportFormat
    mode: ManualImportMode


class ManualImportGateway(Protocol):
    def register(self, settings: ManualImportWorkerSettings) -> None: ...

    def acquire(self, settings: ManualImportWorkerSettings) -> WorkerLease | None: ...

    def heartbeat(
        self, lease: WorkerLease, settings: ManualImportWorkerSettings
    ) -> WorkerLease: ...

    def read_source(
        self,
        lease: WorkerLease,
        source: LeaseArtifact,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes: ...

    def publish_plan(
        self,
        lease: WorkerLease,
        payload: bytes,
        *,
        content_digest: str,
        timeout_seconds: float,
    ) -> object: ...

    def complete(self, lease: WorkerLease, *, plan_digest: str, upload: object) -> None: ...

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: str,
        code: str,
        message: str,
        required_action: str,
    ) -> None: ...


def parse_manual_import_source(lease: WorkerLease) -> ManualImportSource:
    if len(lease.input_artifacts) != 1:
        raise ValueError("manual import work requires exactly one source artifact")
    artifact = lease.input_artifacts[0]
    match = _SOURCE_ROLE.fullmatch(artifact.role)
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
    return ManualImportSource(artifact=artifact, format=format_value, mode=mode_value)


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
