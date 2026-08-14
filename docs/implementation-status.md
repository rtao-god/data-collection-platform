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
| Extraction and normalization | Digest-bound extraction requests, JSON-LD/microdata/RDFa and HTML contact/address evidence, bounded evidence spans, typed extracted records, explicit observation states, phone/URL/email/address/money normalization, negative-aware attribute patterns, derived artifacts, and a capability-isolated processing worker |
| Entity resolution and quality | Canonical candidate batches, bounded deterministic blocking, integer match features, strong-identifier/corroboration rules, name-only and fuzzy-Berlin review gates, immutable manual decisions, transitive separation protection, deterministic reversible clusters, fail-closed cluster quality, synthetic golden data, and a capability-isolated resolution worker |
| Database | Fresh PostgreSQL/PostGIS migration through `20260814_0010`, SQLAlchemy metadata, constraints, indexes, and integration tests for the implemented owners |
| Architecture enforcement | Fail-closed workspace/project registry, declared dependency graph, AST import checks, forbidden capability scans, and worker-image isolation checks |

## Permanent proofs

- `docs/proofs/reconciled-baseline-ci.md` — Stage 3/4 repository and migration baseline;
- `docs/proofs/worker-image-isolation-ci.md` — manual-import and OSM worker image boundaries;
- `docs/proofs/stage5-official-http-ci.md` — official HTTP contracts, worker behavior, full static/unit gate, migration, and image build;
- `docs/proofs/http-worker-isolation-ci.md` — permanent negative inventory proof for the HTTP worker image.

## Explicitly incomplete

- the infrastructure Compose contract currently starts PostgreSQL/PostGIS and SeaweedFS only; Worker Gateway and workers are not yet composed into one clean-machine runtime;
- SeaweedFS compatibility is a permanent dedicated infrastructure proof and remains excluded from ordinary unit execution;
- no approved Berlin boundary artifact and no real Berlin collection run or coverage report;
- no human review persistence/UI, suppression, browser, or export owner;
- no Control API, Dagster composition, retention deployable, or review frontend;
- the campaign does not yet bind and execute a complete manual/OSM/website acquisition flow.

## Next owner batch

Stage 8 is next: immutable review cases and decisions, exact-revision optimistic concurrency, manual observations, suppression, evidence-safe Control API responses, and the React FSD review console.

## Stage 8A — candidate and review foundation

Status: **contracts, pure transitions, and append-only schema implemented; runtime adapter and Control API remain**.

- `review_contracts` owns candidate revisions, review commands/decisions, manual observations, and suppression revisions.
- `review_core` owns optimistic-concurrency transitions and immutable supersession semantics.
- Migration `20260814_0010` owns candidate, quality, and review history tables with insert-only enforcement.
- Manual observations append evidence and never mutate source observations or candidate snapshots.
- Suppression has explicit discovery, normalization, and export scopes.
- PostgreSQL command adapter, Control API, authentication, and review UI are not claimed by this block.

## Stage 8B — review command adapter and Control API

Status: **application, PostgreSQL adapter, authenticated API, generated OpenAPI, and image implemented; frontend remains**.

- Actor identity is derived from the authenticated principal, not request data.
- Decisions, observations, and suppressions use exact command digests and optimistic concurrency.
- Review queue pagination uses opaque cursors.
- Control API startup does not run migrations.
- The React review console is the next Stage 8 owner.
