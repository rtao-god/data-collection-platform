# Berlin live collection runbook

## Scope

This runbook operates the exact `berlin_recording_services` campaign through the production
Data Collection Platform topology. It does not create source facts, replace campaign provenance, or
enable recurring collection automatically.

The canonical closure evidence is:

- `docs/proofs/project-end-to-end-closure.md`;
- `docs/proofs/end-to-end-operational-ci.md`;
- `docs/proofs/berlin-live-collection.json`;
- `campaigns/berlin_recording_services/geography/berlin-boundary.provenance.json`;
- `campaigns/berlin_recording_services/source-inputs.provenance.json`.

## Safety prerequisites

Before every live run, verify all of the following:

1. the boundary provenance identifies the State of Berlin, an official HTTPS resource, a declared
   license, and a digest matching the exact GeoJSON bytes;
2. the campaign validator succeeds;
3. OSM and official-HTTP bindings are enabled and automatic schedules remain disabled;
4. source policies retain bounded concurrency, rates, intervals, timeouts, retry budgets, and kill
   switches;
5. the permanent `Operational E2E` workflow is green for the code being operated;
6. operator credentials and object-store/database credentials are supplied through the deployment
   environment, never committed configuration.

Do not run the campaign when any prerequisite is ambiguous. Record a typed blocker instead.

## Reproduce the bounded proof

From a clean checkout of `main`:

```bash
uv sync --frozen --all-packages --dev
uv run collector config validate berlin_recording_services
uv run pytest -q tools/live_collection/tests
uv run python tools/live_collection/run_bounded_live_proof.py \
  --repository-root "$PWD" \
  --compose-file deploy/compose/application.yaml \
  --startup-timeout-seconds 420 \
  --run-timeout-seconds 1200 \
  --report docs/proofs/berlin-live-collection.local.json
```

The runner builds and starts the complete application Compose topology, discovers the generated
Control API contract, creates one campaign run, waits for terminal state, requests deterministic
export materialization and verification, and then inspects persistent PostgreSQL evidence.

A successful process exit requires all of the following to be positive:

- raw Object Store artifacts represented by `sources.artifact_objects.artifact_kind = raw_artifact`;
- succeeded work units;
- candidate rows;
- sealed or verified export rows.

A successful empty run is not accepted.

## GitHub bounded run

The permanent/manual workflow is `.github/workflows/berlin-bounded-live-run.yml`. It additionally
captures:

- resolved Compose service state;
- complete service logs;
- the live proof JSON;
- object-store-related Compose volumes as compressed workflow artifacts.

The workflow artifact retention is evidence retention for the bounded run; it is not the platform's
canonical Object Store retention policy.

## Inspect the result

Read `docs/proofs/berlin-live-collection.json` and verify:

- `campaignKey` is `berlin_recording_services`;
- `runState` is terminal-successful;
- `runId` and `exportId` are non-empty;
- raw artifact, succeeded work, candidate, and sealed export counts are positive;
- coverage has no blocked, failed, or dead-letter owner;
- source-specific failures, when present, are visible and do not disappear into an empty success.

For raw evidence, use the workflow artifact or the configured S3-compatible Object Store. Do not
copy raw bodies into Git.

## Retry and interruption

The Collection DB is the checkpoint owner. After interruption:

1. restore PostgreSQL and Object Store from the same backup generation;
2. start migration and control-plane services before workers;
3. verify schema head and object-store readiness;
4. inspect expired work and pipeline advancement leases;
5. resume the existing run through Control API rather than creating a duplicate run;
6. verify semantic work identities, artifact digests, and export identity remain unchanged.

Never repair a run by editing worker tables or object metadata manually.

## Source kill switch

Pause the affected source through the Control API before investigating repeated failures. A source
pause must prevent new source permits without invalidating immutable raw evidence or unrelated
sources. Resume only after the policy/config snapshot explicitly records the approved correction.

## Recurring schedule approval

Recurring collection is intentionally disabled in the source-input provenance. Enabling it is a
separate operational decision and requires:

- an identified operator and approval record;
- confirmed source terms and robots/politeness policy;
- measured source budgets from bounded runs;
- alerting for error rate, dead letters, lease expiry, storage growth, and coverage regression;
- retention and backup capacity for the approved cadence;
- a rollback procedure that disables scheduling without deleting evidence.

Do not infer approval from the existence of a successful bounded run.
