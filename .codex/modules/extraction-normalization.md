# Extraction and normalization

Status: in development

Specification: `docs/specifications/stage-6-extraction-normalization-v1.md`

## Owners

- `packages/observation_contracts`: typed observations, evidence, field assessments, extraction policy, and output bundle contracts.
- `packages/normalization_core`: deterministic phone, email, URL, address-text, and money normalization.
- `packages/extraction_core`: bounded JSON-LD, microdata, RDFa, contact/address extraction, policy application, diagnostics, and observation materialization.
- `apps/extraction_worker`: Worker Gateway composition and exact lease processing.

Work Engine owns scheduling/retry/crash recovery. Object Store owns raw evidence. The worker has no direct database, network crawler, browser, or S3 implementation ownership.

## Contracts

- capability: `extraction`;
- input roles: `raw_source`, `extraction_request`;
- request contract: `extraction-request@1`;
- expected output contract: `typed-observation-bundle@1`;
- output role: `typed_observations`;
- bundle revision: `typed-observation-bundle-v1`.

Every observation requires artifact-bound evidence. Every requested field requires exactly one assessment: `observed`, `conflicting`, `not_present`, `present_empty`, `invalid`, `blocked_by_policy`, or `unsupported`.

## Physical boundaries

```text
extraction_worker -> extraction_core
extraction_worker -> observation_contracts
extraction_worker -> source_connector_sdk
extraction_core -> normalization_core
extraction_core -> observation_contracts
normalization_core -> phonenumbers
observation_contracts -> pydantic
```

Forbidden in the worker image: Scrapy, Playwright, Alembic, boto3/botocore, psycopg, SQLAlchemy, database migrations, collection infrastructure, and direct Object Store clients.

## Lifecycle

```text
DB-owned extraction work
-> exact request + raw artifact lease inputs
-> policy/artifact validation
-> bounded inert parsing
-> deterministic normalization
-> evidence-bound observation bundle
-> Worker Gateway upload
-> exact lease completion
```

Malformed source content becomes bounded diagnostics and explicit assessment states when safe completion is possible. It is not silently treated as absence and is not retried locally.

## Proof

Completion requires deterministic contract tests, structured/contact/address fixtures, normalizer negative cases, policy/prohibited-field enforcement, evidence coverage, missing-state coverage, worker/gateway tests, architecture checks, frozen lock, Ruff, strict mypy, compilation, permanent `Verify`, and permanent extraction-worker image isolation proof.
