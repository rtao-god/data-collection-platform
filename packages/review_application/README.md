# Review application

`review_application` owns review use cases and the boundary between HTTP composition and transactional persistence.

Every decision, manual observation, suppression activation, and suppression resolution requires an exact expected revision. The PostgreSQL repository performs the projection compare-and-swap, immutable command insert, and immutable audit insert in one transaction. A stale command writes no history and returns the current revision through `StaleReviewRevision`.

Admission is deterministic and idempotent by source snapshot digest, item kind, and subject identity. Evidence preview orchestration resolves one exact artifact through the repository, reads only a bounded prefix through the evidence port, and returns decoded text; it never exposes storage credentials or executable HTML.

This package defines application ports and failure semantics. It does not contain FastAPI routes, SQL, S3 SDK calls, frontend code, entity-resolution logic, or mutable catalog state. The immutable owner contract is `docs/specifications/stage-8-review-console-v1.md`.
