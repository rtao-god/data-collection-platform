from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from tools.compose_topology.constants import (
    EXPECTED_SECRETS,
    EXPECTED_SERVICES,
    OBJECT_STORE_SECRETS,
    TOKEN_CONTRACT,
)
from tools.compose_topology.support import (
    fail,
    mapping,
    required_environment,
    resolve_secret_source,
    sequence,
    service_secrets,
    strings,
)


def require_secret_inventory(
    payload: Mapping[str, object],
    services: Mapping[str, object],
    secret_root: Path,
    environment: Mapping[str, str],
) -> None:
    secrets = mapping(payload.get("secrets", {}), owner="secrets")
    if frozenset(secrets) != frozenset(EXPECTED_SECRETS):
        fail(
            code="COMPOSE_SECRET_INVENTORY_MISMATCH",
            message="The Compose secret inventory differs from its owner contract.",
            context={"expected": sorted(EXPECTED_SECRETS), "actual": sorted(secrets)},
            required_action="Mount only the object-store and Worker Gateway credential owners.",
        )

    root = secret_root.resolve()
    if not root.is_dir() or secret_root.is_symlink():
        fail(
            code="COMPOSE_SECRET_ROOT_UNSAFE",
            message="The materialized Compose secret root is unavailable or unsafe.",
            context={"secretRoot": str(secret_root)},
            required_action="Materialize secrets into a project-owned non-symlink directory.",
        )
    if os.name != "nt" and stat.S_IMODE(root.stat().st_mode) != 0o700:
        fail(
            code="COMPOSE_SECRET_ROOT_MODE_MISMATCH",
            message="The materialized Compose secret root is not owner-only.",
            context={"secretRoot": str(root), "mode": oct(stat.S_IMODE(root.stat().st_mode))},
            required_action="Restrict the materialized secret root to mode 0700.",
        )

    for secret_name, source_variable in EXPECTED_SECRETS.items():
        secret = mapping(secrets[secret_name], owner=f"secrets.{secret_name}")
        if secret.get("environment") is not None:
            fail(
                code="COMPOSE_ENVIRONMENT_BACKED_SECRET_FORBIDDEN",
                message="A Compose secret is backed directly by an environment value.",
                context={"secret": secret_name},
                required_action="Materialize the value into the owned file-backed secret path.",
            )
        source = resolve_secret_source(secret.get("file"), root)
        if source.parent != root or not source.is_file() or source.is_symlink():
            fail(
                code="COMPOSE_SECRET_SOURCE_UNSAFE",
                message="A Compose secret source escaped the owned root or is unsafe.",
                context={"secret": secret_name, "source": str(source)},
                required_action="Use one regular file directly under the owned secret root.",
            )
        if hasattr(source.stat(), "st_uid") and source.stat().st_uid != root.stat().st_uid:
            fail(
                code="COMPOSE_SECRET_SOURCE_OWNER_MISMATCH",
                message="A Compose secret source has a different host owner.",
                context={"secret": secret_name, "source": str(source)},
                required_action="Re-materialize the file through the Compose secret owner tool.",
            )
        if os.name != "nt" and stat.S_IMODE(source.stat().st_mode) != 0o444:
            fail(
                code="COMPOSE_SECRET_SOURCE_MODE_MISMATCH",
                message="A mounted secret is not read-only for the fixed non-root container UID.",
                context={
                    "secret": secret_name,
                    "source": str(source),
                    "mode": oct(stat.S_IMODE(source.stat().st_mode)),
                },
                required_action="Re-materialize the mounted secret with mode 0444.",
            )
        expected_value = required_environment(environment, source_variable)
        if source.read_text(encoding="utf-8") != expected_value:
            fail(
                code="COMPOSE_SECRET_SOURCE_VALUE_MISMATCH",
                message="A materialized secret differs from its declared local owner value.",
                context={"secret": secret_name, "sourceVariable": source_variable},
                required_action="Rotate and materialize the exact current environment value.",
            )

    expected_mounts = {name: frozenset() for name in EXPECTED_SERVICES}
    expected_mounts["control-api"] = OBJECT_STORE_SECRETS
    expected_mounts["worker-gateway"] = OBJECT_STORE_SECRETS | {"worker-gateway-credentials"}
    for name, raw_service in services.items():
        actual = service_secrets(mapping(raw_service, owner=f"services.{name}"))
        if actual != expected_mounts[name]:
            fail(
                code="COMPOSE_SECRET_SCOPE_MISMATCH",
                message="A service's mounted secret scope differs from its owner contract.",
                context={
                    "service": name,
                    "expected": sorted(expected_mounts[name]),
                    "actual": sorted(actual),
                },
                required_action="Mount each secret only into its exact trusted owner process.",
            )


def require_worker_credentials(environment: Mapping[str, str]) -> None:
    tokens = {
        service: required_environment(environment, variable)
        for service, (variable, _) in TOKEN_CONTRACT.items()
    }
    if len(set(tokens.values())) != len(tokens):
        fail(
            code="COMPOSE_WORKER_TOKEN_REUSED",
            message="Worker credentials are not unique per capability owner.",
            context={"services": sorted(tokens)},
            required_action="Allocate one distinct credential per capability worker.",
        )
    short = sorted(service for service, token in tokens.items() if len(token) < 32)
    if short:
        fail(
            code="COMPOSE_WORKER_TOKEN_TOO_SHORT",
            message="One or more worker credentials are shorter than the local owner minimum.",
            context={"services": short, "minimumCharacters": 32},
            required_action="Rotate each listed credential to at least 32 characters.",
        )

    raw_document = required_environment(environment, "WORKER_GATEWAY_CREDENTIALS_JSON")
    try:
        decoded: object = json.loads(raw_document)
    except json.JSONDecodeError as exc:
        fail(
            code="COMPOSE_GATEWAY_CREDENTIAL_DOCUMENT_INVALID",
            message="The Worker Gateway credential document is not valid JSON.",
            context={"causeType": type(exc).__name__},
            required_action="Publish the exact typed local credential document.",
        )
    document = mapping(decoded, owner="worker-gateway-credentials")
    if (
        document.get("contract") != "worker-gateway-local-credentials"
        or document.get("contractRevision") != "worker-gateway-local-credentials-v1"
    ):
        fail(
            code="COMPOSE_GATEWAY_CREDENTIAL_IDENTITY_MISMATCH",
            message="The Worker Gateway credential document identity is incompatible.",
            context={
                "contract": document.get("contract"),
                "contractRevision": document.get("contractRevision"),
            },
            required_action="Restore the exact current local credential contract identity.",
        )

    actual: dict[str, tuple[str, tuple[str, ...]]] = {}
    for position, raw_item in enumerate(
        sequence(document.get("credentials"), owner="worker-gateway-credentials.credentials")
    ):
        item = mapping(raw_item, owner=f"worker-gateway-credentials.credentials[{position}]")
        token = item.get("token")
        worker_id = item.get("workerId")
        if not isinstance(token, str) or not isinstance(worker_id, str):
            fail(
                code="COMPOSE_GATEWAY_CREDENTIAL_DOCUMENT_INVALID",
                message="A Worker Gateway credential entry violates its typed shape.",
                context={"position": position},
                required_action="Publish the exact capability credential entries.",
            )
        if token in actual:
            fail(
                code="COMPOSE_GATEWAY_CREDENTIAL_TOKEN_DUPLICATED",
                message="A Worker Gateway credential token is declared more than once.",
                context={"position": position},
                required_action="Keep one exact entry for each capability token.",
            )
        actual[token] = (
            worker_id,
            tuple(
                strings(
                    item.get("capabilities"),
                    owner=f"worker-gateway-credentials.credentials[{position}].capabilities",
                )
            ),
        )

    expected = {
        tokens[service]: (f"{service}-local", (capability,))
        for service, (_, capability) in TOKEN_CONTRACT.items()
    }
    if actual != expected:
        fail(
            code="COMPOSE_GATEWAY_CREDENTIAL_SCOPE_MISMATCH",
            message="Worker Gateway credentials do not match the declared capability services.",
            context={
                "expectedWorkers": sorted(value[0] for value in expected.values()),
                "actualWorkers": sorted(value[0] for value in actual.values()),
            },
            required_action="Align every token, worker identity, and capability exactly.",
        )
