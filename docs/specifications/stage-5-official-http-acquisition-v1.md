# Stage 5 — Official website HTTP acquisition

Specification identity: `stage-5-official-http-acquisition-v1`

This specification is immutable. A material owner, contract, lifecycle, or failure-semantics change requires a replacement specification rather than editing this file to match implementation drift.

## 1. Result and ownership

Stage 5 adds a capability-minimal official-website HTTP acquisition worker. The Work Engine remains the only canonical scheduler and retry owner. The worker processes exactly one leased HTTP request at a time and may not persist or recover crawl state through Scrapy `JOBDIR`, a local scheduler database, a filesystem queue, or process memory.

Production owners:

- `connectors/official_http` owns request validation, URL normalization, robots and sitemap interpretation, deterministic page-interest planning, selected response metadata, and one-request Scrapy execution.
- `apps/http_worker` owns Worker Gateway composition, lease validation, artifact transfer, completion/failure mapping, and the long-running worker loop.
- `source_connector_sdk` remains the only worker-side access path to leases and artifacts.
- Work Engine and source-capacity persistence remain canonical for work state, retry budget, lease expiry, source permits, and restart recovery.
- Object Store remains canonical for raw response bytes. The worker has no database or S3 credentials.

The worker does not own extraction, entity resolution, browser escalation, or crawl-frontier persistence.

## 2. Dependency direction

Allowed production dependencies:

```text
http_worker
  -> official_http
  -> source_connector_sdk

official_http
  -> Scrapy
  -> Python standard library
```

Forbidden dependencies for the HTTP worker image include Alembic, boto3, botocore, Playwright, psycopg, SQLAlchemy, database migrations, collection infrastructure, and direct Object Store clients.

`official_http` must not import Worker Gateway or platform persistence code. `http_worker` must not import database or Object Store implementations.

## 3. Work input contract

The input artifact role is `http_request`. Its canonical JSON contract is `official-http-request@1`.

Required fields:

- `contract`: exactly `official-http-request`;
- `contractRevision`: exactly `official-http-request-v1`;
- `sourceKey`: must equal the lease source key;
- `requestKind`: `robots`, `sitemap`, or `page`;
- `url`: absolute HTTP(S) URL;
- `allowedOrigin`: normalized scheme/host/port boundary;
- `userAgent`: non-empty configured crawler identity;
- `timeoutSeconds`: bounded request timeout;
- `maximumResponseBytes`: bounded decoded body size;
- `interestPolicy`: explicit deterministic candidate-scoring and limit contract.

Optional conditional request fields:

- `etag`;
- `lastModified`;
- `priorArtifactId`;
- `priorContentDigest`.

`priorArtifactId` and `priorContentDigest` are both required or both absent. A page or sitemap request that is disallowed by the previously admitted robots decision must not be leased as runnable work; the worker nevertheless fails closed if `robotsAllowed` is explicitly false.

The worker accepts `GET` only. Credentials, URL fragments, unsupported schemes, malformed ports, overlong URLs, and non-public literal or resolved addresses are rejected before network I/O.

## 4. URL and origin semantics

Normalization is deterministic:

- lowercase scheme and host;
- IDNA host normalization;
- remove default ports;
- remove fragments;
- normalize dot segments and duplicate path separators;
- preserve path case;
- sort query pairs deterministically;
- remove configured tracking parameters only when the input interest policy explicitly lists them;
- normalize an empty path to `/`.

Redirect following is disabled. A redirect is emitted as an acquisition outcome with its selected `Location` metadata; it is never followed inside the same lease. Any follow-up URL requires a new Work Engine work unit after policy admission.

`allowedOrigin` is enforced before request dispatch and on every discovered URL. Cross-origin candidates are retained only as rejected planning evidence, never as runnable child requests.

## 5. Scrapy execution boundary

Every lease invokes an isolated one-request Scrapy crawl. The persistent worker process launches a child Python process for the Scrapy reactor so multiple leases do not attempt to restart Twisted in one interpreter.

The child crawl has these invariants:

- exactly one outbound request;
- `JOBDIR` is unset;
- cookies are disabled;
- redirects are disabled;
- retries are disabled because Work Engine owns retry state;
- download timeout and maximum decoded response size come from the validated request contract;
- response bodies are written only to an ephemeral worker-local file and are uploaded or discarded before completion;
- selected headers and status are returned through a bounded JSON result;
- no discovered URL is scheduled locally.

Scrapy is the HTTP execution engine, not the canonical scheduler.

## 6. Robots, sitemap, and page-interest planning

Robots acquisition is an explicit `robots` work unit. Its body is parsed deterministically for the configured user agent. The output records:

- robots policy digest;
- applicable allow/disallow decision evidence;
- normalized sitemap candidates;
- parse warnings without silently converting malformed policy to allow-all.

Sitemap acquisition is an explicit `sitemap` work unit. XML sitemap and sitemap-index documents are parsed with entity expansion disabled. Candidate URLs are normalized, constrained to `allowedOrigin`, deduplicated, scored, and truncated by the explicit `interestPolicy` limit.

HTML page responses may emit normalized same-origin link candidates. Planning is deterministic and does not enqueue locally. Candidate order is descending score, then canonical URL. The output records rejected cross-origin, invalid, disallowed, and over-limit candidates as counts rather than runnable work.

The interest policy contains explicit positive path tokens, negative path tokens, query parameters to remove, maximum depth, and maximum candidates. No hidden global ranking or latest-wins behavior is allowed.

## 7. Acquisition output contract

The expected Work Engine output contract is `official-http-acquisition@1`.

The manifest artifact role is `http_acquisition_manifest`. The manifest contract is `official-http-acquisition-manifest-v1` and contains:

- requested and canonical URL;
- request kind and source key;
- HTTP status;
- outcome: `fetched`, `not_modified`, or `redirect`;
- selected response headers;
- body content digest when a new body was fetched;
- reused prior artifact identity and digest for `not_modified`;
- robots/sitemap/page planning result as applicable;
- deterministic manifest digest inputs.

For a successful body response, raw bytes are uploaded as `raw_artifact` with role `http_raw_response`, then the manifest is uploaded as `diagnostic_artifact`.

For HTTP `304`, the worker uploads no replacement body. It validates that the request supplied both prior artifact identity and prior digest, records those exact values in the manifest, and completes with the manifest only. Downstream acquisition resolution therefore reuses the prior raw artifact identity rather than treating an empty `304` body as new content.

Output digest is derived from the expected output contract, canonical manifest bytes, and either the new body digest or the reused prior digest.

## 8. Failure and source-budget semantics

Failures are classified before calling Worker Gateway:

- invalid contract, URL, origin, robots denial, unsupported content semantics, or unsafe address: `permanent` or `policy_blocked` with a stable result code;
- HTTP `403`: `policy_blocked` with `HTTP_FORBIDDEN`;
- HTTP `429`: `transient` with `HTTP_RATE_LIMITED`; a valid `Retry-After` value is preserved in the required action and acquisition diagnostics;
- timeout, DNS failure, connection failure, and HTTP `5xx`: `transient`;
- response exceeding the configured limit: `permanent` with `HTTP_RESPONSE_TOO_LARGE`;
- unexpected worker defect: fail closed as `permanent` only after preserving a bounded diagnostic code; secrets and response bodies must not enter failure messages.

The worker never sleeps to own source rate limiting and never mutates a local source budget. Work Engine/source-capacity state decides the next permit and retry time. The worker verifies that source-bound work has a source key and uses only the permit-bearing lease supplied by Worker Gateway.

## 9. Concurrency, crash, and restart

The worker registers `http_fetch` with maximum concurrency `1` and supports only `official-http-acquisition@1`.

A crash before completion leaves the Work Engine lease to expire. Ephemeral body files are not canonical and may be discarded. A stale worker cannot complete because Worker Gateway validates lease identity and token. On restart, the worker registers again and acquires the next DB-owned runnable work unit. No Scrapy scheduler state is restored or consulted.

Completion order is:

1. validate lease and input artifact;
2. execute one bounded request;
3. upload and verify new raw body when applicable;
4. upload and verify the canonical manifest;
5. complete the exact lease with output bindings and output digest.

A crash before step 5 cannot publish a successful Work Engine result. Existing orphan-upload cleanup owns uncommitted staged objects.

## 10. Security and boundedness

- HTTP(S) only;
- no URL credentials or fragments;
- public IP literals only;
- hostname resolution must reject loopback, private, link-local, multicast, unspecified, reserved, and documentation-only destinations;
- redirects and cookies disabled;
- bounded URL, header, metadata, body, timeout, candidate count, and subprocess output sizes;
- selected headers only; authorization, cookies, and arbitrary server headers are never copied into manifests;
- response body never appears in logs or failure text;
- raw HTML is stored as evidence only and is not executed;
- browser escalation is out of scope and cannot occur automatically.

## 11. Proof

Required automated proof:

- request-contract and canonical JSON validation;
- URL normalization and unsafe-address rejection;
- conditional request header generation;
- deterministic robots, sitemap, and HTML-link planning;
- no local Scrapy scheduler persistence or redirect/retry ownership;
- successful raw body and manifest publication;
- `304` prior-artifact reuse with no raw upload;
- `403`, `429`, timeout, oversized response, and `5xx` failure classification;
- worker registration/acquisition/restart behavior through a fake Worker Gateway;
- architecture dependency checks;
- frozen lock, Ruff, mypy, unit tests, compilation;
- capability-minimal HTTP worker image build and negative dependency inventory;
- permanent GitHub Actions proof on the exact committed head.

## 12. Explicit non-goals

This stage does not implement extraction/normalization, entity resolution, browser rendering, review UI, export, full production deployment policy, or a second crawl scheduler. It does not make the Berlin campaign runnable while its independent geography blocker remains unresolved.
