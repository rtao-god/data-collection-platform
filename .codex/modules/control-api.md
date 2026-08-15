# Control API module

## Owners

- Run creation: `collection_application.campaign_runs.CampaignRunService`.
- Run reads, coverage, and operator transitions: `collection_application.run_control`.
- Review commands and reads: `packages/review_application`.
- PostgreSQL ports: `PostgresCampaignRunStore`, `PostgresRunControlRepository`, and
  `PostgresReviewRepository` in collection infrastructure.
- HTTP composition root: `apps/control_api`.

## Runtime contract

Authenticated operator identity is the only actor source. Request bodies cannot choose an actor.
Permissions are explicit for run creation/read/control and review operations. Run transitions require
an exact expected revision and append immutable history; pause blocks new leases through the
canonical run state, resume revalidates stage ownership, and cancel terminalizes pending work without
rewriting completed evidence.

The API exposes read-only liveness/readiness, run create/read/coverage/pause/resume/cancel, and review
queue/decision/observation/suppression routes. Coverage reports exact work-state counts plus explicit
run, stage, dead-letter, and policy blockers. Startup migration is forbidden. Readiness verifies the
required PostgreSQL owner tables and object-store dependency without mutating either.

OpenAPI and operation inventory are generated into `contracts/control_api` and checked for drift.
Runtime documentation routes are disabled.
