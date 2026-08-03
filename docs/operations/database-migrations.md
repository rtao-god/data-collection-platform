# Database migrations

## Boundary

Schema changes run only through the dedicated migration process. APIs, workers, health checks, image
startup, and read paths must not apply or repair migrations.

## Required environment

`COLLECTOR_DATABASE_URL` must be an explicit `postgresql+psycopg` URL naming the `collector_core`
database. Credentials are injected at runtime and must not be committed, copied into an image layer,
or printed in typed errors.

An alternate Alembic config may be supplied through `COLLECTOR_ALEMBIC_CONFIG` or `--config`; the
repository default is `database/alembic.ini`.

## Upgrade

```text
COLLECTOR_DATABASE_URL=<runtime secret> uv run collection-migrate upgrade head
```

The command returns a structured success payload or a typed owner-context failure. It never creates
a default database URL.

## Verification

A fresh PostgreSQL 18/PostGIS instance is migrated in CI. The integration contract verifies:

- PostGIS is available;
- the exact `config` tables exist;
- required check constraints exist;
- deferred child foreign keys and digest-scoped sealing exist;
- root insertion rejects incomplete, unordered, or inconsistent bundles;
- appending to a sealed bundle fails;
- update and delete attempts fail.

## Downgrade

The initial revision can remove its `config` tables and schema for controlled development recovery.
It intentionally leaves the PostGIS extension installed because later schema revisions may share the
database prerequisite. Production recovery should use forward migrations and restore proof rather
than an unreviewed downgrade.
