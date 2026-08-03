# `config` schema

The first database revision represents only meanings already present in the canonical
`CampaignSnapshot` contract.

## `config_bundles`

Insert-only root metadata:

- `bundle_digest` — primary semantic identity in `sha256:<hex>` form;
- `campaign_key`;
- exact snapshot contract and revision;
- `readiness`;
- component and blocker counts;
- explicit `recorded_at_utc`.

A readiness constraint requires zero blockers for `ready` and at least one blocker for `blocked`.
No timestamp or contract field receives a server-side semantic default.

## `config_bundle_components`

Ordered component identity rows keyed by `(bundle_digest, position)`. Paths are unique within the
bundle, relative, non-empty, and cannot contain a parent-traversal segment. Component digests use the
same SHA-256 wire format as the root.

## `config_bundle_blockers`

Ordered blocker rows keyed by `(bundle_digest, position)`. Code, owner, message, and required action
remain explicit; a blocker is never flattened to a boolean or empty result.

## Immutability

All three tables have `BEFORE UPDATE OR DELETE` triggers. Corrections require a new bundle digest and
new rows. Foreign keys use no cascade delete.

## Deliberately absent

The schema does not yet contain an object key, run, stage, work unit, lease, source state, artifact,
observation, candidate, review, or export table. Those owners are added only with their production
contracts and integration proof.
