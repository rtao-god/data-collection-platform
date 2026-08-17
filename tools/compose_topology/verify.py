from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from tools.compose_topology.network_contracts import (  # noqa: E402
    require_network_topology,
    require_port_inventory,
)
from tools.compose_topology.runtime_contracts import (  # noqa: E402
    require_gateway_boundary,
    require_process_hardening,
    require_profiles_and_images,
    require_service_inventory,
    require_worker_boundaries,
)
from tools.compose_topology.secret_contracts import (  # noqa: E402
    require_secret_inventory,
    require_worker_credentials,
)
from tools.compose_topology.support import ComposeTopologyError, mapping  # noqa: E402


def verify_topology(
    payload: Mapping[str, object],
    *,
    secret_root: Path,
    environment: Mapping[str, str],
) -> None:
    services = mapping(payload.get("services"), owner="services")
    networks = mapping(payload.get("networks"), owner="networks")
    require_service_inventory(services)
    require_process_hardening(services)
    require_worker_boundaries(services)
    require_gateway_boundary(services)
    require_network_topology(services, networks)
    require_port_inventory(services, environment)
    require_profiles_and_images(services)
    require_secret_inventory(payload, services, secret_root, environment)
    require_worker_credentials(environment)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verify-application-compose-topology")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--secret-root", required=True, type=Path)
    return parser


def run(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    correlation_id = str(uuid4())
    try:
        decoded: object = json.loads(args.config.read_text(encoding="utf-8"))
        payload = mapping(decoded, owner="config")
        verify_topology(
            payload,
            secret_root=args.secret_root,
            environment=os.environ if environment is None else environment,
        )
    except (OSError, json.JSONDecodeError) as exc:
        error = ComposeTopologyError(
            code="COMPOSE_CONFIG_UNAVAILABLE",
            message="The resolved Compose configuration could not be read.",
            context={"config": str(args.config), "causeType": type(exc).__name__},
            required_action="Render a valid merged Compose JSON configuration and retry.",
        )
    except ComposeTopologyError as exc:
        error = exc
    else:
        print(
            json.dumps(
                {
                    "owner": "ApplicationComposeTopology",
                    "status": "verified",
                    "correlationId": correlation_id,
                },
                sort_keys=True,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "type": "collection/application-compose-topology-invalid",
                "owner": "ApplicationComposeTopology",
                "code": error.code,
                "message": error.message,
                "context": dict(error.context),
                "requiredAction": error.required_action,
                "correlationId": correlation_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
