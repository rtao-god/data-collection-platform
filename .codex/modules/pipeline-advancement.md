# Pipeline advancement

Status: in development

## Owner

`collection_application.pipeline_advancement` owns the durable interpretation of one exact succeeded
work output into one explicit next action. `collection_infrastructure.postgres.pipeline_advancement`
owns registration, lease, expiry, and atomic apply/block persistence. `apps/dagster_definitions`
invokes that owner through a sensor/job composition; Dagster metadata is not a checkpoint or
transition owner.

## Invariants

- Collection DB is the only atomic pipeline checkpoint; Dagster may orchestrate later but cannot own
  work completion or resume state.
- Every succeeded work unit has one advancement row or a terminal owner-defined exception, and every
  terminal advancement blocker is visible in run coverage.
- Unknown transitions block with owner context; they are never skipped.
- Apply requires the exact source output, transition plan, lease ID/token, Dagster execution identity, and
  unexpired lease.
- Downstream enqueue and applied state commit together.
- Workers remain without database credentials.
- Manual import plans are decoded only through the canonical `ManualImportPlan` contract; the legacy
  unrevisioned output contract and alias-tolerant parser are removed.
- One accepted manual record becomes one deterministic `manual_record` work unit; it does not become
  a normalized observation or candidate automatically.

## Initial routes

- `manual_import` + `manual-import-plan@1` → strict plan admission → `manual_record` work.
- `manual_record` + `manual-import-record@1` → explicit downstream-source-binding blocker until an
  approved website/OSM source owner exists in the exact campaign snapshot.
- every other successful output → explicit unsupported-transition blocker.

## Proof

See `docs/specifications/pipeline-advancement.md`. The status line is removed only after local static
proof, fresh PostgreSQL integration, image proof, permanent CI, and post-implementation boundary
review succeed.
