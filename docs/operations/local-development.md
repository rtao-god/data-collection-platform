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
uv run pytest -m "not integration" packages/collection_application/tests packages/collection_infrastructure/tests
uv run python tools/architecture_checks/check_dependencies.py
```

## Database migration

```text
COLLECTOR_DATABASE_URL=<runtime secret> uv run collection-migrate upgrade head
COLLECTOR_DATABASE_URL=<runtime secret> uv run pytest -m integration database/tests
```

The database must be an explicit PostgreSQL/PostGIS test instance. No command creates a hidden local
SQLite fallback.

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

No command in this slice performs network acquisition, creates a collection run, or writes an
object-store artifact.
