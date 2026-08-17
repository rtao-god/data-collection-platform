# Application Compose

`deploy/compose/infrastructure.yaml` and `deploy/compose/application.yaml` form one local runtime contract. The infrastructure file owns PostgreSQL/PostGIS, SeaweedFS, durable volumes, and the isolated infrastructure network. The application file owns process images, credentials, network reachability, resource limits, and one-shot operational commands.

## Clean local start

Create an ignored local environment file from the example and replace every local credential before use:

```text
cp deploy/compose/.env.example deploy/compose/.env.local
```

Materialize the values owned by that environment file into the ignored, file-backed secret directory before any Compose command:

```text
python tools/compose_secrets/materialize.py --environment-file deploy/compose/.env.local --output-directory deploy/compose/.secrets
```

Run the same command again after rotating any object-store key or Worker Gateway credential. On POSIX hosts the materializer keeps `deploy/compose/.secrets` owner-only (`0700`) and writes each mounted file read-only (`0444`). Docker Compose bind-mounts local secret files without remapping their host UID, so the read bit is required by the fixed non-root container UID. Access remains restricted by the owner-only host directory and by the exact per-service secret mounts.

Use both Compose files for every command:

```text
docker compose \
  --env-file deploy/compose/.env.local \
  -f deploy/compose/infrastructure.yaml \
  -f deploy/compose/application.yaml \
  up --detach --wait collector-postgres seaweedfs
```

Create the object-store bucket through its explicit owner command:

```text
docker compose \
  --env-file deploy/compose/.env.local \
  -f deploy/compose/infrastructure.yaml \
  -f deploy/compose/application.yaml \
  --profile bootstrap run --rm --no-deps object-store-bootstrap
```

Apply database migrations through the one-shot migration image:

```text
docker compose \
  --env-file deploy/compose/.env.local \
  -f deploy/compose/infrastructure.yaml \
  -f deploy/compose/application.yaml \
  --profile migration run --rm --no-deps migration
```

Start the APIs and current capability workers only after bootstrap and migration complete:

```text
docker compose \
  --env-file deploy/compose/.env.local \
  -f deploy/compose/infrastructure.yaml \
  -f deploy/compose/application.yaml \
  up --detach --wait \
    control-api \
    worker-gateway \
    manual-import-worker \
    http-worker \
    osm-worker \
    extraction-worker \
    normalization-worker \
    resolution-worker
```

The Control API is published only through `127.0.0.1:${CONTROL_API_PORT}`. PostgreSQL and SeaweedFS development ports are also loopback-only. Worker Gateway and capability workers have no host ports.

## Credential and network boundaries

| Process | Collection DB | Object Store credentials | External egress | Host port |
|---|---:|---:|---:|---:|
| `control-api` | yes | scoped | no Docker NAT egress | loopback only |
| `worker-gateway` | yes | scoped | no | no |
| `manual-import-worker` | no | no | no | no |
| `http-worker` | no | no | approved HTTP egress | no |
| `osm-worker` | no | no | approved Overpass egress | no |
| `extraction-worker` | no | no | no | no |
| `normalization-worker` | no | no | no | no |
| `resolution-worker` | no | no | no | no |

Worker Gateway is the only bridge between the internal worker network and the infrastructure network. Its default executable contract remains loopback-only; the Compose service must opt into `WORKER_GATEWAY_BIND_MODE=container` and the exact `0.0.0.0` container bind. A non-local bind outside that explicit mode fails before runtime composition.

The acquisition egress network is attached only to `http-worker` and `osm-worker`. Processing workers cannot reach PostgreSQL, SeaweedFS, or the external network. Object-store keys and the Worker Gateway credential document are mounted only into their owning trusted processes through Compose secrets.

Docker does not publish host ports for services that are attached only to an `internal` bridge. The local runtime therefore gives each host-visible service one dedicated loopback-publishing bridge:

- `collection-control-loopback` contains only `control-api`;
- `collection-postgres-loopback` contains only `collector-postgres`;
- `collection-object-store-loopback` contains only `seaweedfs`.

Each bridge binds published ports to `127.0.0.1`, disables inter-container communication, and disables Docker IP masquerading. The service retains its separate internal owner network for application traffic. The loopback bridge exists only to carry the exact host-published port; it does not become a shared service network or a general external-egress path.

`tools/compose_topology/verify.py` is the static owner of this topology proof. It validates the exact service, network, port, secret, profile, credential, and image inventory from resolved Compose JSON. The runtime workflow separately inspects the created Docker networks, proves one expected container per loopback bridge, verifies the exact host bindings, and opens each published loopback port.

## Process hardening

Application containers use:

- a non-root user defined by the image;
- a read-only root filesystem;
- dropped Linux capabilities;
- `no-new-privileges`;
- bounded PID, memory, and CPU limits;
- an explicit writable `/tmp` tmpfs;
- one executable process per container.

Migration, object-store bootstrap, and Collector CLI are profile-scoped one-shot commands. API startup does not run migrations or create storage infrastructure.

## Current owner inventory

The application topology declares only production processes that currently have a real package, executable composition root, and independently buildable image. Browser acquisition, review frontend, retention, orchestration, export, and backup/restore are not represented by placeholder services; each must enter this topology together with its actual production owner and proof.

## Readiness and shutdown

Readiness checks are read-only:

```text
curl --fail http://127.0.0.1:${CONTROL_API_PORT}/health/ready
```

Stop the complete local runtime and remove its local data volumes with:

```text
docker compose \
  --env-file deploy/compose/.env.local \
  -f deploy/compose/infrastructure.yaml \
  -f deploy/compose/application.yaml \
  down --volumes --remove-orphans
```

`.github/workflows/application-compose-ci.yml` renders the merged topology, verifies the credential and network matrix, builds every application image, starts a clean PostgreSQL and Object Store, executes bootstrap and migration, starts the APIs and six workers, proves the exact loopback publication, and verifies the capability-scoped worker registrations in PostgreSQL.
