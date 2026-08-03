# Campaign configuration module

## Source of truth

Authored files in `campaigns/<campaign-key>/` are the only authoring owner. A runtime run will use an
immutable canonical snapshot and its digest; mutable Git files must not be reread as runtime truth.

## Current flow

`FilesystemCampaignBundleSource` -> `CampaignSnapshotService` -> `CampaignSnapshot`.

## Invariants

- campaign key and paths cannot escape the allowlisted root;
- symlinks and unexpected files are rejected;
- duplicate YAML keys are rejected;
- Pydantic models forbid unknown fields and implicit required defaults;
- cross-document references must resolve exactly once;
- manual seed headers and rows are explicit;
- canonical hashes are stable and content-sensitive;
- blocked readiness remains blocked.
