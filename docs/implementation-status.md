# Implementation status

This ledger separates production owners from planned scope. A directory, compile-only shell, skipped
integration test, or synthetic fixture is not reported as implemented behavior.

## Implemented and checked in

| Area | Current owner/result |
|---|---|
| Campaign authoring | Strict YAML/CSV campaign bundle under `campaigns/` |
| Campaign contracts | Pydantic owners in `collection_contracts` |
| Snapshot compilation | Deterministic canonical JSON and SHA-256 identity |
| Generated contracts | Checked-in JSON Schemas plus digest manifest and drift check |
| Typed failures | Owner-context error envelope |
| Work vocabulary | Exact work-unit states and legal transition graph |
| Database metadata | SQLAlchemy Core projection with atomic digest-level sealing |
| Migration history | Alembic revision creating PostGIS prerequisite and immutable `config` tables |
| Migration process | Separate `collection-migrate` composition root and image |
| Architecture proof | AST dependency checker including the migration boundary |
| CI proof | Lock, contracts, lint, type-check, unit tests, fresh migration, PostGIS integration, images |

## Explicitly incomplete

- no runtime DB writer for campaign snapshots until Object Store verification exists;
- no collection-run, stage-run, work-unit, lease, attempt, or source-capacity persistence;
- no Control API or Worker Gateway;
- no worker credentials, worker registration, or source permits;
- no raw artifact/object-store owner;
- no OSM, HTTP, browser, extraction, normalization, matching, quality, review, suppression, or export flow;
- no review frontend or Node workspace;
- no approved Berlin polygon or real-source production run.

## Next owner batch

The next coherent batch is the durable work contract: run/work/attempt persistence, lease token and
input-digest ownership, queue claim transaction, expiry/retry classification, source capacity, and
the minimal Worker Gateway transport required to prove concurrent leasing. It must not introduce an
in-memory production queue or give workers SQL credentials.
