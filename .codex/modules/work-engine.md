# Work engine module

## Current owner

`collection_domain.work_units` owns the initial state vocabulary and legal transition graph.

## Implemented states

`pending`, `leased`, `retry_wait`, `succeeded`, `dead_letter`, `blocked_by_policy`, `cancelled`,
`superseded`.

## Invariants

Terminal states cannot transition. `unchanged` is a successful acquisition outcome, not a work-unit
state. Lease tokens, attempts, persistence, queue claiming, retries, and Worker Gateway transport
are not implemented yet and must not be imitated by in-memory placeholders.
