# Dependency rules

The canonical production-owner registry lives in
`tools/architecture_checks/check_dependencies.py`. The block below is rendered by
`python tools/architecture_checks/check_dependencies.py --print-policy` and is verified for drift
by the normal architecture check.

<!-- dependency-policy:start -->
| Production owner | Project | Allowed internal owners | Allowed external imports |
|---|---|---|---|
| `control_api` | `apps/control_api` | `collection_application`, `collection_contracts`, `collection_infrastructure`, `review_application`, `review_contracts`, `review_infrastructure` | `fastapi`, `pydantic`, `sqlalchemy`, `uvicorn` |
| `collector_cli` | `apps/collector_cli` | `collection_application`, `collection_contracts`, `collection_infrastructure` | `boto3`, `sqlalchemy` |
| `http_worker` | `apps/http_worker` | `official_http`, `source_connector_sdk` | none |
| `processing_worker` | `apps/processing_worker` | `collection_contracts`, `extraction_core`, `normalization_core`, `source_connector_sdk` | `pydantic` |
| `resolution_worker` | `apps/resolution_worker` | `entity_resolution_core`, `quality_core`, `resolution_contracts`, `source_connector_sdk` | none |
| `worker_gateway` | `apps/worker_gateway` | `collection_application`, `collection_contracts`, `collection_infrastructure` | `fastapi`, `pydantic`, `sqlalchemy`, `uvicorn` |
| `collection_migration` | `apps/migration` | `collection_contracts`, `collection_infrastructure` | none |
| `manual_import_worker` | `apps/manual_import_worker` | `collection_contracts`, `manual_import_core`, `source_connector_sdk` | `httpx` |
| `osm_worker` | `apps/osm_worker` | `osm_overpass`, `source_connector_sdk` | none |
| `official_http` | `connectors/official_http` | `source_connector_sdk` | `defusedxml`, `pydantic`, `scrapy` |
| `osm_overpass` | `connectors/osm_overpass` | none | `httpx` |
| `review_application` | `packages/review_application` | `review_contracts` | none |
| `review_contracts` | `packages/review_contracts` | none | `pydantic` |
| `review_core` | `packages/review_core` | `review_contracts` | none |
| `review_infrastructure` | `packages/review_infrastructure` | `review_application`, `review_contracts`, `review_core` | `sqlalchemy` |
| `collection_infrastructure` | `packages/collection_infrastructure` | `collection_application`, `collection_contracts` | `alembic`, `boto3`, `botocore`, `psycopg`, `sqlalchemy` |
| `collection_application` | `packages/collection_application` | `collection_contracts`, `collection_domain`, `manual_import_core` | `pydantic`, `yaml` |
| `extraction_core` | `packages/extraction_core` | `collection_contracts` | `extruct`, `lxml` |
| `normalization_core` | `packages/normalization_core` | `collection_contracts` | `phonenumbers`, `tldextract` |
| `entity_resolution_core` | `packages/entity_resolution_core` | `resolution_contracts` | none |
| `quality_core` | `packages/quality_core` | `resolution_contracts` | none |
| `resolution_contracts` | `packages/resolution_contracts` | none | `pydantic` |
| `manual_import_core` | `packages/manual_import_core` | `collection_contracts` | `pydantic` |
| `source_connector_sdk` | `packages/source_connector_sdk` | `collection_contracts` | `httpx` |
| `collection_domain` | `packages/collection_domain` | none | none |
| `collection_contracts` | `packages/collection_contracts` | none | `pydantic` |
<!-- dependency-policy:end -->

The checker is fail-closed for Python production projects under `apps/`, `packages/`, and
`connectors/`:

- every project with Python source must expose exactly one import-root package;
- every import root must have an explicit owner policy and fixed project path;
- every production project must be a declared `uv` workspace member;
- internal dependencies in each project `pyproject.toml` must exactly match its owner policy;
- the resolved `uv.lock` runtime closure must contain the complete Review owner chain only for
  Control API and must exclude Review owners from Collection Infrastructure, migration, collector
  CLI, and Worker Gateway;
- source imports must stay inside the internal and external allowances above;
- generic production path segments named `utils`, `common`, `helpers`, or `shared_domain` are
  rejected.

Relative imports within one owner are allowed. Python standard-library imports are allowed for
every owner. A new app, package, or connector is blocked until its owner, project path, dependency
direction, and external capabilities are added explicitly in the same change.

Forbidden examples include:

- Domain importing FastAPI, SQLAlchemy, Scrapy, Playwright, Dagster, Infrastructure, or an app;
- Application importing Infrastructure or a composition root;
- Infrastructure importing Domain directly instead of Application ports and Contracts;
- one app importing another app;
- a connector importing another connector, SQLAlchemy models, review, or export owners;
- a project dependency declaration that is broader than its source-level architecture allowance.

CI runs the checker against the complete repository, and its unit suite contains deliberate
unregistered-owner, wrong-path, forbidden-import, dependency-drift, and documentation-drift cases.
