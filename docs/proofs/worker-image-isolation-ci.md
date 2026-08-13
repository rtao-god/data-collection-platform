# Worker image isolation CI proof

## Proven subject

- Status: `proven`
- Repository: `rtao-god/data-collection-platform`
- Branch: `main`
- Subject commit: `83de76941590e422ec2378e69899f773609b704d`
- Permanent workflow: `Worker Image Isolation`
- Workflow run: `31736560680`
- Event: `push`
- Conclusion: `success`

This document records evidence for the exact subject commit. The workflow remains permanent and is triggered by changes to either worker, its owned packages, the exact lock inputs, or either worker Dockerfile.

## Manual import worker

Job `manual-import-worker` (`94569492629`) completed successfully and proved:

- `deploy/docker/manual-import-worker.Dockerfile` builds from the frozen workspace;
- runtime user is exactly `10001:10001`;
- runtime entrypoint is exactly `manual-import-worker`;
- `manual_import_worker` and `source_connector_sdk` are importable;
- the runtime image does not contain Alembic, boto3, botocore, Playwright, psycopg, Scrapy, or SQLAlchemy.

## OSM worker

Job `osm-worker` (`94569492608`) completed successfully and proved:

- `deploy/docker/osm-worker.Dockerfile` builds from the frozen workspace;
- runtime user is exactly `10001:10001`;
- runtime entrypoint is exactly `osm-worker`;
- `osm_worker` and `source_connector_sdk` are importable;
- the runtime image does not contain Alembic, boto3, botocore, Playwright, psycopg, Scrapy, or SQLAlchemy.

## Boundary result

The two workers have separate capability-minimal runtime images. Neither image contains database, migration, browser, crawler, or S3 SDK ownership. Their only artifact access path remains the scoped Worker Gateway/Object Store protocol exposed through `source_connector_sdk`.

This proof does not claim browser sandboxing, read-only root filesystems, or runtime network-policy enforcement; those belong to later deployable hardening and browser-worker stages.
