# Application Compose

`deploy/compose/infrastructure.yaml` and `deploy/compose/application.yaml` form one local runtime contract. The infrastructure file owns PostgreSQL/PostGIS, SeaweedFS, durable volumes, and the isolated infrastructure network. The application file owns process images, credentials, network reachability, resource limits, and one-shot operational commands.

## Clean local start

Create an ignored local environment file from the example and replace every local credential before use:

```text
cp deploy/compose/.env.example deploy/compose/.env.local
```

Materialize the values owned by that environment file into the ignored, file-backed secret
directory before any Compose command:

```text
python tools/compose_secrets/materialize.py --environment-file deploy/compose/.env.local --output-directory deploy/compose/.secrets
```

Run the same command again after rotating any object-store key or Worker Gateway credential. On
POSIX hosts the materializer keeps `deploy/compose/.secrets` owner-only (`0700`) and writes each
mounted file read-only (`0444`). Docker Compose bind-mounts local secret files without remapping
their host UID, so the read bit is required by the fixed non-root container UID. Access remains
restricted by the owner-only host directory and by the exact per-service secret mounts.

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

The Control API is exposed only through `127.0.0.1:${CONTROL_API_PORT}`. PostgreSQL and SeaweedFS development ports are also loopback-only. The Worker Gateway has no host port.

## Credential and network boundaries

| Process | Collection DB | Object Store credentials | External egress | Host port |
|---|---:|---:|---:|---:|
| `control-api` | yes | scoped | no | loopback only |
| `worker-gateway` | yes | scoped | no | no |
| `manual-import-worker` | no | no | no | no |
| `http-worker` | no | no | approved HTTP egress | no |
| `osm-worker` | no | no | approved Overpass egress | no |
| `extraction-worker` | no | no | no | no |
| `normalization-worker` | no | no | no | no |
| `resolution-worker` | no | no | no | no |

The Worker Gateway is the only bridge between the internal worker network and the infrastructure network. Its default executable contract remains loopback-only; the Compose service must opt into `WORKER_GATEWAY_BIND_MODE=container` and the exact `0.0.0.0` container bind. A non-local bind outside that explicit mode fails before runtime composition.

The acquisition egress network is attached only to `http-worker` and `osm-worker`. Processing workers cannot reach PostgreSQL, SeaweedFS, or the external network. Object-store keys and the Worker Gateway credential document are mounted only into their owning trusted processes through Compose secrets.

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

`.github/workflows/application-compose-ci.yml` renders the merged topology, verifies the credential and network matrix, builds every application image, starts a clean PostgreSQL and Object Store, executes bootstrap and migration, starts the APIs and six workers, and proves the exact capability-scoped worker registrations in PostgreSQL.
