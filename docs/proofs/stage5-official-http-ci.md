# Stage 5 official HTTP acquisition CI proof

## Proven subject

- Status: `proven`
- Repository: `rtao-god/data-collection-platform`
- Branch: `main`
- Subject commit: `de260cdfde3fb4fca60d1f30a8332baf64256fe7`
- Production owner commit: `de70ba0f6f4ebff756dd4227bd2eafeda338e326`
- Permanent workflow: `Verify`
- Workflow run: `31773798430`
- Conclusion: `success`

This document records evidence for the exact subject commit. It does not replace the production contracts, tests, generated schemas, or workflow definitions.

## Owner gates

The `verify` job (`94684943404`) completed successfully and proved:

- frozen workspace restoration from the committed `uv.lock`;
- lock consistency and generated-contract drift absence;
- Ruff formatting and lint;
- strict mypy over 101 owned source files;
- 252 non-integration tests passed, with 26 integration-scoped tests deselected;
- fail-closed dependency-boundary enforcement;
- Python compilation for apps, connectors, database, packages, and tools;
- deterministic validation of the Berlin campaign snapshot;
- collector CLI, HTTP worker, migration, and Worker Gateway image builds.

The `migration` job (`94684943443`) completed successfully and proved fresh PostgreSQL/PostGIS migration plus the complete database integration contract for the implemented owners.

## Stage 5 owner result

The proven subject contains:

- `official_http` as the owner of strict request and acquisition-manifest contracts;
- deterministic HTTP(S) URL normalization and same-origin planning;
- fail-closed public-address validation before connection and connected-address verification;
- robots and sitemap interpretation, page-interest prioritization, and DTD-safe XML parsing;
- one fresh Scrapy child process per leased request, without `JOBDIR`, redirect following, cookies, or a second canonical queue;
- separate encoded and decoded response-size limits and bounded decompression;
- exact conditional-request identity and typed `304` reuse of the previous leased raw artifact;
- typed redirect, not-found, policy-blocked `403`, bounded transient `429`, transient `5xx`, and contract-invalid outcomes;
- `http_worker` as the Worker Gateway composition owner for heartbeat, scoped reads, verified uploads, and typed completion/failure;
- generated `official-http-request` and `official-http-acquisition` JSON Schemas.

## Remaining boundary

This proof does not claim a live external crawl or Berlin coverage. The campaign remains fail-closed because the reviewed official Berlin boundary artifact is absent. A live run and coverage report belong to the later operational-hardening stage.
