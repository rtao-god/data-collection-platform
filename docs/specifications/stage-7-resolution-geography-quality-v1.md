# Stage 7 — Entity resolution, geography integration, and quality

Specification identity: `stage-7-resolution-geography-quality-v1`

This specification is immutable. A material owner, matching contract, cluster lifecycle, geography interpretation, decision rule, or export-quality rule change requires a replacement specification.

## 1. Result and owners

Stage 7 converts one immutable candidate batch into a deterministic entity-resolution snapshot containing pair features, match dispositions, reversible clusters, geography-bound quality assessments, and explicit review work.

Production owners:

- `packages/resolution_contracts` owns candidate, geography reference, decision, pair-feature, cluster-lineage, quality, batch, and resolution-snapshot wire contracts.
- `packages/entity_resolution_core` owns deterministic blocking, pair feature calculation, disposition rules, constrained clustering, and cluster lineage.
- `packages/quality_core` owns fail-closed cluster quality and export-eligibility assessment.
- `apps/resolution_worker` owns Worker Gateway composition, non-source lease validation, batch parsing, snapshot publication, and failure mapping.
- the existing campaign geography owner in `collection_application.geography`, backed by the PostGIS adapter in `collection_infrastructure.postgres.geography`, remains canonical for point/boundary classification. Stage 7 consumes its exact boundary revision, digest, and `inside`/`boundary`/`outside`/`unknown` result and does not reimplement polygon classification.
- Work Engine remains canonical for scheduling, retry, lease expiry, crash recovery, and stage progression.
- Object Store remains canonical for immutable batch and snapshot bytes. The worker has no direct database or S3 credentials.

Stage 7 does not own human review persistence, canonical catalog entities, publication, or export materialization.

## 2. Dependency direction

```text
resolution_worker
  -> entity_resolution_core
  -> quality_core
  -> resolution_contracts
  -> source_connector_sdk

entity_resolution_core
  -> resolution_contracts

quality_core
  -> resolution_contracts

resolution_contracts
  -> pydantic
```

The resolution worker image must not contain Alembic, boto3, botocore, Playwright, psycopg, Scrapy, SQLAlchemy, database migrations, collection infrastructure, or direct Object Store clients.

## 3. Work and artifact contracts

The worker capability is `entity_resolution`. It is a non-source capability: the lease must have no `source_key`.

Input artifact role: `resolution_batch`.

Input contract: `entity-resolution-batch@1` / revision `entity-resolution-batch-v1`.

Expected output contract: `entity-resolution-snapshot@1`.

Output artifact role: `resolution_snapshot`.

Output revision: `entity-resolution-snapshot-v1`.

The batch is an immutable, canonical snapshot. It embeds bounded candidate records, exact geography references, explicit manual decisions, optional prior clusters, resolution thresholds, and quality rules. It does not point to mutable latest state.

## 4. Candidate contract

Each candidate contains:

- deterministic candidate ID;
- entity kind;
- sorted unique normalized names, phones, emails, website URLs, and addresses;
- exact observation IDs and source-artifact IDs from Stage 6;
- sorted unique source keys for provenance;
- one geography reference produced by the existing campaign-geography owner.

A geography reference contains:

- coverage: `inside`, `boundary`, `outside`, or `unknown`;
- exact market-area revision and digest;
- optional classified latitude/longitude;
- observation IDs supporting the coordinate when a point exists.

Every candidate geography reference must use the exact batch market-area revision and digest. `inside`, `boundary`, and `outside` require coordinates; `unknown` must not invent them.

The production batch is rejected while the market-area readiness is not `ready`. The existing `BERLIN_BOUNDARY_ARTIFACT_MISSING` campaign blocker remains authoritative; Stage 7 tests may use a clearly identified synthetic fixture but must not make the Berlin campaign runnable.

## 5. Blocking

Blocking is deterministic and bounded. Candidate pairs may be generated from:

- exact normalized phone;
- exact normalized email;
- exact normalized website host;
- entity-kind plus normalized name token/signature;
- entity-kind plus normalized address token/signature;
- an explicit manual decision pair.

Pair generation is deduplicated, canonicalized by candidate ID, sorted, and capped by the batch policy. Exceeding the pair limit fails the batch rather than silently dropping potential matches.

A shared name may create a review candidate but is never sufficient for an automatic merge.

## 6. Match features and dispositions

Pair features are explicit and versioned:

- exact phone overlap;
- exact email overlap;
- exact website-host overlap;
- normalized name similarity in integer basis points;
- normalized address similarity in integer basis points;
- entity-kind compatibility;
- geography coverage compatibility;
- source overlap;
- strong non-name feature count;
- `nameOnly` marker.

Allowed dispositions:

- `auto_match`;
- `manual_match`;
- `review_required`;
- `no_match`;
- `manual_separate`.

Precedence:

1. an explicit `separate` decision prevents the pair from joining;
2. entity-kind mismatch cannot auto-merge;
3. an explicit `match` decision is represented as `manual_match`;
4. exact phone or email may auto-match compatible entity kinds;
5. website-host or address evidence may auto-match only with policy-required corroboration;
6. name-only, fuzzy-name, fuzzy-address, and otherwise ambiguous evidence requires review or remains no-match.

A pair whose only positive evidence is name similarity can never become `auto_match`.

Any fuzzy pair where both candidates are classified `inside` or `boundary` for the exact Berlin market-area revision must be `review_required` with reason `FUZZY_BERLIN_MATCH_REQUIRES_REVIEW`.

## 7. Manual decisions

Manual decisions are immutable input records produced by the later review owner. Each decision contains an ID, canonical candidate pair, action `match` or `separate`, revision, actor reference, reason code, and decision digest.

There may be at most one active decision for a pair in a batch. The resolution engine does not invent, mutate, or persist review decisions.

A direct `separate` decision overrides automatic evidence for that pair. Transitive clustering must also respect it: a match edge that would place any explicitly separated pair in one component is blocked and recorded in the snapshot.

## 8. Clusters and reversible lineage

Clusters are built deterministically from accepted `auto_match` and `manual_match` edges, subject to all separation constraints.

Match edges are processed in deterministic priority order: manual match first, then automatic matches by descending evidence strength and pair identity. Before joining two components, the engine proves that no explicit separation would be violated.

Cluster IDs are UUIDs derived from the sorted member candidate IDs. Therefore the same membership always produces the same cluster ID.

The batch may include prior immutable clusters. Every output cluster records:

- parent cluster IDs with overlapping members;
- lineage kind: `new`, `unchanged`, `split`, `merge`, or `recombined`;
- digest of the exact parent membership used for lineage;
- blocked match edges affecting the cluster.

A split never mutates or deletes its parent snapshot. Reversing the separating decision in a new immutable batch recomputes the prior membership and therefore restores the original deterministic cluster ID. This is the reversible split contract.

## 9. Quality and export eligibility

Quality is evaluated per cluster and is fail-closed. `exportEligible` defaults to false and becomes true only when all applicable rules pass.

Quality rules are explicit per entity kind and include:

- required fields;
- single-value fields that may not remain conflicting;
- minimum distinct source count;
- allowed geography coverage;
- whether `boundary` requires review;
- whether pending pair review blocks export.

Blocking reasons include at minimum:

- missing quality policy;
- mixed entity kinds;
- required field absent;
- single-value field conflict;
- insufficient distinct sources;
- geography `outside` or `unknown`;
- geography boundary review required;
- pending resolution review touching the cluster;
- a match edge blocked by a separation constraint;
- missing observation provenance.

Warnings never make a blocked cluster eligible. Export code in a later stage must consume `exportEligible`; it must not independently reinterpret raw match scores.

## 10. Golden dataset

The repository owns a versioned synthetic golden dataset under `datasets/entity_resolution/`.

It contains no production business data and explicitly identifies its market boundary as a test fixture. It covers:

- exact strong-identifier match;
- same-name-only non-merge;
- fuzzy Berlin review requirement;
- manual separation;
- quality eligibility and blockers;
- deterministic output identities.

The dataset is validated in the ordinary unit suite. A partial ad hoc fixture is not a replacement for this golden contract.

## 11. Output snapshot

The canonical snapshot contains:

- batch digest and market-area identity;
- pair features and dispositions;
- blocked match edge IDs;
- deterministic clusters and lineage;
- quality assessments;
- pending review pair IDs;
- bounded diagnostics;
- output digest inputs.

All collections are sorted canonically. Re-running the same batch bytes produces byte-identical output and the same digest.

## 12. Failure semantics

- malformed batch, duplicate candidate/decision identity, non-canonical collections, geography revision mismatch, blocked market-area readiness, unsupported contract revision, or exceeded candidate/pair limit: `permanent`;
- missing or malformed immutable input artifact: `permanent`;
- stale lease or artifact-transfer failure: Worker Gateway/Work Engine semantics;
- engine defect preventing deterministic completion: `permanent` with a stable defect code and no candidate payload in the failure message.

The worker does not retry locally and does not persist a local cluster cache.

## 13. Concurrency, crash, and restart

The resolution worker registers `entity_resolution` with maximum concurrency `1`. It reads one bounded canonical batch, calculates the snapshot in memory, uploads one canonical result, and completes the exact lease.

A crash before completion leaves the lease to expire. A staged but uncommitted snapshot is owned by existing orphan cleanup. Restart acquires DB-owned work and recomputes from immutable inputs; no local cluster state is consulted.

## 14. Proof

Required proof:

- name-only pair never auto-merges;
- exact phone/email match behavior;
- website/address corroboration thresholds;
- fuzzy Berlin pair requires review;
- explicit separation blocks direct and transitive joins;
- split lineage and reversal restore deterministic cluster membership/ID;
- geography revision/digest/readiness validation;
- quality blockers and fail-closed export eligibility;
- golden dataset validation;
- deterministic canonical bytes and IDs;
- non-source lease enforcement and restart behavior;
- architecture checks, frozen lock, Ruff, strict mypy, unit tests, compilation;
- capability-minimal resolution worker image and negative dependency inventory;
- permanent exact-head GitHub Actions proof.

## 15. Non-goals

This stage does not persist human review decisions, expose review APIs/UI, geocode addresses, calculate the market polygon, render browsers, publish catalog entities, or materialize exports.
