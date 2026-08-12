# Data Collection Platform

Universal collection platform for discovering, acquiring, extracting, normalizing, resolving,
reviewing, and exporting evidence-backed data from external sources.

The first configured campaign targets recording studios, producers, engineers, and adjacent
recording services in Berlin. Berlin- and audio-specific meaning is kept in campaign data, not in
platform packages.

## Current implementation slice

The repository currently implements the foundation, immutable campaign configuration, and the
runtime Work Engine boundary:

- strict declarative campaign contracts and CSV/JSON/JSONL manual input validation;
- deterministic canonical JSON, component digests, and campaign snapshot identity;
- transactional publication of immutable campaign snapshots;
- generated JSON Schemas and Worker Gateway OpenAPI with checked-in drift proof;
- durable runs, stages, semantic work units, attempts, leases, retries, dead letters, and source permits;
- authenticated Worker Gateway registration, claim, heartbeat, completion, failure, and release;
- lease-scoped pre-signed artifact upload/read contracts;
- streamed size, MIME, metadata, and SHA-256 verification before content-addressed promotion;
- ordered role-bound artifact inputs and outputs;
- one PostgreSQL transaction for verified artifact metadata and work completion;
- separate migration and Worker Gateway images;
- fail-closed architecture, contract, unit, PostgreSQL/PostGIS, and concurrency checks.

This is not yet the complete platform. SeaweedFS Compose compatibility, orphan/retention ownership,
manual-file artifact ingestion, source connectors, acquisition workers, processing, entity resolution,
review, and sealed exports remain later stages. The Berlin boundary is intentionally not fabricated;
the campaign remains explicitly blocked until an approved polygon artifact is added.

## Requirements

- Python 3.13
- `uv`
- Git with `core.hooksPath=.githooks`
- PostgreSQL 18 with PostGIS for integration verification
- an S3-compatible private bucket for Worker Gateway artifact operations
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

Worker Gateway additionally requires exact object-store configuration and mounted secret files:

```text
COLLECTOR_DATABASE_URL=<runtime secret>
WORKER_GATEWAY_TOKEN_FILE=<mounted worker-token document>
COLLECTOR_OBJECT_STORE_ENDPOINT=<http-or-https endpoint>
COLLECTOR_OBJECT_STORE_BUCKET=<private bucket>
COLLECTOR_OBJECT_STORE_ACCESS_KEY_FILE=<mounted access-key file>
COLLECTOR_OBJECT_STORE_SECRET_KEY_FILE=<mounted secret-key file>
COLLECTOR_OBJECT_STORE_REGION=<region identity>
```

Worker processes call Worker Gateway and use only scoped pre-signed URLs. They do not receive
`COLLECTOR_DATABASE_URL`, object-store account credentials, or a direct PostgreSQL route.

## Repository boundary

This repository owns collector state and collector exports. It must not write to a future catalog
database or define public listing, ranking, billing, booking, or SEO meaning. See
`docs/architecture/system-boundary.md` and `docs/implementation-status.md`.
