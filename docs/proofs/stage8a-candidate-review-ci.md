# Stage 8A candidate review CI proof

## Proven subject

- Status: `proven`
- Repository: `rtao-god/data-collection-platform`
- Branch: `main`
- Stage 8A production commit: `d0c3b26a8467ba1deb36c5027b683f1d0892b486`
- Exact CI subject: `de2c44be43be26c65f54c39d41d817fc300afd3f`

The CI subject contains the complete Stage 8A production commit and the permanent workflow correction that tracks migration `20260814_0010_candidate_review_foundation.py`.

## Stage 8A owner workflow

Permanent workflow `Stage 8A Candidate Review Foundation`, run `31826979874`, completed with conclusion `success` on the exact CI subject.

- `contracts-core` job `94853306162` proved frozen workspace restoration, deterministic review-contract drift, review owner tests, and dependency boundaries.
- `fresh-schema` job `94853306181` proved a fresh PostgreSQL/PostGIS migration and the candidate/review schema integration contract.

## Repository workflow

Permanent workflow `Verify`, run `31826979907`, completed with conclusion `success` on the exact CI subject.

- `verify` job `94853306467` proved lock consistency, generated-contract drift, Ruff formatting and lint, strict mypy, the complete non-integration suite, dependency boundaries, Python compilation, campaign validation, and all configured deployable image builds.
- `migration` job `94853306534` proved a fresh PostgreSQL/PostGIS migration and the repository database integration contract.

## Owner boundary established

Stage 8A now has:

- versioned candidate, review-decision, manual-observation, and suppression contracts in `review_contracts`;
- pure optimistic-concurrency and immutable-supersession transitions in `review_core`;
- deterministic checked-in review JSON Schemas and drift verification;
- append-only candidate, quality, and review persistence in migration `20260814_0010`, linearly following `20260814_0009_derived_artifacts`;
- restrictive foreign keys and negative schema proof for immutable history and unsafe markup.

Runtime PostgreSQL command handling, authenticated Control API endpoints, and the React review console remain downstream Stage 8 owners and are not claimed by this proof.
