from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from source_connector_sdk import WorkerLease, WorkFailureKind


@dataclass(frozen=True, slots=True)
class ResolutionWorkerSettings:
    gateway_url: str
    gateway_token: str
    build_identity: str
    lease_duration_seconds: int = 300
    heartbeat_interval_seconds: int = 60
    poll_interval_seconds: float = 5.0
    maximum_input_bytes: int = 64 * 1024 * 1024
    gateway_timeout_seconds: float = 30.0

    @property
    def capability(self) -> str:
        return "entity_resolution"

    @property
    def output_contract(self) -> str:
        return "entity-resolution-snapshot@1"

    @classmethod
    def from_environment(cls) -> ResolutionWorkerSettings:
        return cls(
            gateway_url=_required("WORKER_GATEWAY_BASE_URL"),
            gateway_token=_required("WORKER_GATEWAY_TOKEN"),
            build_identity=_required("RESOLUTION_WORKER_BUILD_IDENTITY"),
            lease_duration_seconds=_integer("RESOLUTION_LEASE_SECONDS", 300),
            heartbeat_interval_seconds=_integer("RESOLUTION_HEARTBEAT_SECONDS", 60),
            poll_interval_seconds=_float("RESOLUTION_POLL_SECONDS", 5.0),
            maximum_input_bytes=_integer("RESOLUTION_MAXIMUM_INPUT_BYTES", 64 * 1024 * 1024),
            gateway_timeout_seconds=_float("WORKER_GATEWAY_TIMEOUT_SECONDS", 30.0),
        )


class ResolutionGateway(Protocol):
    def register(self, settings: ResolutionWorkerSettings) -> None: ...

    def acquire(self, settings: ResolutionWorkerSettings) -> WorkerLease | None: ...

    def heartbeat(
        self,
        lease: WorkerLease,
        settings: ResolutionWorkerSettings,
    ) -> WorkerLease: ...

    def read_input(
        self,
        lease: WorkerLease,
        *,
        role: str,
        maximum_bytes: int,
    ) -> bytes: ...

    def publish_and_complete(
        self,
        lease: WorkerLease,
        *,
        content: bytes,
        content_type: str,
        output_role: str,
        output_digest: str,
    ) -> None: ...

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: WorkFailureKind,
        code: str,
        message: str,
        required_action: str,
    ) -> None: ...


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"required resolution worker setting {name} is missing")
    return value.strip()


def _integer(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"resolution worker setting {name} is invalid") from exc
    if parsed <= 0:
        raise RuntimeError(f"resolution worker setting {name} must be positive")
    return parsed


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"resolution worker setting {name} is invalid") from exc
    if parsed <= 0:
        raise RuntimeError(f"resolution worker setting {name} must be positive")
    return parsed
