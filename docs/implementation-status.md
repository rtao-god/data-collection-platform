# Implementation status

This ledger distinguishes implemented production contracts from work that still lacks its owning artifacts or infrastructure proof.

## Implemented in the foundation slice

| Requirement area | Owner/result | Proof |
|---|---|---|
| Python workspace | Python 3.13, uv lock, Ruff and strict mypy configuration | CI quality job |
| Dependency boundaries | Executable AST import graph for shared/domain/configuration/application/infrastructure/entrypoints | Architecture checker and negative tests |
| Typed failures | Stable code, message and owner context | Domain/configuration tests |
| UTC domain time | Explicit aware UTC only; no implicit normalization | Negative run tests |
| Collection run lifecycle | Strict created/running/cancelling/terminal transitions | Domain tests |
| Work leasing | Owner token, expiry, renewal, retry and finite attempt budget | Domain tests |
| Campaign contract | Strict JSON/GeoJSON/NDJSON source validation | Configuration tests |
| Source policy gate | Explicit approval, robots policy, exact hosts, request/rate/concurrency budgets | Configuration tests |
| Seed evidence gate | Website, OSM id or reference URL required | Configuration tests |
| Geography gate | Polygon/MultiPolygon, finite coordinates, bounds and closed rings | Configuration tests |
| Deterministic artifact | Canonical JSON, exact source manifest, single SHA-256 owner, atomic output | Repeat compilation and output tests |
| Administrative entrypoint | `compile-campaign` command with structured diagnostics | Source inspection and contract tests |
| CI | Lock, lint, strict type-check, architecture, tests and compile checks | GitHub Actions |

## Deliberately not represented as complete

The following areas remain outside this commit because their production owners require further implementation or verified external artifacts:

- PostgreSQL/PostGIS schemas, Alembic migrations and row-level persistence;
- SeaweedFS-compatible raw/evidence object storage and atomic object metadata;
- Control API and Worker Gateway HTTP contracts;
- queue, lease persistence, scheduler and concurrency integration;
- HTTP, OSM and browser worker adapters;
- SSRF/DNS-rebinding/browser sandbox enforcement;
- extraction, assertion, provenance, entity-resolution and human-review workflows;
- deterministic catalog export and downstream delivery;
- OpenAPI, JSON Schema and generated TypeScript contracts;
- telemetry, audit trail, retention and erasure workflows;
- Docker Compose and container hardening;
- real-source fixture suites and opt-in live smoke;
- PostgreSQL, object-store and queue integration tests;
- any deployable Berlin campaign bundle.

These are not marked successful by empty projects, mocked production responses or placeholder data.

## Required owner batch next

The next coherent implementation batch is persistence-backed orchestration:

1. define PostgreSQL owners for run, work unit, lease and outbox state;
2. add Alembic fresh migration and migration tests;
3. implement application ports and transaction boundaries around the existing domain lifecycle;
4. implement lease acquisition with database concurrency proof;
5. expose only the minimal Control API and Worker Gateway contracts consumed by that flow;
6. generate checked-in OpenAPI/JSON Schema artifacts and verify drift;
7. add Testcontainers integration proof.

Real campaign collection remains blocked until geography, source policies and seeds are reviewed and committed as evidence-bearing source artifacts.
