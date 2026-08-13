# Browser worker

Status: in development

Specification: `docs/specifications/stage-9-browser-worker-v1.md`

## Owners

- `packages/browser_contracts`: explicit browser authorizations, request policy, allowed origins, rendered-document metadata, diagnostics, and result contracts.
- `packages/browser_security`: canonical origin validation, DNS/IP classification, SSRF enforcement, URL redaction/digests, and request budgets.
- `connectors/playwright_browser`: Playwright launch, interception, one-context-per-task isolation, rendered DOM capture, and browser fixtures.
- `apps/browser_worker`: Worker Gateway composition, exact source-bound lease validation, failure mapping, artifact publication, and completion.

Work Engine owns scheduling, source capacity, retries, lease expiry, and crash recovery. Object Store owns immutable request/result artifacts. The HTTP worker remains the ordinary acquisition owner and cannot import Stage 9 packages.

## Contracts

- stage: `acquisition`;
- capability: `browser_fetch`;
- input role: `browser_request`;
- input contract: `browser-acquisition-request@1` / `browser-acquisition-request-v1`;
- expected output contract: `browser-acquisition-result@1`;
- output roles: `browser_rendered_document`, `browser_acquisition_metadata`.

`browser_fetch` is source-capable. The request source key and source-policy digest must equal the exact Work Engine lease. Every request requires a non-expired immutable authorization for the exact target, source, policy, and allowed robots decision.

## Escalation invariants

- valid reasons are only `javascript_required` and `operator_approved_dynamic_content`;
- HTTP 403/429, robots denial, source disablement, and source-budget exhaustion do not authorize browser work;
- Stage 9 never watches HTTP outcomes or enqueues work;
- a higher-level owner must explicitly create a new immutable `browser_fetch` work unit;
- the HTTP worker image remains Playwright-free.

## Security invariants

- every navigation, redirect, frame, subresource, XHR/fetch, worker, and socket request is intercepted before continuation;
- only exact approved HTTP(S) origins and resource types are allowed;
- IP literals and DNS results in non-global address space are blocked;
- main-document policy failure aborts the work; blocked subresources produce explicit incomplete metadata;
- request and redirect limits fail closed;
- no userinfo, wildcard origins, persistent profiles, downloads, granted permissions, or cross-task storage;
- every lease receives a fresh browser context and the context closes in `finally`;
- no `--no-sandbox` or equivalent launch flag;
- runtime user is `10001:10001`.

## Physical boundaries

```text
browser_worker -> playwright_browser
browser_worker -> browser_contracts
browser_worker -> browser_security
browser_worker -> source_connector_sdk
playwright_browser -> browser_contracts
playwright_browser -> browser_security
playwright_browser -> playwright
browser_security -> browser_contracts
browser_contracts -> pydantic
```

Forbidden in the browser worker image: Scrapy, Alembic, boto3/botocore, psycopg, SQLAlchemy, database migrations, collection/review infrastructure, ordinary HTTP connector, extraction core, and entity-resolution core.

## Lifecycle

```text
explicit authorized source-bound browser work
-> exact request artifact
-> lease/source/policy/authorization/robots validation
-> fresh non-persistent browser context
-> interception installed
-> bounded navigation and rendering
-> rendered document + metadata artifacts
-> exact Work Engine completion
-> context cleanup in finally
```

No local escalation queue, scheduler, cookie jar, browser profile, or rendered-page cache is canonical.

## Proof

Completion requires capability/migration proof; authorization and no-auto-escalation tests; direct and DNS SSRF tests; redirect/subresource interception; request budgets; popup/download restrictions; cross-task cookie isolation; rendered artifact/digest tests; HTTP image negative inventory; non-root sandboxed Chromium fixture execution; architecture checks; frozen lock; Ruff; strict mypy; unit and browser integration tests; compilation; permanent `Verify`; and permanent browser-worker isolation proof.
