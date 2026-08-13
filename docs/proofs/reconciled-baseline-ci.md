# Reconciled baseline CI proof

## Proven subject

- Status: `proven`
- Repository: `rtao-god/data-collection-platform`
- Branch: `main`
- Subject commit: `83de76941590e422ec2378e69899f773609b704d`
- Permanent workflow: `Verify`
- Workflow run: `31736560687`
- Event: `push`
- Conclusion: `success`

This document records evidence for the exact subject commit. It does not become a second source of truth for code, contracts, migrations, or workflow state.

## Successful owner gates

The `verify` job (`94569492811`) completed successfully and proved:

- frozen workspace restoration from the committed `uv.lock`;
- lock consistency;
- generated contract drift absence;
- Ruff formatting and lint;
- strict mypy over the owned source set;
- the non-integration pytest suite;
- dependency-boundary enforcement;
- Python compilation for the configured owned source roots;
- validation of `berlin_recording_services`;
- collector CLI image build;
- migration image build;
- Worker Gateway image build.

The `migration` job (`94569492798`) completed successfully and proved:

- startup of an isolated PostgreSQL 18/PostGIS 3.6 service;
- fresh Alembic migration through `20260813_0008`;
- the PostgreSQL/PostGIS integration contract, including manual-import ownership and artifact-cleanup tombstones.

## Scope boundary

This proof establishes the permanent reconciled baseline for the subject commit. Capability-minimal manual-import and OSM worker images are proven separately in `worker-image-isolation-ci.md`.

The SeaweedFS object lifecycle compatibility test remains explicitly classified as `integration` and `object_store_integration`. It requires its dedicated external infrastructure profile and is not represented here as part of the ordinary unit job.
