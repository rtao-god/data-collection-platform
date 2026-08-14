# Candidate and review module

- Wire owner: `packages/review_contracts`.
- Pure transition owner: `packages/review_core`.
- Durable schema owner: migration `20260814_0010`.
- Review decisions and manual observations are append-only evidence.
- Optimistic concurrency is explicit through expected revisions.
- Suppression scopes are discovery, normalization, and export.
- Runtime PostgreSQL adapter and Control API are downstream owners.
