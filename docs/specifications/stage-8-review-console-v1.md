# Stage 8 — Review console

Specification identity: `stage-8-review-console-v1`

This specification is immutable. A material owner, command contract, persistence lifecycle, evidence-safety rule, API interpretation, or frontend state model change requires a replacement specification.

## 1. Result and owners

Stage 8 adds one internal review system with a transactional Control API and a React review console.

Production owners:

- `packages/review_contracts` owns review-item, admission, queue, detail, decision, manual-observation, suppression, evidence-preview, audit, and error wire contracts.
- `packages/review_application` owns review use cases, ports, optimistic-concurrency semantics, evidence-preview orchestration, and command failure classification.
- `packages/review_infrastructure` owns PostgreSQL persistence, append-only history, projection updates, exact-artifact lookup, and the S3-compatible bounded evidence reader.
- `apps/control_api` owns FastAPI composition and HTTP interpretation only.
- `apps/review_console` owns the browser review UI using Feature-Sliced Design and Control API contracts only.
- PostgreSQL remains canonical for review queue state, revisions, immutable decisions, manual observations, suppression events, and audit.
- Object Store remains canonical for source evidence bytes. The review database stores only exact artifact identities, locators, and immutable review metadata.

The review system does not mutate Stage 6 observations, Stage 7 snapshots, clusters, or quality verdicts. Its immutable decisions become explicit inputs to later recomputation.

## 2. Dependency direction

Backend:

```text
control_api
  -> review_application
  -> review_contracts
  -> review_infrastructure

review_application
  -> review_contracts

review_infrastructure
  -> review_application
  -> review_contracts
  -> SQLAlchemy
  -> boto3
```

Frontend:

```text
app -> pages -> widgets -> features -> entities -> shared
```

The frontend communicates only with the Control API. Direct PostgreSQL, S3, ORM, filesystem, or Python-package access is forbidden.

## 3. Review item identity and admission

Review item kinds:

- `resolution_pair`;
- `cluster_quality`;
- `observation_conflict`.

An internal admission request contains:

- source snapshot contract and digest;
- campaign key;
- item kind;
- stable subject ID;
- immutable canonical payload;
- zero or more exact evidence bindings;
- correlation ID.

The review item ID is a deterministic UUID derived from source snapshot digest, item kind, and subject ID. Admission is idempotent:

- the same identity, payload digest, and evidence bindings returns the existing item;
- the same identity with different immutable content fails with `REVIEW_ADMISSION_CONFLICT`;
- admission never replaces or mutates an existing payload.

An admitted item starts at revision `0`, state `pending`, and receives an immutable `admit` audit event in the same transaction.

## 4. Mutable projection and immutable history

`review.review_items` is the only mutable review projection. It contains stable identity, current state, current revision, current decision identity, active suppression identity, payload digest, and timestamps.

The following tables are insert-only and physically reject update/delete:

- `review.review_item_payloads`;
- `review.evidence_bindings`;
- `review.review_decisions`;
- `review.manual_observations`;
- `review.suppression_events`;
- `review.audit_events`.

Every mutation increments the item revision by exactly one. Immutable identity, campaign, kind, subject, source snapshot, payload digest, and creation time cannot change.

## 5. Optimistic concurrency

Every decision, manual-observation, suppression activation, and suppression resolution command requires `expectedRevision`.

The repository owns the compare-and-swap operation:

```sql
UPDATE review.review_items
SET revision = revision + 1, ...
WHERE review_item_id = :id
  AND revision = :expected_revision
```

Zero updated rows produces `REVIEW_REVISION_STALE`; the API returns HTTP `409` with the current revision when the item still exists. A stale command writes no decision, observation, suppression event, or audit row.

Projection update, immutable command row, and immutable audit row occur in one PostgreSQL transaction. Partial success is forbidden.

## 6. Decisions

Decision actions are explicit by review kind:

- resolution pair: `match` or `separate`;
- cluster quality: `approve` or `reject`;
- observation conflict: `accept_candidate`, `reject_candidate`, or `defer`.

A decision contains:

- deterministic decision ID supplied by the client or generated once by the API boundary;
- item ID;
- expected and resulting revision;
- action;
- reason code;
- optional selected observation/candidate ID where the action requires it;
- actor reference;
- correlation ID;
- UTC timestamp;
- canonical decision digest.

A successful decision sets the projection state to `decided` unless an active suppression remains, in which case the visible state remains `suppressed`. Previous decisions are never updated or deleted.

## 7. Manual observations

A reviewer may append an explicit manual observation containing:

- field key;
- value kind;
- normalized value;
- reason code;
- optional evidence note limited to plain text;
- actor, correlation, revisions, timestamp, and digest.

The review owner records the observation; it does not mutate Stage 6 bundles or silently make the value canonical. A later recomputation/publishing owner must consume the immutable manual observation explicitly.

Manual observations increment revision and write an audit event but do not automatically mark the review item decided.

## 8. Suppression

Suppression actions are `activate` and `resolve`.

Activation requires a reason code and makes the projection state `suppressed`. Resolve requires the exact active suppression ID and restores visible state to `decided` when a decision exists, otherwise `pending`.

Suppression history is append-only. Resolving a suppression does not update the activation event. Suppression hides an item from the default queue; it does not delete evidence, payloads, decisions, observations, or audit history.

## 9. Audit completeness

Every successful command writes exactly one immutable audit event containing:

- command kind and command ID;
- item ID;
- actor;
- expected and resulting revision;
- payload digest;
- UTC timestamp;
- correlation ID.

There is exactly one audit event per item revision, including admission revision `0`. Audit rows are insert-only and cannot contain raw evidence bodies or secrets.

## 10. Queue and detail reads

Queue reads use opaque cursor pagination. Cursor identity includes creation time and item ID; clients do not parse it.

Default queue state is `pending`. API-supported filters are explicit: item kind, state, campaign key, and bounded page size. The frontend never sorts or filters a downloaded page as if it were the complete queue.

Item detail returns:

- current projection and revision;
- immutable payload;
- evidence bindings;
- decision history;
- manual observations;
- suppression history;
- audit history.

All history collections are sorted deterministically by resulting revision and immutable ID.

## 11. Evidence preview

Evidence lookup is exact by artifact ID. Infrastructure joins `sources.artifact_records` to `sources.artifact_objects`, verifies that the artifact exists and has a storage reference, then reads a bounded prefix from the configured S3-compatible bucket.

The endpoint returns JSON containing:

- artifact ID;
- recorded content digest and content type;
- recorded object size;
- requested maximum bytes;
- actual returned bytes;
- truncation flag;
- UTF-8 text decoded with replacement;
- storage content is never returned as executable HTML.

The maximum preview is bounded to 256 KiB. The response copies no authorization, cookie, or arbitrary object-store metadata. Raw bytes and secrets never enter logs or error messages.

The React UI renders preview text only as a text node inside `<pre>`. `dangerouslySetInnerHTML`, iframe/srcdoc, document.write, DOMParser-driven insertion, and script execution are forbidden.

## 12. Control API

Routes:

- `POST /v1/internal/review/admissions`;
- `GET /v1/review/queue`;
- `GET /v1/review/items/{reviewItemId}`;
- `POST /v1/review/items/{reviewItemId}/decisions`;
- `POST /v1/review/items/{reviewItemId}/manual-observations`;
- `POST /v1/review/items/{reviewItemId}/suppressions`;
- `GET /v1/review/evidence/{artifactId}/preview`;
- `GET /health`.

All mutations require non-empty `X-Review-Actor` and `X-Correlation-Id` headers. Admission additionally requires `X-Review-Internal-Key`, compared against configured internal credentials without logging the value. Reads are internal-network endpoints and return no database or storage credentials.

HTTP mapping:

- invalid request or policy: `400`;
- missing item/artifact: `404`;
- stale revision or admission identity conflict: `409`;
- missing/invalid actor or internal key: `401`/`403`;
- unavailable dependency: `503`;
- unexpected defect: bounded RFC 7807-style `500` without internal payloads.

## 13. React review console

The frontend uses React, TypeScript, Vite, TanStack Query, React Router, Vitest, and Testing Library with exact committed package versions and lockfile. Node uses the committed current Node 24 LTS patch selected from the official Node distribution index during owner materialization.

Feature-Sliced Design:

```text
src/app
src/pages/review-queue
src/pages/review-item
src/widgets/review-layout
src/features/submit-decision
src/features/add-manual-observation
src/features/manage-suppression
src/entities/review-item
src/shared/api
src/shared/config
src/shared/ui
```

Required observable states:

- actor not configured;
- queue loading;
- queue error with retry;
- empty queue;
- queue ready;
- detail loading;
- detail error;
- detail ready;
- mutation pending;
- stale revision conflict with current revision and reload action;
- suppression active/resolved;
- evidence loading/error/empty/truncated/ready.

The actor reference is entered explicitly and stored in browser local storage. It is sent only in mutation headers. The UI does not invent successful state before the server response.

## 14. Frontend safety and accessibility

- no raw HTML execution or insertion;
- evidence preview uses plain text;
- mutation buttons are disabled without actor identity or while pending;
- labels are associated with controls;
- validation errors are visible and announced;
- loading and mutation status use ARIA live regions;
- keyboard focus remains visible;
- stale conflict does not silently retry or overwrite newer state;
- queue pagination is explicit and cursor-driven.

## 15. Deployment and configuration

Control API configuration is environment-owned:

- PostgreSQL URL;
- S3 endpoint, bucket, access key, secret key, and region;
- internal admission key;
- maximum evidence preview bytes;
- optional allowed frontend origin.

The Control API image runs as a non-root user. The review console builds to static assets and receives only the Control API base URL; no backend secret is compiled into the frontend.

## 16. Failure, crash, and restart

Control API commands are request-scoped database transactions. A process crash before commit produces no successful command state; a crash after commit is recoverable by idempotent command identity and immutable audit/history.

Admission and command IDs are unique. Retrying the exact successful command returns the recorded result when its immutable digest matches; a reused ID with different payload fails closed.

Frontend state is cache only. Reloading reconstructs current state from Control API. Browser local storage contains only actor reference and UI preferences, never evidence bodies or credentials.

## 17. Proof

Required automated proof:

- migration creates exact review schema and append-only triggers;
- update/delete against immutable tables fails;
- projection revision must increment exactly once;
- admission is deterministic and idempotent;
- conflicting admission fails;
- stale decision writes no history/audit and API returns `409`;
- decision/manual observation/suppression and audit are atomic;
- suppression activation/resolution lifecycle;
- exact queue cursor behavior;
- evidence lookup and bounded/truncated UTF-8 preview;
- raw HTML remains inert in API and React tests;
- no frontend DB/S3 packages or direct backend imports;
- explicit UI states and stale-conflict interaction;
- Python lock, generated-contract drift, Ruff, strict mypy, unit and PostgreSQL integration tests, architecture checks, compilation;
- frontend exact lock, ESLint, TypeScript, Vitest, production build, and source safety scan;
- Control API image build and non-root runtime identity;
- permanent exact-head GitHub Actions proof.

## 18. Non-goals

This stage does not implement public authentication, production SSO, entity-resolution recomputation orchestration, catalog publication, browser crawling, export, or a production Berlin run. It does not bypass the existing `BERLIN_BOUNDARY_ARTIFACT_MISSING` blocker.
