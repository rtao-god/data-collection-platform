# ADR 0003: PostgreSQL owner for campaign snapshot metadata

## Status

Accepted.

## Context

The campaign compiler already owns a strict `CampaignSnapshot` containing a bundle digest, contract
identity, campaign key, ordered component digests, readiness, and explicit blockers. Stage-one
persistence must represent that meaning without inventing run, worker, or object-store state.

## Decision

Use SQLAlchemy Core metadata and an explicit Alembic history for PostgreSQL 18. The first revision:

1. installs the PostGIS prerequisite in `collector_core`;
2. creates only the `config` schema;
3. stores snapshot root metadata in `config.config_bundles`;
4. stores ordered component digests and blockers in typed child tables;
5. seals a bundle atomically by inserting children before the root under a digest-scoped advisory
   transaction lock and deferred foreign keys;
6. validates component order and readiness/blocker consistency when the root is inserted;
7. rejects every later child insert, update, or delete;
8. avoids cascade deletion, duplicate count columns, and server-generated semantic defaults.

Migration execution is a separate `collection-migrate` process. It requires an explicit
`postgresql+psycopg` URL and is not invoked from service startup or readiness paths.

## Rejected options

### One JSONB snapshot row

Rejected as the only persistence model because component identity, blocker order, and SQL
constraints would be hidden inside an untyped payload.

### Root row followed by unrestricted child inserts

Rejected because an already committed bundle could be extended and its digest would no longer
identify the stored composition. The root-last seal contract makes that invalid state impossible.

### Stored component and blocker counts

Rejected because the child rows already own those counts. Storing derived counts would create a
second value that could drift.

### Full Collection schema in the first migration

Rejected because run, work, lease, observation, review, and export persistence require their own
approved contracts. Empty tables or guessed columns would create false owners.

### Object key placeholder

Rejected because Object Store preparation and integrity verification are not implemented. The
database must not contain a reference to an unverified or nonexistent object.

## Consequences

The migration proves the database owner and immutable config projection, but it does not yet create
a runtime snapshot record. The future repository adapter must insert children and root in one
transaction after content-addressed upload verification succeeds.
