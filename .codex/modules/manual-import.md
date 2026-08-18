# Manual import

## Owners

- `collection_contracts.manual_import` owns immutable plan, locator, issue, record, mode, disposition, and materialized-record contracts.
- `manual_import_core` owns UTF-8 decoding, CSV/JSON/JSONL parsing, exact locators, row validation, record and plan digests, canonical plan decoding, and deterministic record materialization.
- `collection_application.manual_seed` owns campaign binding and source-policy enforcement and delegates parsing to `manual_import_core`.
- `collection_application.manual_import_admission` owns one deterministic `manual_record` work unit for each schedulable plan record.
- `manual_import_worker` is one capability-scoped process host; each process registers exactly one of `manual_import` or `manual_record`.
- `source_connector_sdk` owns strict consumption of the app-owned Worker Gateway transport; workers never receive PostgreSQL or object-store credentials.

## Invariants

- Atomic mode schedules no records when any issue exists.
- Partial acceptance is possible only when the caller explicitly requests partial mode and source policy permits it.
- CSV row numbers and JSONL line numbers are physical source locators.
- A plan is reusable only after its canonical semantic and artifact identities are verified.
- Every schedulable record becomes one deterministic work unit and one immutable `manual-import-record@1` artifact.
- The record artifact preserves source role, source digest, plan digest, plan artifact digest, selected position, locator, and record digest.
- `manual_record` has no source permit because it processes already admitted immutable artifacts.
- Downstream acquisition remains blocked until an approved campaign source owner maps the record to exact website or OSM work.
