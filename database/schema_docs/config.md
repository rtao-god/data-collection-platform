# `config` schema

The first database revision represents only meanings already present in the canonical
`CampaignSnapshot` contract.

## `config_bundles`

Immutable root metadata:

- `bundle_digest` — primary semantic identity in `sha256:<hex>` form;
- `campaign_key`;
- exact snapshot contract and revision;
- `readiness`;
- explicit `recorded_at_utc`.

No timestamp or contract field receives a server-side semantic default. Component and blocker
counts are derived from child rows rather than stored as a second truth.

## `config_bundle_components`

Ordered component identity rows keyed by `(bundle_digest, position)`. Paths are unique within the
bundle, relative, non-empty, and cannot contain a parent-traversal segment. Component digests use the
same SHA-256 wire format as the root.

## `config_bundle_blockers`

Ordered blocker rows keyed by `(bundle_digest, position)`. Code, owner, message, and required action
remain explicit; a blocker is never flattened to a boolean or empty result.

## Atomic seal contract

A bundle is materialized in one transaction:

1. acquire the digest-scoped advisory lock implicitly through the child insert trigger;
2. insert ordered component rows and optional blocker rows;
3. insert the root row last;
4. the root trigger validates contiguous positions, at least one component, and readiness/blocker
   consistency;
5. deferred foreign keys are checked at commit.

The same advisory lock closes the concurrency window between child insertion and root sealing. Once
the root exists, further child inserts fail. All three tables reject update and delete. Corrections
therefore require a new bundle digest and a complete new transaction.

## Deliberately absent

The schema does not yet contain an object key, run, stage, work unit, lease, source state, artifact,
observation, candidate, review, or export table. Those owners are added only with their production
contracts and integration proof.
