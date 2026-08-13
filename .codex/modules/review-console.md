# Review console

Status: in development

Specification: `docs/specifications/stage-8-review-console-v1.md`

## Owners

- `packages/review_contracts`: review admission, queue, detail, decision, manual observation, suppression, evidence preview, audit, and error contracts.
- `packages/review_application`: use cases, ports, optimistic concurrency, command idempotency, and evidence-preview orchestration.
- `packages/review_infrastructure`: PostgreSQL projection/history repositories, append-only enforcement, exact artifact catalog, and bounded S3 evidence reader.
- `apps/control_api`: FastAPI composition and HTTP interpretation.
- `apps/review_console`: React/TypeScript Feature-Sliced Design UI consuming Control API only.

PostgreSQL is canonical for review revisions and immutable history. Object Store is canonical for evidence bytes. Stage 6 observations and Stage 7 snapshots remain immutable inputs; review commands do not mutate them.

## Contracts

- deterministic idempotent admission by source snapshot digest, item kind, and subject ID;
- item kinds: `resolution_pair`, `cluster_quality`, `observation_conflict`;
- projection states: `pending`, `decided`, `suppressed`;
- every mutation requires `expectedRevision` and increments revision exactly once;
- decision, manual observation, suppression event, and audit rows are append-only;
- exactly one audit event exists per item revision;
- stale revision maps to `REVIEW_REVISION_STALE` / HTTP `409` and writes nothing;
- evidence preview is exact-artifact, bounded, decoded as text, and never executed.

## Physical boundaries

```text
control_api -> review_application
control_api -> review_contracts
control_api -> review_infrastructure
review_application -> review_contracts
review_infrastructure -> review_application
review_infrastructure -> review_contracts
review_infrastructure -> SQLAlchemy/boto3

review_console app -> pages -> widgets -> features -> entities -> shared
review_console -> Control API only
```

Frontend access to PostgreSQL, S3, Python packages, ORM clients, or backend source files is forbidden. Evidence rendering through `dangerouslySetInnerHTML`, iframe/srcdoc, document.write, or script execution is forbidden.

## Lifecycle

```text
immutable admission
-> pending projection revision 0 + admit audit
-> queue/detail read
-> expected-revision command
-> projection compare-and-swap
-> immutable command row + immutable audit in one transaction
-> API response
-> frontend cache reconciliation
```

Suppression is append-only activation/resolution history. Resolution restores `decided` when a decision exists, otherwise `pending`.

## Proof

Completion requires PostgreSQL schema/trigger proof, stale and idempotent command proof, atomic audit/history proof, bounded inert evidence proof, API contract tests, explicit React UI-state and stale-conflict tests, frontend safety/dependency scans, architecture checks, frozen Python and npm locks, Ruff, strict mypy, ESLint, TypeScript, Vitest, production builds, Control API image proof, permanent exact-head CI, and removal of this development status.
