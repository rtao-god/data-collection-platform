# Collection database module

## Current owner

- SQLAlchemy metadata: `collection_infrastructure.postgres.metadata` and owner-specific metadata
  modules under `collection_infrastructure.postgres`.
- Migration execution: `collection_infrastructure.postgres.migrations`.
- Composition root: `apps/migration`.
- Migration history: `database/migrations/`.

## Implemented scope

Alembic owns the physical PostgreSQL/PostGIS contract for immutable campaign snapshots, durable
runs/stages/work/attempts, append-only run transitions, source capacity, artifact transfer and
lineage, manual-import admission, derived observations/candidates/quality, and review history. The
current head is `20260815_0012`.

Run state is canonical operational truth in `runs.collection_runs`. Operator transitions are
optimistic-concurrency commands recorded in `runs.collection_run_transitions`; pause is enforced by
the Work Engine lease query, and cancel updates only non-leased pending/retry work while preserving
immutable attempts and artifacts.

## Invariants

- migrations run only through the explicit migration process;
- `COLLECTOR_DATABASE_URL` must use `postgresql+psycopg` and name the target database;
- no migration runs during API, worker, readiness, or image startup;
- immutable evidence/history tables reject update/delete where their contract requires it;
- run, stage, work, attempt, artifact, candidate, and review identities remain explicit;
- revisioned state changes use exact optimistic concurrency and append-only history;
- foreign keys do not cascade-delete evidence;
- PostGIS remains an infrastructure prerequisite during downgrade;
- no worker receives database credentials.

## Proof

- metadata compilation and architecture tests;
- migration composition negative tests;
- fresh PostgreSQL 18/PostGIS migration in CI;
- integration checks for constraints, atomic owner transactions, immutable history, run control,
  worker concurrency, artifacts, and review persistence.
