# Campaign configuration module

## Source of truth

Authored files in `campaigns/<campaign-key>/` are the only authoring owner. A runtime run will use an
immutable canonical snapshot and its digest; mutable Git files must not be reread as runtime truth.

## Current flow

`FilesystemCampaignBundleSource` -> `CampaignSnapshotService` -> `CampaignSnapshot`.

The PostgreSQL migration owns insert-only metadata tables for the snapshot identity, ordered
component digests, and explicit blockers. No runtime writer exists yet because the object-store
upload and verification owner must be implemented before DB metadata can point at a stored bundle.

## Invariants

- campaign key and paths cannot escape the allowlisted root;
- symlinks and unexpected files are rejected;
- duplicate YAML keys are rejected;
- Pydantic models forbid unknown fields and implicit required defaults;
- cross-document references must resolve exactly once;
- manual seed headers and rows are explicit;
- canonical hashes are stable and content-sensitive;
- blocked readiness remains blocked;
- migrated config records cannot be updated or deleted;
- database metadata does not imitate an object-store artifact.
