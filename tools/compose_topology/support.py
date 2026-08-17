from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast


@dataclass(frozen=True, slots=True)
class ComposeTopologyError(Exception):
    code: str
    message: str
    context: Mapping[str, object]
    required_action: str

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)


def mapping(raw: object, *, owner: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        fail(
            code="COMPOSE_OBJECT_SHAPE_INVALID",
            message="A resolved Compose owner is not an object.",
            context={"owner": owner, "actualType": type(raw).__name__},
            required_action="Render a valid merged Compose JSON object.",
        )
    return cast(Mapping[str, object], raw)


def sequence(raw: object, *, owner: str) -> Sequence[object]:
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return cast(Sequence[object], raw)
    fail(
        code="COMPOSE_SEQUENCE_SHAPE_INVALID",
        message="A resolved Compose owner is not a sequence.",
        context={"owner": owner, "actualType": type(raw).__name__},
        required_action="Render a valid merged Compose sequence.",
    )


def strings(raw: object, *, owner: str) -> tuple[str, ...]:
    values = sequence(raw, owner=owner)
    if not all(isinstance(value, str) for value in values):
        fail(
            code="COMPOSE_STRING_SEQUENCE_INVALID",
            message="A resolved Compose string sequence contains another value type.",
            context={"owner": owner},
            required_action="Use only exact string values in this Compose sequence.",
        )
    return tuple(cast(str, value) for value in values)


def service_networks(service: Mapping[str, object]) -> frozenset[str]:
    raw = service.get("networks", {})
    if isinstance(raw, Mapping):
        return frozenset(str(name) for name in raw)
    return frozenset(strings(raw, owner="service.networks"))


def service_secrets(service: Mapping[str, object]) -> frozenset[str]:
    result: set[str] = set()
    for item in sequence(service.get("secrets", ()), owner="service.secrets"):
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, Mapping) and isinstance(item.get("source"), str):
            result.add(cast(str, item["source"]))
        else:
            fail(
                code="COMPOSE_SECRET_MOUNT_SHAPE_INVALID",
                message="A service secret mount has an unsupported shape.",
                context={"actualType": type(item).__name__},
                required_action="Use a secret name or an exact source mapping.",
            )
    return frozenset(result)


def published_ports(
    service: Mapping[str, object], *, service_name: str
) -> set[tuple[str, int, int, str]]:
    result: set[tuple[str, int, int, str]] = set()
    for position, raw_port in enumerate(
        sequence(service.get("ports", ()), owner=f"{service_name}.ports")
    ):
        port = mapping(raw_port, owner=f"{service_name}.ports[{position}]")
        host_ip = port.get("host_ip")
        if not isinstance(host_ip, str):
            fail(
                code="COMPOSE_PORT_HOST_INVALID",
                message="A published port has no explicit host address.",
                context={"service": service_name, "position": position},
                required_action="Bind every published port explicitly to 127.0.0.1.",
            )
        try:
            published = int(str(port.get("published")))
            target = int(str(port.get("target")))
        except ValueError as exc:
            fail(
                code="COMPOSE_PORT_VALUE_INVALID",
                message="A published port has an invalid host or target value.",
                context={
                    "service": service_name,
                    "position": position,
                    "causeType": type(exc).__name__,
                },
                required_action="Use exact numeric published and target ports.",
            )
        result.add((host_ip, published, target, str(port.get("protocol", "tcp"))))
    return result


def resolve_secret_source(raw: object, secret_root: Path) -> Path:
    if not isinstance(raw, str) or not raw:
        fail(
            code="COMPOSE_SECRET_SOURCE_MISSING",
            message="A Compose secret has no file source.",
            context={"actualType": type(raw).__name__},
            required_action="Configure the exact materialized file path.",
        )
    candidate = Path(raw)
    if candidate.is_absolute() or candidate.exists():
        unresolved = candidate
    else:
        unresolved = secret_root.parent / candidate
    if unresolved.is_symlink():
        fail(
            code="COMPOSE_SECRET_SOURCE_UNSAFE",
            message="A Compose secret source is a symbolic link.",
            context={"source": str(unresolved)},
            required_action="Use one owned regular file under the secret root.",
        )
    return unresolved.resolve()


def required_port(environment: Mapping[str, str], name: str) -> int:
    value = required_environment(environment, name)
    try:
        port = int(value)
    except ValueError as exc:
        fail(
            code="COMPOSE_PORT_ENVIRONMENT_INVALID",
            message="A required published-port environment value is not an integer.",
            context={"variable": name, "value": value, "causeType": type(exc).__name__},
            required_action="Set one valid TCP port number.",
        )
    if not 1 <= port <= 65_535:
        fail(
            code="COMPOSE_PORT_ENVIRONMENT_INVALID",
            message="A required published port is outside the valid TCP range.",
            context={"variable": name, "value": port},
            required_action="Set one valid TCP port number.",
        )
    return port


def required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or value == "":
        fail(
            code="COMPOSE_REQUIRED_ENVIRONMENT_MISSING",
            message="A required Compose proof environment value is unavailable.",
            context={"variable": name},
            required_action="Load the exact Compose environment file before verification.",
        )
    return value


def service_failure(service: str, field: str, expected: object) -> NoReturn:
    fail(
        code="COMPOSE_PROCESS_HARDENING_MISMATCH",
        message="An application service violates its process hardening contract.",
        context={"service": service, "field": field, "expected": expected},
        required_action="Restore the non-root read-only least-privilege process settings.",
    )


def network_failure(network: str, field: str, expected: object) -> NoReturn:
    fail(
        code="COMPOSE_NETWORK_CONTRACT_MISMATCH",
        message="A Compose network violates its isolation contract.",
        context={"network": network, "field": field, "expected": expected},
        required_action="Restore the exact internal, egress, or loopback network contract.",
    )


def fail(
    *,
    code: str,
    message: str,
    context: Mapping[str, object],
    required_action: str,
) -> NoReturn:
    raise ComposeTopologyError(
        code=code,
        message=message,
        context=context,
        required_action=required_action,
    )
