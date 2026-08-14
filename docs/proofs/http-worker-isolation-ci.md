# HTTP worker image isolation CI proof

## Proven subject

- Status: `proven`
- Repository: `rtao-god/data-collection-platform`
- Branch: `main`
- Subject commit: `de260cdfde3fb4fca60d1f30a8332baf64256fe7`
- Permanent workflow: `HTTP Worker Isolation`
- Workflow run: `31773798409`
- Job: `94684943406`
- Conclusion: `success`

This document records evidence for the exact subject commit. The permanent workflow is retriggered by changes to the HTTP worker, official HTTP connector, source SDK, relevant contracts, lock inputs, or HTTP-worker Dockerfile.

## Proven image boundary

The job completed successfully and proved:

- `deploy/docker/http-worker.Dockerfile` builds from the frozen workspace;
- runtime user is exactly `10001:10001`;
- runtime entrypoint is exactly `http-worker`;
- `http_worker`, `official_http`, `scrapy`, and `source_connector_sdk` are importable;
- Alembic, boto3, botocore, Playwright, psycopg, and SQLAlchemy are absent from the runtime image.

The HTTP worker therefore has Scrapy acquisition capability without database, migration, browser, or S3 SDK ownership. Its only platform state and artifact path remains the scoped Worker Gateway protocol implemented by `source_connector_sdk`.

## Scope boundary

This proof does not claim runtime egress policy, read-only root filesystem, seccomp, or production resource limits. Those remain deployment-hardening concerns and must be proven in the Compose/security profile rather than inferred from package inventory.
