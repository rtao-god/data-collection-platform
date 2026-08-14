# Control API module

- Application owner: `packages/review_application`.
- PostgreSQL owner: `PostgresReviewRepository` in collection infrastructure.
- HTTP composition root: `apps/control_api`.
- Reviewer actor identity comes from authenticated bearer configuration only.
- OpenAPI is generated into `contracts/control_api` and checked for drift.
- Startup migration is forbidden.
