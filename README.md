# Data Collection Platform

Universal collection platform for discovering, acquiring, extracting, normalizing, resolving,
reviewing, and exporting evidence-backed data from external sources.

The first configured campaign targets recording studios, producers, engineers, and adjacent
recording services in Berlin. Berlin- and audio-specific meaning is kept in campaign data, not in
platform packages.

## Current implementation slice

The repository implements the durable collection foundation through official HTTP acquisition,
evidence-backed extraction, and typed normalization:

- strict campaign, manual-import, HTTP acquisition, extraction, and observation contracts;
- immutable config snapshots, runs, stages, semantic work, leases, retries, dead letters, and source permits;
- authenticated Worker Gateway with scoped object reads/uploads and atomic artifact completion;
- content-addressed raw, diagnostic, and derived artifacts with orphan cleanup tombstones;
- isolated manual-import, OSM, HTTP, and processing workers without PostgreSQL credentials;
- JSON-LD/microdata/RDFa plus HTML contact/address extraction with bounded evidence spans;
- explicit observed/missing/prohibited/invalid observation states;
- phone, URL/domain, email, address, money, set, and negative-aware boolean normalization;
- PostgreSQL/PostGIS and SeaweedFS compatibility through the checked-in infrastructure Compose contract;
- generated schemas, architecture checks, strict typing, unit/integration tests, and capability-specific images.

This is not yet the complete platform. Candidate resolution, quality, review, browser acquisition,
sealed export, full application Compose, and a real Berlin run remain later stages. The Berlin
boundary is intentionally not fabricated; the campaign remains explicitly blocked until an approved
polygon artifact is added.

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
