from __future__ import annotations

EXPECTED_SERVICES = frozenset(
    {
        "collector-postgres",
        "seaweedfs",
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
)
INFRASTRUCTURE_SERVICES = frozenset({"collector-postgres", "seaweedfs"})
APPLICATION_SERVICES = EXPECTED_SERVICES - INFRASTRUCTURE_SERVICES
WORKERS = frozenset(
    {
        "manual-import-worker",
        "manual-record-worker",
        "http-worker",
        "osm-worker",
        "extraction-worker",
        "normalization-worker",
        "resolution-worker",
    }
)
EGRESS_WORKERS = frozenset({"http-worker", "osm-worker"})
EXPECTED_NETWORK_MEMBERS = {
    "collection-infrastructure": frozenset(
        {
            "collector-postgres",
            "seaweedfs",
            "object-store-bootstrap",
            "migration",
            "control-api",
            "worker-gateway",
        }
    ),
    "collection-operator": frozenset({"control-api", "collector-cli"}),
    "collection-workers": frozenset({"worker-gateway", *WORKERS}),
    "collection-acquisition-egress": EGRESS_WORKERS,
    "collection-control-loopback": frozenset({"control-api"}),
    "collection-postgres-loopback": frozenset({"collector-postgres"}),
    "collection-object-store-loopback": frozenset({"seaweedfs"}),
}
INTERNAL_NETWORKS = frozenset(
    {"collection-infrastructure", "collection-operator", "collection-workers"}
)
LOOPBACK_NETWORKS = frozenset(
    {
        "collection-control-loopback",
        "collection-postgres-loopback",
        "collection-object-store-loopback",
    }
)
LOOPBACK_DRIVER_OPTIONS = {
    "com.docker.network.bridge.enable_icc": "false",
    "com.docker.network.bridge.enable_ip_masquerade": "false",
    "com.docker.network.bridge.host_binding_ipv4": "127.0.0.1",
}
EXPECTED_PROFILES = {
    "object-store-bootstrap": frozenset({"bootstrap"}),
    "migration": frozenset({"migration"}),
    "collector-cli": frozenset({"tools"}),
}
EXPECTED_SECRETS = {
    "collector-object-store-access-key": "COLLECTOR_OBJECT_STORE_ACCESS_KEY_ID",
    "collector-object-store-secret-key": "COLLECTOR_OBJECT_STORE_SECRET_ACCESS_KEY",
    "worker-gateway-credentials": "WORKER_GATEWAY_CREDENTIALS_JSON",
}
OBJECT_STORE_SECRETS = frozenset(
    {"collector-object-store-access-key", "collector-object-store-secret-key"}
)
TOKEN_CONTRACT = {
    "manual-import-worker": ("MANUAL_IMPORT_WORKER_TOKEN", "manual_import"),
    "manual-record-worker": ("MANUAL_RECORD_WORKER_TOKEN", "manual_record"),
    "http-worker": ("HTTP_WORKER_TOKEN", "http_fetch"),
    "osm-worker": ("OSM_WORKER_TOKEN", "osm_query"),
    "extraction-worker": ("EXTRACTION_WORKER_TOKEN", "extraction"),
    "normalization-worker": ("NORMALIZATION_WORKER_TOKEN", "normalization"),
    "resolution-worker": ("RESOLUTION_WORKER_TOKEN", "entity_resolution"),
}
MANUAL_WORKER_CAPABILITIES = {
    "manual-import-worker": "manual_import",
    "manual-record-worker": "manual_record",
}
FORBIDDEN_WORKER_ENVIRONMENT = (
    "COLLECTOR_DATABASE_URL",
    "COLLECTOR_POSTGRES_",
    "COLLECTOR_OBJECT_STORE_",
    "ARTIFACT_S3_",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)
