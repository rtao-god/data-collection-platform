# Implementation status

This ledger separates production owners from planned scope. A directory, compile-only shell, skipped
integration test, or synthetic fixture is not reported as implemented behavior.

## Implemented and checked in

| Area | Current owner/result |
|---|---|
| Campaign authoring | Strict YAML/CSV/JSON/JSONL campaign bundle under `campaigns/` |
| Campaign contracts | Pydantic owners in `collection_contracts` |
| Snapshot compilation | Deterministic canonical JSON and SHA-256 identity |
| Snapshot persistence | Transactional publication of immutable bundle metadata and blockers |
| Generated contracts | Checked-in JSON Schemas and Worker Gateway OpenAPI with drift checks |
| Typed failures | Owner-context error envelope |
| Runs and work | Durable runs, stages, semantic work units, attempts, leases, retries, and dead letters |
| Worker boundary | Authenticated Worker Gateway with worker identity, source permits, and no worker SQL path |
| Artifact transfer | Lease-scoped pre-signed upload/read, streamed integrity verification, and content-addressed promotion |
| Artifact completion | Verified output metadata and work success committed atomically in PostgreSQL |
| Artifact lineage | Ordered role-bound work inputs/outputs; physical object reuse preserves distinct records |
| Database ownership | Alembic migrations and SQLAlchemy metadata for `config`, `runs`, `work`, and current `sources` state |
| Migration process | Separate `collection-migrate` composition root and image |
| Architecture proof | Fail-closed owner registry, workspace/dependency checks, AST import graph, and capability allowlists |
| CI proof | Lock, contracts, formatting, lint, type-check, unit tests, architecture checks, fresh migration, integration tests, and images |

## Explicitly incomplete

- no owner command for staging/orphan cleanup, retention tombstones, or legal hold;
- no SeaweedFS Compose profile or live compatibility proof against a real S3-compatible server;
- manual files are parsed and snapshot-bound, but the source file is not yet preserved as a raw artifact and each row is not yet scheduled as its own work unit;
- no OSM, HTTP, browser, extraction, normalization, matching, quality, review, suppression, or export flow;
- no Control API, Dagster composition, acquisition/processing workers, or review frontend;
- no approved Berlin polygon or real-source production run.

## Next owner batch

The next coherent batch is Stage 3 closure: a SeaweedFS-backed local runtime profile, object-store
compatibility tests, owner-controlled staging/orphan cleanup with grace period and tombstones, and a
manual connector that stores the exact source file before scheduling one atomic work unit per row.
It must reuse the existing Worker Gateway artifact protocol and must not give workers SQL credentials.
