# Work engine module

## Owners

- `collection_domain.work_units` owns stages, capabilities, work-unit states, legal transitions, and
  source-bound capability classification.
- `collection_domain.work_leases` owns immutable lease identity, active-time validation, and
  heartbeat renewal semantics.
- `collection_domain.work_retry` owns classified failure decisions and bounded retry delays.
- `collection_domain.work_artifacts` owns immutable lease input identity and role constraints.
- `collection_domain.source_capacity` owns source operational state and permit value contracts.
- `collection_application.work_engine` owns commands, results, validation, the `WorkEnginePort`, and
  owner-context error translation.
- `collection_infrastructure.postgres.work_engine.PostgresWorkEngine` is the PostgreSQL port
  implementation.
- `worker_gateway` is the authenticated worker-facing HTTP composition root. It may construct and
  invoke application commands but does not own Work Engine state transitions.

## Implemented runtime path

Run admission accepts only an exact published `ready` campaign snapshot. A blocked or missing
snapshot fails with a typed Work Engine conflict; mutable campaign files are not read from the run
path.

The PostgreSQL adapter implements worker registration, source capacity configuration, run and stage
creation, semantic work enqueue, lease acquisition, heartbeat, completion, classified failure,
explicit safe release, and bounded lease-expiry sweeping. It is exported through
`collection_infrastructure` for composition roots.

Worker registration binds one worker ID to its build identity, capabilities, supported output
contracts, resource profile, and local concurrency. Re-registration is idempotent only for the exact
same contract.

The Worker Gateway exposes authenticated registration, lease acquisition and heartbeat,
lease-scoped artifact upload/verification/read, typed completion, classified failure, explicit
release, protocol metadata, liveness, and readiness routes.
The authenticated principal supplies the worker identity; request bodies cannot choose another
worker. A mounted local credential binds the worker ID to an exact capability set, and registration
must match that scope. Tokens are hashed before in-memory lookup and are not returned in errors.

The gateway runs bounded lease-expiry sweeps through `WorkEngineService`. Health and readiness are
read-only. Readiness verifies PostgreSQL and the configured S3-compatible bucket and reports the
latest expiry-owner failure without performing migration, repair, or recovery. Object-store access
keys are read from bounded secret files rather than request bodies or campaign configuration.

## Persistence

Alembic and SQLAlchemy metadata own these physical schemas:

- `runs.collection_runs` and `runs.stage_runs`;
- `sources.source_capacity_states`;
- `work.worker_registrations`, `work.worker_capabilities`,
  `work.worker_output_contracts`, and `work.worker_heartbeats`;
- `work.work_units`, `work.work_attempts`, and `work.dead_letters`;
- `sources.artifact_uploads`, `sources.artifact_objects`, and `sources.artifact_records`;
- `work.work_input_artifacts` and `work.work_output_artifacts`.

`attempt_count` records every acquired lease. `failure_count` alone consumes retry budget, so a safe
release does not become a classified failure. Immutable work input is protected by a run-scoped
semantic key and input digest.

## Claim and mutation invariants

Lease acquisition uses `FOR UPDATE OF unit SKIP LOCKED` only inside the queue-claim query. The same
transaction verifies the running run and stage, worker capability and output-contract compatibility,
worker concurrency, source operational state, source capacity, minimum interval, and retry-after;
then it reserves capacity, creates the attempt, writes the lease token and deadlines, and returns the
lease.

Heartbeat, completion, failure, release, and expiry lock the current work and attempt. Completion requires the exact work ID, lease ID, lease token, worker ID, worker build identity,
input digest, expected output contract, and ordered output-artifact bindings. Each selected upload
must already be verified under that same active lease. Physical content objects may be reused by
kind and digest, while every completion creates a distinct immutable artifact record and role-bound
lineage. Artifact records, output bindings, consumed-upload state, attempt success, work success, and
capacity release are committed in one PostgreSQL transaction. An expired lease is persisted as
expired before the stale worker receives its rejection. Terminal failure creates one dead-letter
record without deleting prior attempts.

## Generated contract and deployment

`contracts/openapi/worker-gateway.openapi.json` is generated deterministically from the dependency-free
FastAPI app factory. CI checks route inventory, stable operation IDs, OpenAPI 3.1 identity, bearer
security on worker routes, and absence of that security requirement from public health routes.

`deploy/docker/worker-gateway.Dockerfile` builds a separate pinned, non-root runtime image from the
frozen workspace. The executable host composes `WorkEngineService` over `PostgresWorkEngine`, reads
its worker credential document from a mounted file, performs no startup migration, and rejects an
unsafe non-local bind until a broader deployment identity contract exists.

## Proof

Unit and structural tests cover command validation, transition and retry semantics, credential
parsing and rotation, capability authorization, request validation, owner-error mapping, correlation,
protocol metadata, readiness behavior, owner metadata, generated OpenAPI, and fail-closed SQL
constraints.

PostgreSQL integration tests cover ready/blocked run admission, registration idempotency, output
compatibility, concurrent claims, worker and source capacity, heartbeat, idempotent completion, safe
release, retry/dead-letter, expiry, stale completion, and authenticated HTTP registration, lease, and
completion against the durable database owner.

Permanent CI restores the frozen workspace, checks generated-contract drift, formatting, lint,
strict typing, unit and architecture tests, compiles owned Python, validates the Berlin campaign,
builds the Collector CLI, migration, and Worker Gateway images, applies a fresh PostgreSQL/PostGIS
migration, and runs the integration suite.

## Remaining boundary

The pre-signed protocol and atomic completion path are implemented. Stage 3 remains open until a
real SeaweedFS compatibility proof, owner-controlled staging/orphan cleanup, retention tombstones,
and manual-import raw-file preservation exist. No worker may receive PostgreSQL credentials or
bypass Worker Gateway and its application-owned ports.
