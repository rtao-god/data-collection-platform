# Manual Seed Live Vertical

## Observable outcome

A clean local runtime must execute one exact Berlin campaign revision containing reviewed manual seeds and produce a reproducible ledger with:

1. one immutable source artifact for the exact seed file;
2. one successful `manual_import` plan;
3. one `manual_record` work unit per accepted seed row;
4. one immutable typed observation set per manual record;
5. one candidate revision and one open review case per accepted seed;
6. explicit run coverage that distinguishes completed, blocked, failed, and unresolved work.

The vertical proves real data flow. It does not claim market completeness, publication readiness, or export eligibility.

## Scope

### Affected owners

| Owner | Input | Produced meaning |
|---|---|---|
| Campaign configuration | Git-authored Berlin campaign bundle | Exact immutable campaign revision |
| Campaign run bootstrap | Exact campaign revision | Collection run, stage owners, initial manual-import work, raw source artifact |
| Manual import worker | Exact seed artifact | Canonical `manual-import-plan@1` |
| Manual import admission | Successful plan and exact artifact lineage | One deterministic `manual_record` work unit per accepted row |
| Manual record worker | Exact accepted row input | Immutable `manual-import-record@1` document |
| Manual observation materialization | Exact manual-record document and campaign mapping revision | Typed observation batch |
| Candidate intake | Exact observation batch | Candidate revision, evidence, quality state, open review case |
| Run coverage | Canonical stage/work/candidate rows | Explicit counts and blockers |

### Protected scope

- no direct worker or CLI access to PostgreSQL;
- no collector write into `aggregator-backend`;
- no browser crawling;
- no automatic acceptance, publication, or sealed export;
- no synthetic or demo production rows;
- no OSM or website facts presented as completed until those owners execute;
- no legacy transport fallback.

## Caller graph

```text
operator / bounded proof
→ Control API create run
→ Collection run bootstrap
→ Worker Gateway
→ manual-import worker
→ pipeline supervisor
→ manual-record worker
→ pipeline supervisor
→ manual observation materializer
→ candidate intake
→ Control API run/coverage and review reads
```

Pipeline supervision is an application command invoked through the Control API. It may read verified artifacts before the database mutation, but the advancement state transition and downstream persistence occur atomically in their canonical PostgreSQL owners. A future Dagster composition calls the same Control API command and does not receive Collection DB credentials.

## Contracts

### `manual-import-record@1`

The document is canonical JSON and contains:

- parent manual-import work identity;
- exact plan artifact identity and digest;
- exact source artifact identity, digest, role, and row locator;
- exact accepted record digest;
- typed seed values;
- campaign/run identity;
- worker build identity.

It represents discovery evidence only. It is not a candidate and not an accepted fact.

### Manual observation batch

The manual observation materializer creates only fields present in the accepted seed contract:

- expected entity kind;
- display name;
- website;
- optional OSM ID;
- optional reference URLs;
- optional note;
- provenance.

Every observation carries the manual-record artifact digest and row locator. Missing optional values remain explicit missing states and are not fabricated.

### Candidate intake

Each observation batch creates one stable candidate identity derived from the exact run and manual-record semantic identity. Revision `0` is immutable. The candidate state is review-required. The intake transaction writes:

- candidate identity;
- candidate revision payload and digest;
- evidence references;
- fail-closed quality record;
- open review case and initial review-case revision.

A replay with the same semantic identity returns the existing result. The same identity with another digest is a typed conflict.

## State and idempotency

| Operation | Semantic identity |
|---|---|
| Manual import plan | source artifact digest + parser revision + mode |
| Manual record | run ID + plan digest + accepted record digest + position |
| Observation batch | manual-record artifact digest + mapping revision |
| Candidate intake | run ID + manual-record work ID + observation-batch digest |

A completed atomic unit is never removed because a later unit fails. A stale lease cannot complete work. Pipeline replay verifies prior result identities instead of silently skipping.

## Failure model

Every failure records owner, expected state, actual state, context, required action, and correlation ID. Required blockers include:

- invalid or noncanonical seed file;
- plan/source artifact lineage mismatch;
- unsupported manual-record document revision;
- missing required seed field;
- observation contract failure;
- candidate identity conflict;
- unavailable review schema;
- incomplete downstream work.

The run remains incomplete or blocked; it is never reported as successful empty data.

## Proof

The owner proof uses a clean PostgreSQL/PostGIS and SeaweedFS runtime and the production composition roots:

1. validate the exact campaign bundle;
2. apply fresh migrations;
3. create the run through Control API;
4. run the manual-import and manual-record capabilities through Worker Gateway;
5. invoke bounded pipeline supervision until no applicable transition remains;
6. query canonical work, artifact, candidate, review, and coverage state;
7. verify exact counts equal the accepted seed count;
8. verify every candidate remains review-required and export-ineligible;
9. write a deterministic run report containing run ID, campaign digest, source digest, work IDs, artifact digests, candidate IDs, review case IDs, counts, blockers, and proof commit.

The proof fails if it uses direct SQL inserts for business data, fixture candidates, or a non-production worker path.

## Readiness gate

Implementation may proceed because:

- the user-visible vertical and protected scope are explicit;
- current owners and missing owners are identified;
- the wire and persistence outputs are fixed for this batch;
- the caller graph preserves Worker Gateway and Control API boundaries;
- negative invariants and proof are defined;
- export, browser, OSM, website crawling, frontend, retention, and backup remain separate owner batches.
