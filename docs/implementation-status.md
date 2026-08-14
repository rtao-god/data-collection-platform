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
| Official website HTTP | Strict request/manifest contracts, canonical URL and public-address enforcement, robots/sitemap/page-interest planning, one-request Scrapy execution, conditional `304` reuse, bounded raw acquisition, typed `403`/`429` behavior, isolated worker, generated schemas, and Docker image |
| Database | Fresh PostgreSQL/PostGIS migration through `20260813_0008`, SQLAlchemy metadata, constraints, indexes, and integration tests for the implemented owners |
| Architecture enforcement | Fail-closed workspace/project registry, declared dependency graph, AST import checks, forbidden capability scans, and worker-image isolation checks |

## Permanent proofs

- `docs/proofs/reconciled-baseline-ci.md` — Stage 3/4 repository and migration baseline;
- `docs/proofs/worker-image-isolation-ci.md` — manual-import and OSM worker image boundaries;
- `docs/proofs/stage5-official-http-ci.md` — official HTTP contracts, worker behavior, full static/unit gate, migration, and image build;
- `docs/proofs/http-worker-isolation-ci.md` — permanent negative inventory proof for the HTTP worker image.

## Explicitly incomplete

- no Docker Compose runtime that starts PostgreSQL, SeaweedFS, Worker Gateway, and workers from a clean machine;
- the SeaweedFS compatibility test is opt-in infrastructure proof, not part of ordinary unit execution;
- no approved Berlin boundary artifact and no real Berlin collection run or coverage report;
- no extraction, normalization, candidate resolution, quality, review, suppression, browser, or export owner;
- no Control API, Dagster composition, retention deployable, or review frontend;
- the campaign does not yet bind and execute a complete manual/OSM/website acquisition flow.

## Next owner batch

Stage 6 is next: implement extraction and normalization from exact immutable raw artifacts. The batch must introduce evidence-backed extracted records, typed field observations, JSON-LD/microdata/RDFa and contact/address extraction, phone/URL/address/money normalization, prohibited-field enforcement, and an isolated processing worker without general internet access.
