# Candidate and review module

- Wire owner: `packages/review_contracts`.
- Pure transition owner: `packages/review_core`.
- Durable schema owner: migration `20260814_0010`.
- Review decisions and manual observations are append-only evidence.
- Optimistic concurrency is explicit through expected revisions.
- Suppression scopes are discovery, normalization, and export.
- Runtime PostgreSQL adapter owner: `packages/review_infrastructure`.
- Runtime composition and authenticated transport owner: `apps/control_api`.
- Worker Gateway and other non-review deployables must not depend on `review_infrastructure`.
