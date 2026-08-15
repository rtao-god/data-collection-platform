# Pipeline advancement specification

Status: implementation-ready design

## Purpose

A succeeded work unit is immutable evidence, but it is not itself permission to mutate the next
stage. The platform needs one durable control-plane owner that converts exact successful output into
one explicit next action, preserves crash/retry safety, and records why the chain stopped when a
transition cannot be authorized.

This specification defines that owner. It does not make Dagster, a worker, or a recovery workflow a
second checkpoint owner.

## Observed defects

1. `work.work_units` records successful output contracts and digests, but no production composition
   root advances those outputs.
2. The existing manual-import admission model duplicates and tolerantly re-parses the canonical
   `ManualImportPlan` wire contract. It expects `ready`/`blocked`, while the producer owns
   `accepted`/`partial`/`rejected` dispositions.
3. Manual-import child work is configured by caller-provided target strings and currently binds only
   the source and plan artifacts. The selected record identity is not exposed to a worker through a
   canonical input role.
4. CI and recovery workflows name a pipeline owner that has never existed as committed production
   code. Workflow presence is not production ownership.

## Requirements

- One succeeded source work creates at most one canonical advancement record.
- Registration, lease, apply, block, expiry, and replay are durable.
- A Dagster execution crash cannot lose a successful source output or duplicate downstream work.
- A stale advancement lease cannot apply or block work.
- The owner selects transitions by exact stage, capability, output contract, and output digest.
- Unknown or currently unsupported output contracts become explicit blocked advancement state with
  owner context. They are not skipped.
- Downstream work keeps semantic idempotency and exact artifact lineage.
- Workers remain without PostgreSQL or object-store account credentials.
- Dagster invokes the application owner, but Collection DB remains the canonical checkpoint.

## Alternatives

### Completion-time scheduling inside Worker Gateway

Rejected. It would make worker completion own pipeline semantics, couple source completion to every
future stage, and make fan-out/review gates part of the untrusted worker-facing transaction.

### Dagster-owned scheduling

Rejected. Dagster metadata would become a second checkpoint owner. Loss of Dagster state could then
require recrawling or manual reconstruction, contrary to the repository contract.

### Durable Pipeline Advancement owner

Selected. A thin Dagster sensor/job composition invokes application-owned transitions over a
PostgreSQL lease/state adapter. Downstream work and applied advancement state commit in one
transaction. Object reads and manual-plan interpretation remain exact and fail closed.

## Canonical owners

- `collection_application.pipeline_advancement` owns advancement commands, immutable value
  contracts, legal state transitions, transition selection, and owner-context errors.
- `collection_infrastructure.postgres.pipeline_advancement` owns durable registration, lease,
  expiry, exact source/output loading, and atomic apply/block persistence.
- `apps/dagster_definitions` owns the Dagster sensor/job composition that invokes the application
  owner. Dagster metadata records invocations only; it is not a pipeline checkpoint.
- `collection_contracts.manual_import` remains the only manual-import wire owner.
- `manual_import_core` owns canonical plan and selected-record verification.
- `collection_application.manual_import_admission` owns one accepted plan record becoming one
  deterministic manual-record work unit.
- `manual_import_worker` owns only capability-scoped execution of `manual_import` and
  `manual_record`; it never schedules downstream work.

## Durable state

`work.pipeline_advancements` has one row per successful source work:

- advancement ID;
- source work, run, stage, capability, output contract, and output digest;
- exact selected output artifact ID, role, content digest, size, and content type;
- exact required parent input artifact IDs, roles, content digests, sizes, and content types when the
  transition depends on them;
- transition key and transition-plan digest over the complete immutable source and artifact identity;
- state: `pending`, `leased`, `applied`, or `blocked`;
- lease ID, token, worker identity, issued/expiry times;
- attempt count and revision;
- applied result digest or blocker owner/code/message/required action;
- correlation and UTC timestamps.

The source work ID is unique. The row is registered only after the source work is `succeeded` and its
output contract/digest are complete. The source output identity cannot be changed after registration.
`work.pipeline_advancement_attempts` stores one append-only lease attempt with lease identity,
Dagster execution/build identity, issued/expiry/finished times, outcome, and typed failure identity.

## Lifecycle

```text
succeeded work without advancement
→ registered pending advancement
→ leased by Pipeline Advancement through a Dagster execution
→ applied
   or blocked
```

An expired lease returns to `pending`, increments revision, and closes the append-only attempt as
`expired`. `applied` and `blocked` are terminal. Retrying the exact registration or apply returns the
exact prior result. Same source identity with a different output, artifact, or transition digest is
corruption. Supported routes register as `pending`; unsupported routes register directly as terminal
`blocked` and do not create a synthetic lease attempt.

## Concurrency and crash behavior

- Registration uses a bounded query and unique source-work constraint.
- Claim uses `FOR UPDATE SKIP LOCKED` only in the queue claim.
- Apply/block locks the advancement and validates lease ID, token, Dagster execution identity, source
  output and selected artifact identities, transition plan digest, and expiry.
- Downstream work enqueue and advancement `applied` state commit in one PostgreSQL transaction.
- Object reads occur before the mutation transaction and are verified by stored digest, size, and
  content type. A later transaction conflict is safe because reads are immutable and replayable.
- The Dagster sensor requests bounded one-at-a-time work initially. Parallelism can increase only
  after database/object-store measurements.
- Registration and transition execution are separate idempotent operations. A sensor retry may repeat
  either without changing the result.

## Initial transition registry

### `manual-import-plan-admission`

Source identity:

- stage: `discovery`;
- capability: `manual_import`;
- output contract: `manual-import-plan@1`;
- output role: `manual_import_plan`.

The handler also requires exactly one parent input artifact whose role is
`manual_source:<format>:<mode>` or `manual_import_source:<format>:<mode>`. It resolves exactly one
`manual_import_plan` output artifact and rejects missing, duplicate, or conflicting bindings.

The handler:

1. reads and verifies the plan artifact bytes against artifact metadata;
2. validates only the canonical `ManualImportPlan` contract;
3. verifies the plan's internal digest, source digest, mode, disposition, record and issue counts;
4. rejects `rejected` disposition from child admission;
5. creates one deterministic `manual_record` discovery work unit per accepted record;
6. binds the exact source artifact and the plan artifact with role
   `manual_import_plan_record:<zero-based-position>`;
7. commits admission evidence, child work, input bindings, and applied advancement state atomically
   through an explicit in-transaction admission port.

`accepted` and `partial` are admissible. A partial plan is admissible only because its producer
already proves explicit partial mode. No alias keys or old `ready`/`blocked` status are accepted.

### `manual-record-routing`

Source identity:

- stage: `discovery`;
- capability: `manual_record`;
- output contract: `manual-import-record@1`;
- output role: `manual_import_record`.

The initial implementation verifies the record result, then records an explicit blocked advancement
until the downstream manual-seed routing owner is implemented against approved website/OSM source
bindings in the exact campaign snapshot. It must not invent a website source policy or create
acquisition work under the manual-file policy.

All other successful output contracts are registered directly as blocked with
`PIPELINE_TRANSITION_UNSUPPORTED`, including exact source and artifact identity plus the required
implementation owner. This prevents silent successful dead ends while keeping unimplemented
downstream owners explicit. Pipeline blockers are projected into run coverage so an operator cannot
see a successful or merely empty run while advancement is terminally blocked.

## Manual record contract

`ManualImportRecordDocument` is an immutable source-specific discovery result. It contains:

- exact source and plan digests;
- exact plan record locator and record digest;
- validated `ManualSeedRow`;
- manual-record materializer revision;
- deterministic content digest.

It is not a normalized observation, candidate, review decision, or public listing. A manual seed
remains discovery input rather than accepted product truth.

## Manual worker boundary

The same image may run one configured capability per process:

- `manual_import` reads one source artifact and emits `manual-import-plan@1`;
- `manual_record` reads one exact plan-record binding, verifies the plan and selected record, and
  emits `manual-import-record@1`.

A process registers only its explicitly configured capability and output contract. The capability is
required configuration, not a silent default, and one registration cannot claim both. The
manual-record path does not need the source file body; it retains the exact source artifact and digest
as lineage. Legacy registration of the unrevisioned `manual-import-plan` output contract is removed.

## Failure model

Failures expose owner, expected state, actual state, source work/advancement identities, correlation,
and required action. Required codes include:

- `PIPELINE_SOURCE_OUTPUT_INCOMPLETE`;
- `PIPELINE_TRANSITION_UNSUPPORTED`;
- `PIPELINE_LEASE_STALE`;
- `PIPELINE_PLAN_DIGEST_CONFLICT`;
- `PIPELINE_INPUT_ARTIFACT_MISSING`;
- `PIPELINE_INPUT_ARTIFACT_CONFLICT`;
- `MANUAL_IMPORT_PLAN_CONTRACT_INVALID`;
- `MANUAL_IMPORT_PLAN_REJECTED`;
- `MANUAL_IMPORT_RECORD_INPUT_MISMATCH`;
- `MANUAL_RECORD_DOWNSTREAM_SOURCE_UNAVAILABLE`.

A transient object-store/database failure releases only through lease expiry or an explicit retryable
failure path; it never becomes a successful empty transition.

## Implementation boundaries

The first owner batch changes only:

- manual-import canonical contracts/core/application/worker;
- work capability registry and SQL checks;
- pipeline application, PostgreSQL adapter, Dagster definitions app, migration, image, module docs,
  run-coverage projection, and tests;
- workspace/dependency registry and permanent pipeline CI.

It does not implement HTTP/OSM routing from manual records, browser escalation, candidate aggregation,
review UI, export, retention, full application Compose, or real Berlin inputs.

## Proof

Required proof before the owner is declared complete:

1. contract tests for manual-record canonical digest and rejection of altered content;
2. application tests for transition selection, strict canonical plan decode, rejected-plan blocking,
   deterministic child identities, and unsupported transitions;
3. PostgreSQL tests for unique registration, concurrent claim, expiry, stale lease rejection,
   atomic child enqueue/apply, exact replay, and conflicting replay rejection;
4. manual worker tests for capability isolation, exact plan-record selection, digest verification,
   output role/contract, and failure classification;
5. architecture check registration and forbidden dependency proof;
6. generated contract drift check;
7. Dagster definitions/sensor tests proving that invocation metadata is not the checkpoint owner;
8. run-coverage tests proving terminal pipeline blockers are visible;
9. Ruff, strict mypy, non-integration suite, fresh migration, targeted integration suite, and image
   build in permanent CI.

Docker is unavailable in the current local execution environment, so image and PostgreSQL integration
proof must run in GitHub Actions before the production owner is reported as proven.

## Readiness review

- Owner and source of truth: fixed.
- Producer/consumer contracts: fixed.
- Persistence and rehydration: fixed.
- Concurrency, crash, replay, and stale lease behavior: fixed.
- Invalid states and diagnostics: fixed.
- First implementation boundary and excluded downstream scope: fixed.
- Proof and deployment path: fixed.
- Independent review tooling: unavailable in this execution environment; the implementation remains
  unapproved until the separate post-implementation boundary review and permanent CI proof complete.

No architecture choice remains for the writer inside this batch. Implementation may begin.
