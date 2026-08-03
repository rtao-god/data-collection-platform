# Data Collection Platform agent routing

## Project scope

This repository owns collection-source policy, campaign snapshots, durable collection work,
raw evidence, typed observations, candidate resolution, quality/review state, and sealed
collector exports. It does not own public catalog listings, SEO, billing, booking, publication,
or the future Aggregator Platform database.

## Repository root

- Repository: `data-collection-platform`
- Root: resolve the current Git checkout; do not assume a drive path.
- Active branch: use the branch already selected by the user. Do not create or switch branches.

## Required shared rules

Read the applicable shared owners before changing the repository:

1. `D:\tools\Agents_Rules\General_Rules.md`
2. `D:\tools\Agents_Rules\Backend_Rules.md` for backend, data, API, worker, pipeline, report, or runtime work.
3. `D:\tools\Agents_Rules\Git_Rules.md` for any Git state change.
4. `D:\tools\Agents_Rules\General_AGENT_INSTRUCTIONS_CHANGE_RULES.md` before changing agent rules or routing.

Frontend rules are not routed by this file until review-console implementation begins.

## Project identity

- Python 3.13 and one `uv` workspace own backend and pipeline dependencies.
- Campaign configuration is declarative data, never executable code.
- Workers must not receive Collection DB credentials; Worker Gateway is the future runtime boundary.
- Database migrations run only through the dedicated migration composition root.
- The current implementation must fail explicitly on incomplete or invalid owner state.
- A collector-owned sealed export is the repository boundary; no temporary Aggregator DTO is allowed.

## Project-specific paths

- Campaign authoring: `campaigns/`
- Python deployables: `apps/`
- Python owner packages: `packages/`
- Database schema and migration history: `database/`
- Generated machine-readable contracts: `contracts/`
- Architecture and operator documentation: `docs/`
- Repository checks and generators: `tools/`
- Temporary local artifacts: `.tmp/` only; never commit them.
