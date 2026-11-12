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

`tests` excludes every integration marker. PostgreSQL, PostGIS, object storage, and container proof
need explicit infrastructure and are never part of `verify`.

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

# Runtime

Worker Gateway needs exact object-store configuration and mounted secret files:
`WORKER_GATEWAY_TOKEN_FILE`, `COLLECTOR_OBJECT_STORE_ENDPOINT`,
`COLLECTOR_OBJECT_STORE_BUCKET`, `COLLECTOR_OBJECT_STORE_ACCESS_KEY_FILE`,
`COLLECTOR_OBJECT_STORE_SECRET_KEY_FILE`, `COLLECTOR_OBJECT_STORE_REGION`.

Compose and image commands live in `deploy/compose/` and `deploy/docker/`.
