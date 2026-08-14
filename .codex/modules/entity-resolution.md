# Entity resolution and quality

## Owners

- `resolution_contracts` owns the immutable candidate batch, exact geography reference, manual
  decision, pair-feature, match disposition, cluster lineage, quality assessment, and resolution
  snapshot contracts.
- `entity_resolution_core` owns deterministic bounded blocking, integer similarity features,
  fail-closed disposition precedence, explicit separation constraints, deterministic cluster IDs,
  and reversible lineage.
- `quality_core` owns per-cluster blocking issues and `exportEligible`. Later export code consumes
  that verdict and must not reinterpret matching evidence.
- `resolution_worker` is the non-source Worker Gateway composition root. It receives one canonical
  `resolution_batch` artifact, publishes one verified `resolution_snapshot`, and has no PostgreSQL
  or S3 credentials.
- Campaign geography remains owned by `collection_application.geography` and the PostGIS adapter.
  Resolution consumes an exact market-area revision/digest and never recalculates polygon coverage.

## Invariants

- `entity_resolution` is a non-source capability and rejects a source permit.
- A name-only pair can never become `auto_match`.
- Fuzzy pairs inside or on the exact Berlin test boundary require review.
- Exact phone/email may auto-match only compatible entity kinds and non-conflicting geography.
- Website/address evidence needs policy-defined corroboration.
- An explicit separation blocks direct and transitive cluster joins.
- Cluster identity derives only from sorted member candidate IDs; split/reversal is reproducible.
- Quality starts blocked and becomes export-eligible only when every applicable blocking rule passes.
- Golden data is synthetic test evidence and does not make the real Berlin campaign runnable.

## Proof

```text
uv run pytest packages/resolution_contracts/tests \
  packages/entity_resolution_core/tests \
  packages/quality_core/tests \
  apps/resolution_worker/tests
uv run mypy
uv run python tools/architecture_checks/check_dependencies.py
docker build --file deploy/docker/resolution-worker.Dockerfile .
```
