from __future__ import annotations

from collections.abc import Mapping

from tools.compose_topology.constants import (
    APPLICATION_SERVICES,
    EXPECTED_PROFILES,
    EXPECTED_SERVICES,
    FORBIDDEN_WORKER_ENVIRONMENT,
    INFRASTRUCTURE_SERVICES,
    WORKERS,
)
from tools.compose_topology.support import (
    fail,
    mapping,
    service_failure,
    service_secrets,
    strings,
)


def require_service_inventory(services: Mapping[str, object]) -> None:
    actual = frozenset(services)
    if actual != EXPECTED_SERVICES:
        fail(
            code="COMPOSE_SERVICE_INVENTORY_MISMATCH",
            message="The application Compose service inventory differs from its owner contract.",
            context={"expected": sorted(EXPECTED_SERVICES), "actual": sorted(actual)},
            required_action="Declare only production services with a real composition root.",
        )


def require_process_hardening(services: Mapping[str, object]) -> None:
    for name in sorted(EXPECTED_SERVICES - INFRASTRUCTURE_SERVICES):
        service = mapping(services[name], owner=f"services.{name}")
        if service.get("read_only") is not True:
            service_failure(name, "read_only", True)
        if "ALL" not in strings(service.get("cap_drop", ()), owner=f"{name}.cap_drop"):
            service_failure(name, "cap_drop", ["ALL"])
        security_options = set(
            strings(service.get("security_opt", ()), owner=f"{name}.security_opt")
        )
        if "no-new-privileges:true" not in security_options:
            service_failure(name, "security_opt", ["no-new-privileges:true"])
        if service.get("privileged"):
            service_failure(name, "privileged", False)


def require_worker_boundaries(services: Mapping[str, object]) -> None:
    for name in sorted(WORKERS):
        service = mapping(services[name], owner=f"services.{name}")
        worker_environment = mapping(
            service.get("environment", {}), owner=f"services.{name}.environment"
        )
        forbidden = sorted(
            key
            for key in worker_environment
            if any(
                key == prefix or key.startswith(prefix) for prefix in FORBIDDEN_WORKER_ENVIRONMENT
            )
        )
        if forbidden:
            fail(
                code="COMPOSE_WORKER_OWNER_CREDENTIAL_EXPOSED",
                message="A capability worker contains platform-owner credentials.",
                context={"service": name, "environmentVariables": forbidden},
                required_action="Route worker access through Worker Gateway contracts only.",
            )
        mounted = service_secrets(service)
        if mounted:
            fail(
                code="COMPOSE_WORKER_SECRET_EXPOSED",
                message="A capability worker mounts a platform-owner secret.",
                context={"service": name, "secrets": sorted(mounted)},
                required_action="Remove the secret and use the capability-scoped gateway token.",
            )
        if service.get("ports"):
            fail(
                code="COMPOSE_WORKER_PORT_PUBLISHED",
                message="A capability worker publishes a host port.",
                context={"service": name},
                required_action="Keep workers reachable only through their owned internal path.",
            )


def require_gateway_boundary(services: Mapping[str, object]) -> None:
    gateway = mapping(services["worker-gateway"], owner="services.worker-gateway")
    if gateway.get("ports"):
        fail(
            code="COMPOSE_GATEWAY_PORT_PUBLISHED",
            message="Worker Gateway publishes a host port.",
            context={"service": "worker-gateway"},
            required_action="Keep Worker Gateway internal to workers and infrastructure.",
        )
    gateway_environment = mapping(
        gateway.get("environment", {}), owner="worker-gateway.environment"
    )
    expected = {
        "WORKER_GATEWAY_BIND_MODE": "container",
        "WORKER_GATEWAY_HOST": "0.0.0.0",  # noqa: S104 -- container-internal bind
    }
    mismatches = {
        key: {"expected": value, "actual": gateway_environment.get(key)}
        for key, value in expected.items()
        if gateway_environment.get(key) != value
    }
    if mismatches:
        fail(
            code="COMPOSE_GATEWAY_BIND_CONTRACT_MISMATCH",
            message="Worker Gateway does not use its explicit container bind contract.",
            context={"mismatches": mismatches},
            required_action="Restore the exact internal container bind configuration.",
        )


def require_profiles_and_images(services: Mapping[str, object]) -> None:
    for name, expected_profiles in EXPECTED_PROFILES.items():
        service = mapping(services[name], owner=f"services.{name}")
        actual = frozenset(strings(service.get("profiles", ()), owner=f"{name}.profiles"))
        if actual != expected_profiles:
            fail(
                code="COMPOSE_PROFILE_CONTRACT_MISMATCH",
                message="A one-shot service has an unexpected Compose profile.",
                context={
                    "service": name,
                    "expected": sorted(expected_profiles),
                    "actual": sorted(actual),
                },
                required_action="Restore the exact bootstrap, migration, or tools profile.",
            )
    for name in sorted(APPLICATION_SERVICES - frozenset(EXPECTED_PROFILES)):
        service = mapping(services[name], owner=f"services.{name}")
        if service.get("profiles"):
            fail(
                code="COMPOSE_RUNTIME_PROFILE_UNEXPECTED",
                message="A default runtime service unexpectedly requires a Compose profile.",
                context={"service": name, "profiles": service.get("profiles")},
                required_action="Keep current runtime owners in the default application profile.",
            )
    for name in sorted(APPLICATION_SERVICES):
        service = mapping(services[name], owner=f"services.{name}")
        if not service.get("build"):
            fail(
                code="COMPOSE_APPLICATION_IMAGE_OWNER_MISSING",
                message="An application service has no independently buildable image owner.",
                context={"service": name},
                required_action="Add the real image owner or remove the placeholder service.",
            )
    for name, raw_service in services.items():
        service = mapping(raw_service, owner=f"services.{name}")
        image = service.get("image")
        if isinstance(image, str) and image.endswith(":latest"):
            fail(
                code="COMPOSE_LATEST_IMAGE_FORBIDDEN",
                message="A Compose service uses the mutable latest image tag.",
                context={"service": name, "image": image},
                required_action="Pin the image to an approved immutable version contract.",
            )
