# Implementation status

This ledger reports only production owners that are present in the registered workspace and covered by owner tests. Planning documents, generator scripts, placeholder directories, and isolated fixtures are not implementation evidence.

## Implemented production owners

| Stage/area | Current owner/result |
|---|---|
| Foundation | Python 3.13 `uv` workspace, exact lock, Ruff, strict mypy, pytest, architecture checks, generated-contract drift checks, Alembic, and capability-specific Docker images |
| Campaign configuration | Strict campaign documents, cross-reference validation, canonical JSON, deterministic SHA-256 bundle identity, readiness blockers, and immutable snapshot publication |
| Runs and work | Durable runs, stage runs, semantic work units, attempts, leases, heartbeat, expiry, typed retries, dead letters, worker registration, output compatibility, and source permits |
| Worker boundary | Authenticated Worker Gateway is the only worker-facing state and artifact boundary; source workers have no PostgreSQL or S3 credentials |
| Object transfer | Lease-scoped pre-signed upload/read, streamed size/digest verification, content-addressed object promotion, ordered artifact bindings, and atomic work completion |
| Manual import | CSV/JSON/JSONL parsing, exact row/line locators, deterministic plan identity, complete issue ledger, explicit `reject_all`/`accept_valid` semantics, isolated worker, and transactional admission of one child work unit per accepted record |
| Artifact cleanup | Grace-period orphan selection, persisted cleanup tombstones, bounded retry, terminal failure, and S3-compatible delete adapter |
| OSM/Overpass | Query contract and allowlisted grammar, deterministic planning, bounded HTTP adapter, response parsing, provenance/attribution output, isolated worker, and campaign geography evaluation support |
| Database | Fresh PostgreSQL/PostGIS migration through `20260813_0008`, SQLAlchemy metadata, constraints, indexes, and integration tests for the implemented owners |
| Architecture enforcement | Fail-closed workspace/project registry, declared dependency graph, AST import checks, forbidden capability scans, and worker-image isolation checks |

## Explicitly incomplete

- no Docker Compose runtime that starts PostgreSQL, SeaweedFS, Worker Gateway, and workers from a clean machine;
- the SeaweedFS compatibility test is opt-in infrastructure proof, not part of ordinary unit execution;
- no approved Berlin boundary artifact and no real Berlin collection run or coverage report;
- no production official-website HTTP connector or `http-worker`;
- no extraction, normalization, candidate resolution, quality, review, suppression, browser, or export owner;
- no Control API, Dagster composition, retention deployable, or review frontend;
- the campaign does not yet bind and execute a complete manual/OSM/website acquisition flow.

## Next owner batch

Stage 5 is next: implement the registered `official_http` connector and isolated `http_worker` around one DB-owned leased request. The batch must include robots and sitemap handling, deterministic URL normalization and page-interest planning, conditional requests with typed `304` reuse, bounded raw acquisition, explicit `403`/`429` behavior, an HTTP-worker image without browser/database/S3 dependencies, and permanent CI proof.
