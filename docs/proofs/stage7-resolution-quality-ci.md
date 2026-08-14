# Stage 7 resolution and quality CI proof

## Proven subject

- Status: `proven`
- Repository: `rtao-god/data-collection-platform`
- Branch: `main`
- Production owner commit: `7f89cc96480f7f72d9ffc76b278115d48fce3dca`
- Permanent CI commit: `e6315dfd32e7fadf484e1f8d9e39d0be87a44511`
- Apply/proof workflow run: `31805171869`
- Permanent `Verify` run: `31805509561`
- Permanent `Worker Image Isolation` run: `31805509442`
- Event: `push`
- Conclusion: `success`

This document records evidence for exact immutable commits and workflow runs. It does not become a second source of truth for resolution policy, quality rules, generated schemas, worker composition, or image capabilities.

## Production owner boundary

Stage 7 is owned by the following production packages and deployable:

- `resolution_contracts` owns strict immutable resolution batch/snapshot contracts, canonical serialization, digests, deterministic pair identities, and deterministic cluster identities;
- `entity_resolution_core` owns bounded blocking, deterministic pair features, match dispositions, separation enforcement, reversible cluster composition, and cluster lineage;
- `quality_core` owns deterministic quality findings and fail-closed export eligibility;
- `resolution_worker` owns the worker-facing composition from one immutable resolution batch artifact to one immutable resolution snapshot artifact through the Worker Gateway protocol.

Campaign-specific fuzzy-review reason codes remain batch policy. Core packages do not contain Berlin-specific domain types or branch on campaign identity.

## Permanent `Verify` proof

Workflow run `31805509561` completed successfully for commit `e6315dfd32e7fadf484e1f8d9e39d0be87a44511`.

The `verify` job proved:

- frozen restoration of the complete `uv` workspace;
- exact lock consistency;
- generated JSON Schema drift absence;
- Ruff formatting and lint;
- strict mypy across the registered production source set;
- the complete non-integration test suite, including Stage 7 negative cases;
- fail-closed architecture dependency enforcement;
- Python compilation;
- first-campaign configuration validation;
- independent Docker image build for `resolution-worker` and all previously registered deployables.

The `migration` job proved that Stage 7 did not regress the fresh PostgreSQL/PostGIS migration and existing database integration contract.

## Resolution and quality invariants proved

The owner tests prove at least the following required properties:

- pair and cluster identities are deterministic and validated by the canonical contract;
- name similarity alone cannot create an accepted merge edge;
- fuzzy primary-area matches are explicit review dispositions;
- entity-kind incompatibility blocks a merge;
- manual separation blocks the direct edge and prevents transitive cluster recombination;
- split/merge/recombined cluster lineage remains explicit and reversible;
- overlapping cluster membership is contract-invalid;
- all pending-review and blocked-edge references must resolve to emitted pair evidence;
- geography outside the allowed policy and unresolved boundary review produce explicit blockers;
- missing required fields, unresolved conflicts, insufficient provenance, and unresolved match review block export eligibility;
- an eligible candidate is produced only when every configured blocking condition passes.

## Worker image isolation proof

Workflow run `31805509442` completed successfully for commit `e6315dfd32e7fadf484e1f8d9e39d0be87a44511`.

The `resolution-worker` job (`94783339349`) proved:

- the dedicated resolution image builds from the frozen dependency graph;
- runtime user is exactly `10001:10001`;
- runtime entrypoint is exactly `resolution-worker`;
- `resolution_worker`, `entity_resolution_core`, `quality_core`, `resolution_contracts`, and `source_connector_sdk` are importable;
- Alembic, boto3, botocore, Playwright, psycopg, Scrapy, and SQLAlchemy are absent from the runtime image.

The worker therefore has no PostgreSQL, migration, crawler, browser, or direct S3 SDK path. Its artifact access remains scoped through the Worker Gateway protocol.

## Scope boundary and next dependency

This proof closes the deterministic Stage 7 computation owner. It does not claim that production observations are already admitted into persisted candidate revisions or that a real Berlin collection run has produced candidates.

The next owner-correct dependency is a privileged resolution-batch admission path that:

- selects exact normalized observation artifacts and exact candidate revisions;
- verifies campaign, geography, resolver, and quality policy identities;
- persists batch lineage and semantic idempotency;
- creates the `entity_resolution` work unit and exact input-artifact binding atomically;
- rejects unsupported or divergent replay before the untrusted worker receives a lease.
