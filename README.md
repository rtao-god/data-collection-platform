# Data Collection Platform

Universal collection platform for discovering, acquiring, extracting, normalizing, resolving,
reviewing, and exporting evidence-backed data from external sources.

The first configured campaign targets recording studios, producers, engineers, and adjacent
recording services in Berlin. Berlin- and audio-specific meaning is kept in campaign data, not in
platform packages.

## Current implementation slice

The repository currently implements the foundation and the first database-backed contract slice:

- strict declarative campaign contracts;
- filesystem campaign-boundary validation;
- duplicate-key-safe YAML parsing and exact CSV validation;
- cross-document reference validation;
- deterministic canonical JSON and SHA-256 campaign snapshot identity;
- generated JSON Schemas with a checked-in digest manifest and drift proof;
- typed owner-context error envelopes;
- work-unit state-transition vocabulary;
- PostgreSQL/PostGIS migration ownership;
- insert-only config bundle metadata, component identities, and blockers;
- separate migration CLI and container image;
- import-boundary checks, tests, Git hooks, and CI.

This is not yet the complete platform. Runtime snapshot persistence remains blocked until the
content-addressed Object Store and upload-verification transaction exist. Collection runs, durable
leases, Worker Gateway, acquisition workers, entity resolution, review console, and sealed export
materialization remain later implementation stages. The Berlin boundary is intentionally not
fabricated; the campaign remains explicitly blocked until an approved polygon artifact is added.

## Requirements

- Python 3.13
- `uv`
- Git with `core.hooksPath=.githooks`
- PostgreSQL 18 with PostGIS for integration verification
- Docker for image and service verification

## Bootstrap

```text
uv sync --all-packages --dev
python tools/git_hooks/configure.py
```

## Commands

```text
uv run collector config validate berlin_recording_services
uv run collector config digest berlin_recording_services
uv run python tools/contract_generation/generate.py --check
uv run pytest -m "not integration"
uv run python tools/architecture_checks/check_dependencies.py
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

Database migration is an explicit state-changing operation:

```text
COLLECTOR_DATABASE_URL=<runtime secret> uv run collection-migrate upgrade head
COLLECTOR_DATABASE_URL=<runtime secret> uv run pytest -m integration database/tests
```

The collector CLI only reads the allowlisted campaign directory. It does not create a run, write
runtime state, or perform network acquisition. The migration CLI changes only the Collection
database schema through checked-in Alembic revisions.

## Repository boundary

This repository owns collector state and collector exports. It must not write to a future catalog
database or define public listing, ranking, billing, booking, or SEO meaning. See
`docs/architecture/system-boundary.md` and `docs/implementation-status.md`.
