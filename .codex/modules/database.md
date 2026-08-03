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
- bundle children are inserted before the root inside one transaction;
- digest-scoped advisory locking serializes competing materializations;
- deferred foreign keys require a root before commit;
- root insertion validates contiguous components/blockers and readiness consistency;
- once the root exists, new child inserts, updates, and deletes fail;
- foreign keys do not cascade-delete evidence;
- PostGIS remains an infrastructure prerequisite during downgrade;
- no worker receives database credentials.

## Proof

- metadata compilation tests;
- migration composition negative tests;
- fresh PostgreSQL 18/PostGIS migration in CI;
- integration checks for extension, tables, deferred constraints, atomic seal, and immutability.
