from __future__ import annotations

from collections.abc import Mapping

from tools.compose_topology.constants import (
    EXPECTED_NETWORK_MEMBERS,
    INTERNAL_NETWORKS,
    LOOPBACK_DRIVER_OPTIONS,
    LOOPBACK_NETWORKS,
)
from tools.compose_topology.support import (
    fail,
    mapping,
    network_failure,
    published_ports,
    required_port,
    service_networks,
)


def require_network_topology(
    services: Mapping[str, object], networks: Mapping[str, object]
) -> None:
    actual_networks = frozenset(networks)
    expected_networks = frozenset(EXPECTED_NETWORK_MEMBERS)
    if actual_networks != expected_networks:
        fail(
            code="COMPOSE_NETWORK_INVENTORY_MISMATCH",
            message="The Compose network inventory differs from its owner contract.",
            context={"expected": sorted(expected_networks), "actual": sorted(actual_networks)},
            required_action="Declare only the internal, egress, and dedicated loopback networks.",
        )

    actual_members: dict[str, set[str]] = {name: set() for name in networks}
    for service_name, raw_service in services.items():
        service = mapping(raw_service, owner=f"services.{service_name}")
        for network_name in service_networks(service):
            if network_name not in actual_members:
                fail(
                    code="COMPOSE_UNDECLARED_NETWORK_REFERENCE",
                    message="A service references an undeclared Compose network.",
                    context={"service": service_name, "network": network_name},
                    required_action="Declare the network in the merged Compose owner files.",
                )
            actual_members[network_name].add(service_name)

    for name, expected_members in EXPECTED_NETWORK_MEMBERS.items():
        actual = frozenset(actual_members[name])
        if actual != expected_members:
            fail(
                code="COMPOSE_NETWORK_MEMBERSHIP_MISMATCH",
                message="A Compose network contains an unexpected service set.",
                context={
                    "network": name,
                    "expected": sorted(expected_members),
                    "actual": sorted(actual),
                },
                required_action="Restore the exact owner and capability network membership.",
            )

    for name in sorted(INTERNAL_NETWORKS):
        network = mapping(networks[name], owner=f"networks.{name}")
        if network.get("internal") is not True:
            network_failure(name, "internal", True)

    acquisition = mapping(
        networks["collection-acquisition-egress"],
        owner="networks.collection-acquisition-egress",
    )
    if acquisition.get("internal") is True:
        network_failure("collection-acquisition-egress", "internal", False)

    for name in sorted(LOOPBACK_NETWORKS):
        network = mapping(networks[name], owner=f"networks.{name}")
        options = {
            str(key): str(value).lower()
            for key, value in mapping(
                network.get("driver_opts", {}), owner=f"networks.{name}.driver_opts"
            ).items()
        }
        if network.get("internal") is True or network.get("driver") != "bridge":
            fail(
                code="COMPOSE_LOOPBACK_NETWORK_CONTRACT_MISMATCH",
                message="A loopback publishing network has an invalid bridge contract.",
                context={
                    "network": name,
                    "driver": network.get("driver"),
                    "internal": network.get("internal"),
                },
                required_action="Use a non-internal dedicated bridge for loopback publication.",
            )
        if options != LOOPBACK_DRIVER_OPTIONS:
            fail(
                code="COMPOSE_LOOPBACK_NETWORK_OPTIONS_MISMATCH",
                message="A loopback publishing bridge has unsafe or incomplete driver options.",
                context={
                    "network": name,
                    "expected": LOOPBACK_DRIVER_OPTIONS,
                    "actual": options,
                },
                required_action=(
                    "Disable inter-container communication and IP masquerading, and bind to "
                    "127.0.0.1."
                ),
            )


def require_port_inventory(services: Mapping[str, object], environment: Mapping[str, str]) -> None:
    expected = {
        "control-api": {("127.0.0.1", required_port(environment, "CONTROL_API_PORT"), 8080, "tcp")},
        "collector-postgres": {
            (
                "127.0.0.1",
                required_port(environment, "COLLECTOR_POSTGRES_PORT"),
                5432,
                "tcp",
            )
        },
        "seaweedfs": {
            ("127.0.0.1", required_port(environment, "SEAWEEDFS_S3_PORT"), 8333, "tcp"),
            (
                "127.0.0.1",
                required_port(environment, "SEAWEEDFS_MASTER_PORT"),
                9333,
                "tcp",
            ),
        },
    }
    for name, raw_service in services.items():
        service = mapping(raw_service, owner=f"services.{name}")
        actual = published_ports(service, service_name=name)
        expected_ports = expected.get(name, set())
        if actual != expected_ports:
            fail(
                code="COMPOSE_PORT_INVENTORY_MISMATCH",
                message="A service's published port inventory differs from the loopback contract.",
                context={
                    "service": name,
                    "expected": sorted(expected_ports),
                    "actual": sorted(actual),
                },
                required_action="Publish only the exact owner-approved ports on 127.0.0.1.",
            )
