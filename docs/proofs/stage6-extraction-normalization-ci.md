# Stage 6 extraction and normalization CI proof

## Proven subject

- Status: `proven`
- Repository: `rtao-god/data-collection-platform`
- Branch: `main`
- Production owner commit: `1d65ec9f9f40a4bc315d4c2631d026ea1f466073`
- Permanent CI subject commit: `d86d7491c38c0c1e9164b2778bd47c65b4fe5c74`
- Permanent `Verify` run: `31789399120`
- `Verify` conclusion: `success`
- Permanent `Infrastructure Compatibility` run: `31789399136`
- Infrastructure conclusion: `success`
- Permanent `Worker Image Isolation` run: `31789180193`
- Worker-image conclusion: `success`

This document records evidence for exact commits and workflow runs. It does not become a second source of truth for contracts, observations, extraction, normalization, persistence, or worker behavior.

## Production owners

### `collection_contracts`

Owns the immutable transport contracts for:

- evidence locators, spans, and references;
- extracted fields and records;
- normalization profiles and rules;
- typed field observations;
- explicit `observed`, `not_observed`, `absent_in_source`, `unsupported`, `prohibited_by_policy`, `invalid`, `expired`, and `disputed` states;
- canonical JSON serialization and deterministic content digests.

### `extraction_core`

Owns the pure transformation:

```text
verified raw artifact
→ source-specific ExtractedRecord
```

The owner consumes HTML/structured-data representations, preserves bounded evidence, records extractor revision and raw-artifact identity, and does not produce public listings or normalized product truth.

### `normalization_core`

Owns the pure transformation:

```text
ExtractedRecord + exact NormalizationProfile
→ ObservationBatch
```

The owner handles typed text, URL, email, phone, structured-address, money, set, and evidence-backed boolean normalization. Missing, prohibited, and invalid values remain distinct typed states; no silent value repair or campaign-specific source logic is introduced.

### `processing_worker`

Owns only lease-scoped orchestration for `extraction` and `normalization` capabilities:

- input is read through a scoped Worker Gateway artifact contract;
- heartbeat remains active during processing;
- output is uploaded and verified as one immutable derived artifact;
- completion uses the exact lease, input digest, output contract, and artifact digest;
- typed failures are returned through Worker Gateway;
- no PostgreSQL or S3 credentials are present in the worker image.

## Permanent proof

The permanent `Verify` workflow proved:

- frozen workspace restoration and lock consistency;
- generated-contract drift absence;
- Ruff formatting and lint;
- strict mypy over the complete owned source set;
- non-integration tests;
- dependency-boundary enforcement;
- Python compilation;
- Berlin campaign validation;
- builds of collector CLI, HTTP worker, manual-import worker, OSM worker, processing worker, migration, and Worker Gateway images;
- fresh PostgreSQL/PostGIS migration and database integration tests.

The permanent `Infrastructure Compatibility` workflow additionally proved:

- the checked-in local PostgreSQL/PostGIS and SeaweedFS Compose contract;
- migration through `20260814_0009`;
- PostgreSQL integration for derived artifacts and Stage 6 ownership;
- SeaweedFS S3 lifecycle compatibility.

The permanent `Worker Image Isolation` workflow proved for `processing-worker`:

- runtime user `10001:10001`;
- exact `processing-worker` entrypoint;
- importability of `processing_worker`, `extraction_core`, `normalization_core`, and `source_connector_sdk`;
- absence of Alembic, boto3, botocore, Playwright, psycopg, Scrapy, and SQLAlchemy.

## Stage result

Stage 6 is complete at the implemented boundary:

```text
verified raw artifact
→ immutable extracted record with bounded evidence
→ typed normalized observations
→ verified derived artifact
```

The stage does not claim candidate resolution, geography selection, quality verdicts, review, export, or real Berlin coverage. Those remain downstream owners.
