# Common commands

Read before choosing repository proof commands. Generic test-execution rules are owned by the shared
agent rules; this file holds project-specific entrypoints and exceptional commands only.

`tools/check.ps1` is the only command entrypoint. Every command runs from the repository root and
stops at the first failing step, returning that step's exit code.

# Verification

```powershell
pwsh -NoProfile -File D:\tools\utils\business-module-scanner\maintenance\Invoke-ProjectCommand.ps1 `
    D:\Aggregator\data-collection-platform pwsh -NoProfile -File tools/check.ps1 verify
```

`verify` is the default before broad work and before reporting a change complete. It runs, in order:
generated-contract drift, formatting, lint, strict typing, architecture boundaries, non-integration
tests, and first campaign snapshot validation.

# Scoped

```powershell
pwsh -NoProfile -File tools/check.ps1 contracts
pwsh -NoProfile -File tools/check.ps1 format
pwsh -NoProfile -File tools/check.ps1 lint
pwsh -NoProfile -File tools/check.ps1 types
pwsh -NoProfile -File tools/check.ps1 architecture
pwsh -NoProfile -File tools/check.ps1 tests
pwsh -NoProfile -File tools/check.ps1 campaign
```

Use a scoped command during a single-owner edit; `verify` is the task-stage gate, not a post-edit tax.

Scoped test runs select by path, node id, `-k`, or marker: `uv run pytest packages/collection_domain`
runs one owner's suite in well under a second. `tools/check.ps1 tests` is the whole non-integration
suite and only belongs at the task-stage gate.

`tests` excludes every integration marker. PostgreSQL, PostGIS, object storage, and container proof
need explicit infrastructure and are never part of `verify`.

# Backend submission

```powershell
uv run collector export --campaign-key <campaign> --run-id <run-id> `
    --candidate-revision <candidate-id>:<revision> --correlation-id <correlation-id>
uv run collector ingestion submit --export-path <sealed package path> --campaign-key <campaign> `
    --run-id <run-id> --candidate-revision <candidate-id>:<revision> `
    --target-site-key <site> --target-catalog-key <catalog> `
    --target-configuration-revision-id <active Catalog revision> `
    --producer-identity <producer> --producer-build <build> `
    --api-base-url <ingestion base url> --token-endpoint <token url> --client-id <client> `
    --correlation-id <correlation-id>
```

`submit` отправляет ровно подписанные байты экспорта и печатает `batchId`, `manifestDigest`
и replay-флаги. Workload-секрет и токены берутся только из окружения
(`COLLECTOR_INGESTION_CLIENT_SECRET`), никогда из аргументов и файлов. Дальнейшие шаги
(Ingestion → Catalog), replay и утверждения по сохранённым строкам Catalog:
`D:\Aggregator\aggregator-backend\docs\runbooks\ingestion-catalog-acceptance.md`.

Экспорт читает **все** сохранённые строки кандидатов, поэтому приёмку запускают на
отдельной мигрированной базе: остатки интеграционных прогонов блокируют экспорт, а не
пропускаются.

# Campaign authoring

```powershell
uv run collector config validate berlin_recording_services
uv run collector config digest berlin_recording_services
```

# Integration and infrastructure

These need a running PostgreSQL/PostGIS or SeaweedFS and are never run by `verify`:

```powershell
COLLECTOR_DATABASE_URL=<runtime secret> uv run pytest -m integration database/tests
COLLECTOR_DATABASE_URL=<runtime secret> uv run collection-migrate upgrade head
```

Migration is an explicit state-changing operation. No API, worker, readiness check, or image startup
runs it.

Integration tests are repeatable against one migrated database: every fixture derives per-run unique
identities, so rerunning the suite must not need a fresh database. CI runs the database contract twice
against the same instance to keep that property enforced.

The disposable integration database is started, migrated and removed by its own owner script:
`pwsh -NoProfile -File tools/integration-db.ps1 up|test|down`. It keeps a separate Compose project,
volume-less tmpfs data and its own migration run. `up` treats the database as ready only when the
application database answers a TCP query: the image's init phase keeps a temporary server on the unix
socket only, and a socket-only readiness query used to pass while that server was already shutting
down, so migrations failed with an `OperationalError` instead of retrying.

# Runtime

Worker Gateway needs exact object-store configuration and mounted secret files:
`WORKER_GATEWAY_TOKEN_FILE`, `COLLECTOR_OBJECT_STORE_ENDPOINT`,
`COLLECTOR_OBJECT_STORE_BUCKET`, `COLLECTOR_OBJECT_STORE_ACCESS_KEY_FILE`,
`COLLECTOR_OBJECT_STORE_SECRET_KEY_FILE`, `COLLECTOR_OBJECT_STORE_REGION`.

Compose and image commands live in `deploy/compose/` and `deploy/docker/`.
