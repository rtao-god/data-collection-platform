# Stage 8A — candidate and review foundation

## Owners

`review_contracts` owns versioned candidate revision, review decision, manual observation, and suppression contracts. All command identities and evidence digests are explicit.

`review_core` owns pure optimistic-concurrency transitions. It never imports PostgreSQL, FastAPI, workers, connectors, or object-store adapters.

Migration `20260814_0010` owns append-only candidate, quality, and review history. Every foreign key uses restrictive deletion and every history table rejects update/delete mutations.

## Invariants

- Candidate revisions are immutable snapshots with contiguous, unique evidence lineage.
- A stale expected case or suppression revision fails; it is not silently rebased.
- Decisions are immutable. A replacement must explicitly supersede the current decision.
- Manual edits append manual observations and do not mutate source observations or existing candidate revisions.
- Review text is plain text; markup delimiters are rejected in contracts and PostgreSQL constraints.
- Suppression scopes are explicit across discovery, normalization, and export.
- Export eligibility remains owned by deterministic quality evaluation, not by the review frontend.

## Deferred downstream owners

The PostgreSQL command adapter, authenticated Control API, review queue queries, operator UI, and sealed collector export are separate sequential blocks. This foundation does not claim them.
