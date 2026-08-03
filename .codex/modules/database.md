# Collection database module

## Current owner

- SQLAlchemy metadata: `collection_infrastructure.postgres.metadata`.
- Migration execution: `collection_infrastructure.postgres.migrations`.
- Composition root: `apps/migration`.
- Migration history: `database/migrations/`.

## Implemented scope

The initial revision installs the PostGIS prerequisite and creates only the `config` schema tables
that project the existing `CampaignSnapshot` contract:

- `config_bundles`;
- `config_bundle_components`;
- `config_bundle_blockers`.

## Invariants

- migrations run only through the explicit migration process;
- `COLLECTOR_DATABASE_URL` must use `postgresql+psycopg` and name the target database;
- no migration runs during API, worker, readiness, or image startup;
- config rows are insert-only and protected by database triggers;
- digests, contract identities, readiness, counts, paths, and blocker codes have SQL constraints;
- foreign keys do not cascade-delete evidence;
- PostGIS remains an infrastructure prerequisite during downgrade;
- no worker receives database credentials.

## Proof

- metadata compilation tests;
- migration composition negative tests;
- fresh PostgreSQL 18/PostGIS migration in CI;
- integration checks for extension, tables, constraints, and immutable triggers.
