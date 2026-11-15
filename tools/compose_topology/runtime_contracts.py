from __future__ import annotations

from collections.abc import Mapping

from tools.compose_topology.constants import (
    APPLICATION_SERVICES,
    DAGSTER_SERVICES,
    EXPECTED_PROFILES,
    EXPECTED_SERVICES,
    FORBIDDEN_DAGSTER_ENVIRONMENT,
    FORBIDDEN_WORKER_ENVIRONMENT,
    INFRASTRUCTURE_SERVICES,
    MANUAL_WORKER_CAPABILITIES,
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
        expected_manual_capability = MANUAL_WORKER_CAPABILITIES.get(name)
        if expected_manual_capability is not None:
            actual_manual_capability = worker_environment.get("MANUAL_WORKER_CAPABILITY")
            if actual_manual_capability != expected_manual_capability:
                fail(
                    code="COMPOSE_MANUAL_WORKER_CAPABILITY_MISMATCH",
                    message="A manual worker does not declare its exact process capability.",
                    context={
                        "service": name,
                        "expected": expected_manual_capability,
                        "actual": actual_manual_capability,
                    },
                    required_action=(
                        "Configure one explicit manual capability per worker process."
                    ),
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


def require_dagster_boundary(services: Mapping[str, object]) -> None:
    expected = {
        "dagster-webserver": {
            "DAGSTER_HOME": "/var/lib/dagster",
            "DAGSTER_POSTGRES_HOST": "dagster-postgres",
            "DAGSTER_POSTGRES_DB": "dagster_metadata",
            "DAGSTER_POSTGRES_USER": "dagster_runtime",
            "DAGSTER_POSTGRES_PASSWORD_FILE": "/run/secrets/dagster-postgres-password",
        },
        "dagster-daemon": {
            "DAGSTER_HOME": "/var/lib/dagster",
            "DAGSTER_POSTGRES_HOST": "dagster-postgres",
            "DAGSTER_POSTGRES_DB": "dagster_metadata",
            "DAGSTER_POSTGRES_USER": "dagster_runtime",
            "DAGSTER_POSTGRES_PASSWORD_FILE": "/run/secrets/dagster-postgres-password",
            "PIPELINE_ADVANCEMENT_GATEWAY_URL": "http://worker-gateway:8080",
            "PIPELINE_ADVANCEMENT_TOKEN_FILE": "/run/secrets/pipeline-advancement-token",
        },
    }
    build_identities: set[str] = set()
    for name in sorted(DAGSTER_SERVICES):
        service = mapping(services[name], owner=f"services.{name}")
        environment = mapping(service.get("environment", {}), owner=f"services.{name}.environment")
        forbidden = sorted(
            key
            for key in environment
            if any(
                key == prefix or key.startswith(prefix) for prefix in FORBIDDEN_DAGSTER_ENVIRONMENT
            )
            or key in {"COLLECTOR_DATABASE_URL", "DAGSTER_POSTGRES_PASSWORD"}
        )
        if forbidden:
            fail(
                code="COMPOSE_DAGSTER_COLLECTION_CREDENTIAL_EXPOSED",
                message="Dagster contains Collection DB or direct credential values.",
                context={"service": name, "environmentVariables": forbidden},
                required_action=(
                    "Keep Collection DB credentials in Collection-owned services and mount only "
                    "file-backed Dagster metadata credentials."
                ),
            )
        mismatches = {
            key: {"expected": value, "actual": environment.get(key)}
            for key, value in expected[name].items()
            if environment.get(key) != value
        }
        if mismatches:
            fail(
                code="COMPOSE_DAGSTER_RUNTIME_CONTRACT_MISMATCH",
                message="Dagster runtime configuration differs from its isolated gateway contract.",
                context={"service": name, "mismatches": mismatches},
                required_action=(
                    "Use the dedicated metadata DB and grant Gateway access only to the daemon."
                ),
            )
        build_identities.add(_dagster_build_identity(name, environment))
        _require_dagster_workspace_command(name, service)
        if name == "dagster-webserver" and any(
            key.startswith("PIPELINE_ADVANCEMENT_") for key in environment
        ):
            fail(
                code="COMPOSE_DAGSTER_CAPABILITY_EXPOSED",
                message="Dagster webserver has the daemon's Worker Gateway capability.",
                context={"service": name},
                required_action="Keep the advancement service token on the daemon only.",
            )
        if not service.get("image") and not service.get("build"):
            service_failure(name, "build", "dedicated Dagster runtime image")
    if len(build_identities) != 1:
        fail(
            code="COMPOSE_DAGSTER_BUILD_IDENTITY_MISMATCH",
            message="Dagster processes do not report one consistent build identity.",
            context={"identities": sorted(build_identities)},
            required_action=(
                "Set the same COLLECTOR_BUILD_IDENTITY for Dagster webserver and daemon."
            ),
        )


def _dagster_build_identity(name: str, environment: Mapping[str, object]) -> str:
    value = environment.get("COLLECTOR_BUILD_IDENTITY")
    if not isinstance(value, str) or not value.strip():
        fail(
            code="COMPOSE_DAGSTER_BUILD_IDENTITY_MISSING",
            message="A Dagster process may execute a run without build identity metadata.",
            context={"service": name},
            required_action="Set COLLECTOR_BUILD_IDENTITY on both Dagster runtime processes.",
        )
    return value.strip()


def _require_dagster_workspace_command(name: str, service: Mapping[str, object]) -> None:
    expected = {
        "dagster-webserver": (
            "dagster-webserver",
            "-h",
            "0.0.0.0",  # noqa: S104 -- container-internal listener
            "-p",
            "3000",
            "-w",
            "/opt/dagster/workspace.yaml",
        ),
        "dagster-daemon": (
            "dagster-daemon",
            "run",
            "-w",
            "/opt/dagster/workspace.yaml",
        ),
    }
    actual = tuple(strings(service.get("command", ()), owner=f"{name}.command"))
    if actual != expected[name]:
        fail(
            code="COMPOSE_DAGSTER_WORKSPACE_COMMAND_MISMATCH",
            message="A Dagster process is not explicitly loading the shipped workspace.",
            context={"service": name, "expected": list(expected[name]), "actual": list(actual)},
            required_action="Pass /opt/dagster/workspace.yaml to both Dagster commands.",
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
