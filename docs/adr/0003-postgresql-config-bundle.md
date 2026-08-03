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
5. rejects `UPDATE` and `DELETE` through database triggers;
6. avoids cascade deletion and server-generated semantic defaults.

Migration execution is a separate `collection-migrate` process. It requires an explicit
`postgresql+psycopg` URL and is not invoked from service startup or readiness paths.

## Rejected options

### One JSONB snapshot row

Rejected as the only persistence model because component identity, blocker order, and SQL
constraints would be hidden inside an untyped payload.

### Full Collection schema in the first migration

Rejected because run, work, lease, observation, review, and export persistence require their own
approved contracts. Empty tables or guessed columns would create false owners.

### Object key placeholder

Rejected because Object Store preparation and integrity verification are not implemented. The
database must not contain a reference to an unverified or nonexistent object.

## Consequences

The migration proves the database owner and immutable config projection, but it does not yet create
a runtime snapshot record. That write path must be added together with content-addressed object
storage and the upload-verify-transaction sequence.
