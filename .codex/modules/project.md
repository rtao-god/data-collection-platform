# Project module

## Owner

Repository boundary, dependency direction, implementation status, and proof commands.

## Read when

Read for any cross-package change, new deployable, new project reference, or final architecture
review.

## Current state

Implemented owners: campaign contracts, filesystem bundle adapter, snapshot service, typed errors,
work-unit transition vocabulary, CLI composition, PostgreSQL config-bundle metadata, Alembic
migration composition, generated JSON Schema drift checks, architecture checks, and CI.

The database slice is intentionally limited to meanings already owned by `CampaignSnapshot`. It does
not persist runtime runs or work leases, and it does not create a placeholder object-store reference.

Not implemented: Control API, Worker Gateway, durable work queue, acquisition workers, object store,
review console, entity resolution, quality engine, and export materialization. Do not create empty
projects or placeholder services for these owners.
