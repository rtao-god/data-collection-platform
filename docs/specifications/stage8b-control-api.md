# Stage 8B — review command adapter and Control API

## Owners

`review_application` owns reviewer permissions, command construction, opaque queue cursors, and orchestration against `ReviewRepository`.

`review_infrastructure.PostgresReviewRepository` owns atomic optimistic-concurrency transactions, exact command replay, immutable supersession, queue reads, and mapping between PostgreSQL history and review contracts.

`control_api` is the only production composition root for Collection and Review persistence and owns authenticated HTTP transport. Actor identity is derived only from the bearer principal; request bodies cannot select or override the actor.

## Invariants

- Missing or invalid credentials fail with 401 and never appear in logs or error bodies.
- Permissions are explicit per read, decision, observation, and suppression operation.
- Review and suppression writes compare expected revisions inside the database transaction.
- A repeated exact command digest returns the immutable prior result; different content under the same digest is a conflict.
- Manual observations append evidence and never overwrite candidate snapshots or source observations.
- A replacement decision must explicitly supersede the current decision.
- Suppression identity and expiry cannot change during resolution.
- Control API startup never runs migrations.

## Deferred owner

The React review console consumes the generated OpenAPI contract in the next sequential block. It does not own scores, decisions, or export eligibility.

## Authentication boundary

The bearer credential is an internal reverse-proxy-to-Control-API
capability. It is injected at runtime, never returned to or stored by
the browser, and is not a replacement for the Stage 8C operator
cookie/bootstrap boundary. Runtime OpenAPI is disabled; consumers use
the checked-in generated contract.
