# Local development

## Bootstrap

```text
uv sync --all-packages --dev
python tools/git_hooks/configure.py
```

## Narrow verification

```text
uv run collector config validate berlin_recording_services
uv run python tools/contract_generation/generate.py --check
uv run pytest -m "not integration" packages/collection_application/tests packages/collection_infrastructure/tests apps/worker_gateway/tests
uv run python tools/architecture_checks/check_dependencies.py
```

## Database migration and integration

```text
COLLECTOR_DATABASE_URL=<runtime secret> uv run collection-migrate upgrade head
COLLECTOR_DATABASE_URL=<runtime secret> uv run pytest -m integration database/tests
```

The database must be an explicit PostgreSQL/PostGIS test instance. No command creates a hidden local
SQLite fallback.

Artifact integration additionally requires an S3-compatible test endpoint and private bucket. The
Worker Gateway executable reads account credentials only from mounted files:

```text
COLLECTOR_OBJECT_STORE_ENDPOINT=<endpoint>
COLLECTOR_OBJECT_STORE_BUCKET=<bucket>
COLLECTOR_OBJECT_STORE_ACCESS_KEY_FILE=<mounted file>
COLLECTOR_OBJECT_STORE_SECRET_KEY_FILE=<mounted file>
COLLECTOR_OBJECT_STORE_REGION=<region>
```

Worker containers must not receive those account credentials or `COLLECTOR_DATABASE_URL`; they use
Worker Gateway and short-lived operation-scoped URLs.

## Full current-slice verification

```text
uv lock --check
uv run python tools/contract_generation/generate.py --check
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -m "not integration"
uv run python tools/architecture_checks/check_dependencies.py
uv run python -m compileall -q apps database packages tools
```

No command in the current slice performs external-source acquisition. Object transfer is available
only for already leased work through the authenticated Worker Gateway contract.
