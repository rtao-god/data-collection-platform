# Review persistence boundary

## Owner and observable result

`review_infrastructure` owns the PostgreSQL implementation of the `review_application.ReviewRepository`
port. `control_api` is its only production composition root.

The runtime dependency graph is:

```text
control_api
  -> review_application
  -> review_infrastructure
       -> review_application
       -> review_contracts
       -> review_core
       -> SQLAlchemy
```

Collection workers, Worker Gateway, migration, and collector CLI consume
`collection_infrastructure` without receiving review packages transitively.

## Protected scope

This boundary does not change:

- review wire contracts or Control API routes;
- review command, permission, concurrency, or transition semantics;
- PostgreSQL schemas, tables, constraints, or migration history;
- Collection run, work, artifact, campaign, or source adapters;
- the operator-visible result of any review command or query.

## Invariants

- `review_application` defines use cases and the persistence port;
- `review_core` defines deterministic review and suppression transitions;
- `review_contracts` defines immutable review data contracts;
- `review_infrastructure` performs PostgreSQL persistence only;
- `collection_infrastructure` must not import or declare review owners;
- non-review deployables must not contain review packages in their installed dependency closure;
- `control_api` may compose Collection and Review owners but may not redefine their meaning.

## Obsolete path

`collection_infrastructure.postgres.PostgresReviewRepository` is removed. No compatibility import or
parallel alias remains because only repository-owned callers consume this implementation.

## Proof boundary

The change is accepted only when:

1. architecture policy rejects review imports from `collection_infrastructure` and non-review apps;
2. Control API imports and composes `review_infrastructure.PostgresReviewRepository`;
3. review application and PostgreSQL integration tests pass unchanged in behavior;
4. the Worker Gateway image contains no review packages;
5. the Control API image contains the complete review owner chain;
6. lock, formatting, lint, type-check, compile, migration, and repository tests pass.
