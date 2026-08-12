# Manual import

## Owners

- `collection_contracts.manual_import` owns the immutable plan, locator, issue, record, mode, and disposition contracts.
- `manual_import_core` owns UTF-8 decoding, CSV/JSON/JSONL parsing, exact locators, row validation, record digests, plan digests, and the complete issue ledger.
- `collection_application.manual_seed` owns campaign binding and source-policy enforcement and delegates parsing to `manual_import_core`.
- `source_connector_sdk` owns strict consumption of the app-owned Worker Gateway transport; it never receives PostgreSQL or object-store credentials.

## Invariants

- Atomic mode schedules no records when any issue exists.
- Partial acceptance is possible only when the caller explicitly requests partial mode and source policy permits it.
- CSV row numbers and JSONL line numbers are physical source locators.
- A plan is reusable only after its canonical digest is verified.
- Runtime source-artifact persistence and one-row-per-work scheduling remain a separate owner batch.
