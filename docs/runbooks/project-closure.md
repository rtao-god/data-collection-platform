# Project closure checklist

The project is operationally closed only when all canonical proof files exist and the permanent
`Project End-to-End Closure`, `Operational E2E`, and `Verify` workflows are successful.

Canonical proof files:

- `docs/proofs/project-end-to-end-closure.md`;
- `docs/proofs/end-to-end-operational-ci.md`;
- `docs/proofs/berlin-live-collection.json`;
- `campaigns/berlin_recording_services/geography/berlin-boundary.provenance.json`;
- `campaigns/berlin_recording_services/source-inputs.provenance.json`.

A closure proof is invalid if raw artifacts, successful work units, candidate rows, or sealed exports
are zero; if coverage contains blocked/dead-letter state; or if the boundary/resource provenance is
not exact and licensed.

Recurring collection remains disabled until a separate operator approval records source terms,
measured budgets, alerting, retention capacity, and rollback procedure.
