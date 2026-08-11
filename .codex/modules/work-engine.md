# Work engine module

## Owners

- `collection_domain.work_units` owns stages, capabilities, work-unit states, legal transitions, and
  source-bound capability classification.
- `collection_domain.work_leases` owns immutable lease identity, active-time validation, and
  heartbeat renewal semantics.
- `collection_domain.work_retry` owns classified failure decisions and bounded retry delays.
- `collection_domain.source_capacity` owns source operational state and permit value contracts.
- `collection_application.work_engine` owns commands, results, validation, the `WorkEnginePort`, and
  owner-context error translation.
- `collection_infrastructure.postgres.work_engine.PostgresWorkEngine` is the PostgreSQL port
  implementation.

## Implemented runtime path

Run admission accepts only an exact published `ready` campaign snapshot. A blocked or missing
snapshot fails with a typed Work Engine conflict; mutable campaign files are not read from the run
path.

The PostgreSQL adapter implements worker registration, source capacity configuration, run and stage
creation, semantic work enqueue, lease acquisition, heartbeat, completion, classified failure,
explicit safe release, and bounded lease-expiry sweeping. It is exported through
`collection_infrastructure` for a composition root.

Worker registration binds one worker ID to its build identity, capabilities, supported output
contracts, resource profile, and local concurrency. Re-registration is idempotent only for the exact
same contract.

## Persistence

Alembic and SQLAlchemy metadata own these physical schemas:

- `runs.collection_runs` and `runs.stage_runs`;
- `sources.source_capacity_states`;
- `work.worker_registrations`, `work.worker_capabilities`,
  `work.worker_output_contracts`, and `work.worker_heartbeats`;
- `work.work_units`, `work.work_attempts`, and `work.dead_letters`.

`attempt_count` records every acquired lease. `failure_count` alone consumes retry budget, so a safe
release does not become a classified failure. Immutable work input is protected by a run-scoped
semantic key and input digest.

## Claim and mutation invariants

Lease acquisition uses `FOR UPDATE OF unit SKIP LOCKED` only inside the queue-claim query. The same
transaction verifies the running run and stage, worker capability and output-contract compatibility,
worker concurrency, source operational state, source capacity, minimum interval, and retry-after;
then it reserves capacity, creates the attempt, writes the lease token and deadlines, and returns the
lease.

Heartbeat, completion, failure, release, and expiry lock the current work and attempt. Completion
requires the exact work ID, lease ID, lease token, worker ID, worker build identity, input digest, and
expected output contract. An expired lease is persisted as expired before the stale worker receives
its rejection. Terminal failure creates one dead-letter record without deleting prior attempts.

## Proof

Unit and structural tests cover command validation, transition and retry semantics, owner metadata,
and fail-closed SQL constraints. PostgreSQL integration tests cover ready/blocked run admission,
registration idempotency, output compatibility, concurrent claims, worker and source capacity,
heartbeat, idempotent completion, safe release, retry/dead-letter, expiry, and stale completion.

## Pending consumer boundary

The Worker Gateway HTTP composition root, worker authentication, pre-signed object protocol, and
artifact verification are not implemented yet. No worker-facing transport may bypass
`WorkEngineService` or receive PostgreSQL credentials.
