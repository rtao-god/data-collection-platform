# System boundary

## Owned by this repository

The Data Collection Platform is the sole owner of:

- source usage policy and operational source state;
- exact campaign configuration snapshots used by a collection run;
- collection runs, stages, work units, leases, attempts, and retry state;
- raw acquisition metadata and immutable artifact references;
- extracted records and typed field-level observations;
- candidate revisions, match proposals, reversible clusters, and geography evaluations;
- quality results, review cases, immutable review decisions, and suppression records;
- deterministic sealed collector export packages.

## Explicitly outside the boundary

This repository does not own public listings, slugs, ranking, SEO text, paid placement, billing,
booking, publication state, public reviews, public-site analytics, or the database schema of a
future Aggregator Platform.

Workers must never write directly to Collection PostgreSQL. The planned Worker Gateway is the
single runtime boundary for leases, permits, artifact verification, completion, and typed failure.

The repository currently ends at a collector-owned export contract. No temporary DTO may pretend
to be the future Aggregator ingestion contract.
