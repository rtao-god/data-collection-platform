# Docker (local runtime and integration database)

Docker is not needed for `verify`, `tests`, linters, typing, or architecture checks. It owns exactly
two things: the full application runtime and the disposable integration database.

## Runtime compose

`deploy/compose/infrastructure.yaml` + `deploy/compose/application.yaml` form one contract: 15
services, of which 3 are infrastructure (collector-postgres, dagster-postgres, seaweedfs) and 12 are
application processes. Declared `mem_limit` total is ~7.2 GB and 14 CPUs; the local Docker VM is
limited to 6 GB, so never start the whole file.

Start only the processes you are working on, with the documented order (bootstrap and migration are
profile-scoped one-shot commands, not part of a plain `up`):

```text
docker compose --env-file deploy/compose/.env.local \
  -f deploy/compose/infrastructure.yaml -f deploy/compose/application.yaml \
  up --detach --wait collector-postgres seaweedfs control-api worker-gateway http-worker
```

- Never include `dagster-webserver`/`dagster-daemon` in a routine start: they cost 768 MB and
  1.5 CPU each and are only needed for orchestration work.
- `control-api` (127.0.0.1:8081), `collector-postgres` (127.0.0.1:5432) and `seaweedfs`
  (127.0.0.1:8333/9333) are the only host-visible ports.
- Stop with `down`; `--volumes` deletes development data and belongs to an explicit operator action.

`tools/compose_topology/verify.py` is the static owner of the topology (services, networks, ports,
secrets, profiles, images). Do not change compose topology to save memory locally.

## Native processes instead of containers

`control_api` and `worker_gateway` are FastAPI apps and run natively against an explicit database:

```text
uv run uvicorn control_api.main:app --host 127.0.0.1 --port 8081
uv run python -m worker_gateway
```

They still need `COLLECTOR_DATABASE_URL` and the object-store endpoint; Docker only provides those
services.

## Integration database

`pwsh -NoProfile -File tools/integration-db.ps1 up -PostgresPort 55436` starts one throwaway
PostGIS container (`dcpf-integration`, tmpfs data) and never touches `infrastructure.yaml`. The
default port 55432 collides with SmartFlowTerminal and CryptoMarketData, so pass a free port when
those stacks may run.
