from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_APPLICATION_SERVICES = {
    "object-store-bootstrap",
    "migration",
    "control-api",
    "worker-gateway",
    "manual-import-worker",
    "manual-record-worker",
    "http-worker",
    "osm-worker",
    "extraction-worker",
    "normalization-worker",
    "resolution-worker",
    "collector-cli",
}
_WORKERS = {
    "manual-import-worker",
    "manual-record-worker",
    "http-worker",
    "osm-worker",
    "extraction-worker",
    "normalization-worker",
    "resolution-worker",
}
_TOKEN_CONTRACT = {
    "manual-import-worker": ("MANUAL_IMPORT_WORKER_TOKEN", "manual_import"),
    "manual-record-worker": ("MANUAL_RECORD_WORKER_TOKEN", "manual_record"),
    "http-worker": ("HTTP_WORKER_TOKEN", "http_fetch"),
    "osm-worker": ("OSM_WORKER_TOKEN", "osm_query"),
    "extraction-worker": ("EXTRACTION_WORKER_TOKEN", "extraction"),
    "normalization-worker": ("NORMALIZATION_WORKER_TOKEN", "normalization"),
    "resolution-worker": ("RESOLUTION_WORKER_TOKEN", "entity_resolution"),
}
_ALL_INTERFACES = "0.0.0.0"  # noqa: S104 -- negative bind fixture
_LOOPBACK_OPTIONS = {
    "com.docker.network.bridge.enable_icc": "false",
    "com.docker.network.bridge.enable_ip_masquerade": "false",
    "com.docker.network.bridge.host_binding_ipv4": "127.0.0.1",
}


def _load_verifier() -> ModuleType:
    path = Path(__file__).parents[1] / "verify.py"
    spec = importlib.util.spec_from_file_location("compose_topology_verifier", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _environment() -> dict[str, str]:
    tokens = {
        variable: f"{service}-token-00000000000000000000000000000000"
        for service, (variable, _) in _TOKEN_CONTRACT.items()
    }
    credentials = [
        {
            "token": tokens[variable],
            "workerId": f"{service}-local",
            "capabilities": [capability],
        }
        for service, (variable, capability) in _TOKEN_CONTRACT.items()
    ]
    return {
        "CONTROL_API_PORT": "8081",
        "COLLECTOR_POSTGRES_PORT": "5432",
        "SEAWEEDFS_S3_PORT": "8333",
        "SEAWEEDFS_MASTER_PORT": "9333",
        "COLLECTOR_OBJECT_STORE_ACCESS_KEY_ID": "access-key",
        "COLLECTOR_OBJECT_STORE_SECRET_ACCESS_KEY": "secret-key",
        "WORKER_GATEWAY_CREDENTIALS_JSON": json.dumps(
            {
                "contract": "worker-gateway-local-credentials",
                "contractRevision": "worker-gateway-local-credentials-v1",
                "credentials": credentials,
            },
            separators=(",", ":"),
        ),
        **tokens,
    }


def _runtime_service(*, networks: set[str]) -> dict[str, Any]:
    return {
        "build": {"context": "."},
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "networks": dict.fromkeys(networks),
    }


def _payload(secret_root: Path, environment: dict[str, str]) -> dict[str, Any]:
    services: dict[str, dict[str, Any]] = {
        name: _runtime_service(networks=set()) for name in _APPLICATION_SERVICES
    }
    services["collector-postgres"] = {
        "image": "postgis/postgis:18-3.6@sha256:test",
        "networks": {
            "collection-infrastructure": None,
            "collection-postgres-loopback": None,
        },
        "ports": [
            {
                "host_ip": "127.0.0.1",
                "published": "5432",
                "target": 5432,
                "protocol": "tcp",
            }
        ],
    }
    services["seaweedfs"] = {
        "image": "chrislusf/seaweedfs:4.40",
        "networks": {
            "collection-infrastructure": None,
            "collection-object-store-loopback": None,
        },
        "ports": [
            {
                "host_ip": "127.0.0.1",
                "published": "8333",
                "target": 8333,
                "protocol": "tcp",
            },
            {
                "host_ip": "127.0.0.1",
                "published": "9333",
                "target": 9333,
                "protocol": "tcp",
            },
        ],
    }
    services["object-store-bootstrap"].update(
        {
            "profiles": ["bootstrap"],
            "networks": {"collection-infrastructure": None},
        }
    )
    services["migration"].update(
        {
            "profiles": ["migration"],
            "networks": {"collection-infrastructure": None},
        }
    )
    services["control-api"].update(
        {
            "networks": {
                "collection-infrastructure": None,
                "collection-operator": None,
                "collection-control-loopback": None,
            },
            "ports": [
                {
                    "host_ip": "127.0.0.1",
                    "published": "8081",
                    "target": 8080,
                    "protocol": "tcp",
                }
            ],
            "secrets": [
                {"source": "collector-object-store-access-key"},
                {"source": "collector-object-store-secret-key"},
            ],
        }
    )
    services["worker-gateway"].update(
        {
            "environment": {
                "WORKER_GATEWAY_BIND_MODE": "container",
                "WORKER_GATEWAY_HOST": _ALL_INTERFACES,
            },
            "networks": {
                "collection-infrastructure": None,
                "collection-workers": None,
            },
            "secrets": [
                {"source": "collector-object-store-access-key"},
                {"source": "collector-object-store-secret-key"},
                {"source": "worker-gateway-credentials"},
            ],
        }
    )
    services["manual-import-worker"].update(
        {
            "environment": {"MANUAL_WORKER_CAPABILITY": "manual_import"},
            "networks": {"collection-workers": None},
        }
    )
    services["manual-record-worker"].update(
        {
            "environment": {"MANUAL_WORKER_CAPABILITY": "manual_record"},
            "networks": {"collection-workers": None},
        }
    )
    for name in ("http-worker", "osm-worker"):
        services[name].update(
            {
                "environment": {},
                "networks": {
                    "collection-workers": None,
                    "collection-acquisition-egress": None,
                },
            }
        )
    for name in ("extraction-worker", "normalization-worker", "resolution-worker"):
        services[name].update({"environment": {}, "networks": {"collection-workers": None}})
    services["collector-cli"].update(
        {"profiles": ["tools"], "networks": {"collection-operator": None}}
    )

    secret_files = {
        "collector-object-store-access-key": (
            "COLLECTOR_OBJECT_STORE_ACCESS_KEY_ID",
            "collector-object-store-access-key",
        ),
        "collector-object-store-secret-key": (
            "COLLECTOR_OBJECT_STORE_SECRET_ACCESS_KEY",
            "collector-object-store-secret-key",
        ),
        "worker-gateway-credentials": (
            "WORKER_GATEWAY_CREDENTIALS_JSON",
            "worker-gateway-credentials",
        ),
    }
    secrets: dict[str, dict[str, str]] = {}
    for secret_name, (variable, filename) in secret_files.items():
        path = secret_root / filename
        path.write_text(environment[variable], encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o444)
        secrets[secret_name] = {"file": str(path)}
    if os.name != "nt":
        secret_root.chmod(0o700)

    return {
        "services": services,
        "networks": {
            "collection-infrastructure": {"driver": "bridge", "internal": True},
            "collection-operator": {"driver": "bridge", "internal": True},
            "collection-workers": {"driver": "bridge", "internal": True},
            "collection-acquisition-egress": {"driver": "bridge"},
            "collection-control-loopback": {
                "driver": "bridge",
                "driver_opts": dict(_LOOPBACK_OPTIONS),
            },
            "collection-postgres-loopback": {
                "driver": "bridge",
                "driver_opts": dict(_LOOPBACK_OPTIONS),
            },
            "collection-object-store-loopback": {
                "driver": "bridge",
                "driver_opts": dict(_LOOPBACK_OPTIONS),
            },
        },
        "secrets": secrets,
    }


def _case(tmp_path: Path) -> tuple[ModuleType, dict[str, str], Path, dict[str, Any]]:
    verifier = _load_verifier()
    environment = _environment()
    secret_root = tmp_path / ".secrets"
    secret_root.mkdir()
    payload = _payload(secret_root, environment)
    return verifier, environment, secret_root, payload


def test_valid_topology_passes(tmp_path: Path) -> None:
    verifier, environment, secret_root, payload = _case(tmp_path)

    verifier.verify_topology(payload, secret_root=secret_root, environment=environment)


def test_loopback_network_must_disable_ip_masquerade(tmp_path: Path) -> None:
    verifier, environment, secret_root, payload = _case(tmp_path)
    payload["networks"]["collection-control-loopback"]["driver_opts"][
        "com.docker.network.bridge.enable_ip_masquerade"
    ] = "true"

    with pytest.raises(verifier.ComposeTopologyError) as error:
        verifier.verify_topology(payload, secret_root=secret_root, environment=environment)

    assert error.value.code == "COMPOSE_LOOPBACK_NETWORK_OPTIONS_MISMATCH"
    assert error.value.context["network"] == "collection-control-loopback"


def test_worker_cannot_join_a_loopback_publishing_network(tmp_path: Path) -> None:
    verifier, environment, secret_root, payload = _case(tmp_path)
    payload["services"]["http-worker"]["networks"]["collection-control-loopback"] = None

    with pytest.raises(verifier.ComposeTopologyError) as error:
        verifier.verify_topology(payload, secret_root=secret_root, environment=environment)

    assert error.value.code == "COMPOSE_NETWORK_MEMBERSHIP_MISMATCH"
    assert error.value.context["network"] == "collection-control-loopback"


def test_published_port_must_bind_to_loopback(tmp_path: Path) -> None:
    verifier, environment, secret_root, payload = _case(tmp_path)
    payload["services"]["control-api"]["ports"][0]["host_ip"] = _ALL_INTERFACES

    with pytest.raises(verifier.ComposeTopologyError) as error:
        verifier.verify_topology(payload, secret_root=secret_root, environment=environment)

    assert error.value.code == "COMPOSE_PORT_INVENTORY_MISMATCH"
    assert error.value.context["service"] == "control-api"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode proof")
def test_mounted_secret_must_remain_read_only(tmp_path: Path) -> None:
    verifier, environment, secret_root, payload = _case(tmp_path)
    source = Path(payload["secrets"]["collector-object-store-access-key"]["file"])
    source.chmod(0o600)

    with pytest.raises(verifier.ComposeTopologyError) as error:
        verifier.verify_topology(payload, secret_root=secret_root, environment=environment)

    assert error.value.code == "COMPOSE_SECRET_SOURCE_MODE_MISMATCH"
    assert stat.S_IMODE(source.stat().st_mode) == 0o600


def test_manual_worker_must_declare_exact_process_capability(tmp_path: Path) -> None:
    verifier, environment, secret_root, payload = _case(tmp_path)
    payload["services"]["manual-record-worker"]["environment"]["MANUAL_WORKER_CAPABILITY"] = (
        "manual_import"
    )

    with pytest.raises(verifier.ComposeTopologyError) as error:
        verifier.verify_topology(payload, secret_root=secret_root, environment=environment)

    assert error.value.code == "COMPOSE_MANUAL_WORKER_CAPABILITY_MISMATCH"
    assert error.value.context["service"] == "manual-record-worker"
