# Stage 9 — Browser worker

Specification identity: `stage-9-browser-worker-v1`

This specification is immutable. A material capability, authorization, network policy, browser lifecycle, artifact contract, sandbox, or escalation rule change requires a replacement specification.

## 1. Result and owners

Stage 9 adds one capability-minimal Playwright worker for explicitly authorized JavaScript rendering.

Production owners:

- `packages/browser_contracts` owns browser acquisition requests, explicit escalation authorizations, allowed-origin policy, rendered-document metadata, blocked-request diagnostics, and result contracts.
- `packages/browser_security` owns canonical origin validation, DNS/IP classification, SSRF enforcement, request-budget enforcement, and URL redaction/digests.
- `connectors/playwright_browser` owns Playwright launch, one-context-per-task execution, request interception, popup/download/permission restrictions, rendered DOM capture, and browser fixtures.
- `apps/browser_worker` owns Worker Gateway composition, exact source-bound lease validation, failure mapping, artifact upload, and lease completion.
- Work Engine remains canonical for scheduling, source-capacity permits, retry budget, lease expiry, crash recovery, and stage progression.
- Object Store remains canonical for request and result artifacts. The browser worker has no PostgreSQL or direct S3 credentials.

The HTTP worker remains the canonical ordinary official-site acquisition path. Browser work is exceptional, explicit, separately scheduled work. The HTTP worker must not import browser contracts, Playwright, or browser-runtime code.

## 2. Dependency direction

```text
browser_worker
  -> playwright_browser
  -> browser_contracts
  -> browser_security
  -> source_connector_sdk

playwright_browser
  -> browser_contracts
  -> browser_security
  -> playwright

browser_security
  -> browser_contracts

browser_contracts
  -> pydantic
```

Forbidden in the browser worker image: Alembic, boto3/botocore, psycopg, SQLAlchemy, database migrations, collection infrastructure, Scrapy, the ordinary HTTP connector, extraction core, entity-resolution core, and review infrastructure.

Forbidden in the HTTP worker image: Playwright and every Stage 9 package.

## 3. Work Engine capability migration

A distinct source-capable capability `browser_fetch` is added to the canonical Work Engine contracts.

Stage mapping:

```text
stage = acquisition
capability = browser_fetch
source_key is required
```

The migration must extend the existing stage/capability and source-capability database constraints without weakening any existing capability rule. A non-source `browser_fetch` work unit and a browser capability outside `acquisition` are rejected.

The browser worker registers:

- capability: `browser_fetch`;
- supported output contract: `browser-acquisition-result@1`;
- maximum concurrency: `1`;
- resource profile: `playwright-browser`.

## 4. Explicit browser authorization

Input artifact role: `browser_request`.

Request contract: `browser-acquisition-request@1` / revision `browser-acquisition-request-v1`.

Every request contains one immutable `BrowserEscalationAuthorization` with:

- authorization ID;
- action exactly `browser_fetch`;
- reason exactly `javascript_required` or `operator_approved_dynamic_content`;
- actor reference;
- issued-at and expires-at UTC;
- exact canonical target URL;
- exact source key;
- exact source-policy digest;
- exact robots-decision digest;
- `robotsAllowed = true`;
- canonical authorization digest.

The authorization lifetime is positive and at most 24 hours. The worker rejects expired, future-dated, mutated, robots-denied, source-mismatched, policy-mismatched, or target-mismatched authorization.

Reason codes representing blocking or rate limits are not valid browser authorization reasons. In particular, HTTP `403`, HTTP `429`, robots denial, source disablement, and source-budget exhaustion never authorize browser work.

No Stage 9 component watches HTTP outcomes or enqueues new work. A higher-level owner must explicitly create a new immutable `browser_fetch` work unit after a valid decision. This physically prevents automatic escalation.

## 5. Request policy

The browser request contains:

- source key and source-policy digest;
- exact canonical target URL;
- sorted unique exact allowed origins;
- navigation timeout;
- post-load wait interval;
- wait condition `domcontentloaded` or `load`;
- maximum request count;
- maximum redirect count;
- maximum rendered document bytes;
- sorted allowed resource types;
- explicit authorization.

Allowed origins contain scheme, IDNA-normalized host, and effective port. Wildcards, suffix matching, userinfo, fragments, and unspecified ports are forbidden. The target origin must be present.

The policy is fail-closed. Redirects, frames, scripts, stylesheets, images, XHR/fetch, workers, WebSockets, EventSource, and every other network request pass through the same interception owner before continuation.

## 6. SSRF and request interception

Only `http` and `https` are permitted. Every intercepted URL is rejected when it has:

- userinfo;
- a non-approved exact origin;
- a non-approved resource type;
- an invalid or ambiguous host/port;
- an IP literal or DNS result in loopback, private, link-local, multicast, unspecified, reserved, carrier-grade NAT, IPv6 unique-local, IPv4-mapped private, or other non-global space;
- more DNS addresses than the configured bound;
- a request count exceeding the policy;
- a redirect chain exceeding the policy.

The guard resolves every non-literal host immediately before continuation and validates every returned address. Empty DNS results fail closed. URL queries/fragments are not copied into blocked-request diagnostics; diagnostics store a redacted URL and the digest of the full canonical URL.

The main-document request being blocked fails the work with `policy_blocked`. A blocked subresource is aborted and recorded. The result state becomes `completed_with_blocked_subresources`; incomplete acquisition is never silently represented as fully clean.

## 7. Browser lifecycle and task isolation

The worker may reuse one browser process, but every lease receives a new non-persistent browser context with:

- no storage state input or output;
- no user-data directory;
- no accepted downloads;
- service workers blocked;
- no granted permissions;
- no geolocation, camera, microphone, notifications, clipboard, MIDI, USB, Bluetooth, serial, or filesystem access;
- unexpected pages/popups closed;
- a new page created only after interception is installed.

Cookies, local storage, session storage, IndexedDB, caches, and service-worker state cannot cross contexts. The context is cleared and closed in `finally`, including timeout, policy rejection, browser crash, and upload failure paths.

A test must prove that a cookie set by one task is absent in the next task while the browser process is reused.

## 8. Browser sandbox and container

The deployable image uses the official Playwright Python image whose version exactly matches the locked `playwright` package. Both image tag and content digest are committed.

Runtime requirements:

- non-root user exactly `10001:10001`;
- no `--no-sandbox`, `--disable-setuid-sandbox`, or equivalent escape flag in source, image, or launch options;
- Chromium starts successfully under the non-root user in permanent CI;
- browser cache and temporary directories are owned only by the runtime user;
- no host browser installation or persistent profile mount;
- entrypoint exactly `browser-worker`.

Deployment must retain a Chromium-compatible seccomp profile and must not add broad Linux capabilities. The image proof does not authorize privileged mode.

## 9. Execution and output

The runtime installs interception before navigation, navigates once to the authorized target, waits only according to the bounded policy, captures the final rendered DOM through Playwright, and closes the context.

The rendered document is UTF-8 and bounded by `maximumRenderedDocumentBytes`. Over-limit content fails instead of truncating evidence.

Expected output contract: `browser-acquisition-result@1`.

Output artifact roles:

- `browser_rendered_document`: `raw_artifact`, content type `text/html; charset=utf-8`;
- `browser_acquisition_metadata`: `diagnostic_artifact`, content type `application/vnd.collection.browser-acquisition-metadata+json`.

Metadata contains:

- request and authorization identities/digests;
- source key and source-policy digest;
- requested and final canonical URLs;
- final HTTP status when available;
- redirect count;
- intercepted and blocked request counts;
- bounded sorted blocked-request diagnostics;
- browser engine/version and worker build identity;
- rendered-document digest and size;
- result state `completed` or `completed_with_blocked_subresources`.

The worker output digest is derived from the exact output contract, rendered-document digest, and metadata digest.

## 10. Failure semantics

`policy_blocked`:

- invalid/denied/expired authorization;
- robots not allowed;
- source or policy mismatch;
- SSRF or unapproved origin/resource request for the main document;
- forbidden sandbox launch option;
- request/redirect budget violation.

`permanent`:

- malformed or unsupported request contract;
- artifact identity/digest mismatch;
- rendered document exceeds the configured limit;
- deterministic unsupported browser policy.

`transient`:

- browser process crash;
- bounded navigation timeout;
- temporary DNS failure for an otherwise approved origin;
- temporary browser runtime startup failure.

The worker never retries locally. HTTP status `403` or `429` observed during an already authorized browser task is recorded in metadata and handled by the existing source policy/retry owner; it does not create another escalation.

## 11. Crash and restart

A crash before Work Engine completion leaves the lease to expire. Staged uploads are handled by existing orphan cleanup. Restart creates a new browser process or context, acquires DB-owned work, and recomputes from the immutable request. No local scheduler, cookie jar, rendered-page cache, or escalation queue is canonical.

## 12. Browser fixtures

The repository owns deterministic local fixtures for:

- JavaScript-rendered content;
- redirect chains;
- approved and unapproved subresources;
- cookie set/read behavior;
- popup/download attempts;
- direct and DNS-resolved SSRF targets;
- request-count and redirect-count limits.

Integration tests map fixture hostnames to the local server at browser-launch time while injecting an explicit test resolver that reports globally routable addresses. Production composition has no host-resolver override.

## 13. Proof

Required proof:

- explicit authorization digest, lifetime, source, policy, target, and robots validation;
- block-derived reasons cannot form a valid authorization;
- HTTP worker has no browser imports and its image remains Playwright-free;
- browser capability is accepted only for source-bound acquisition work;
- every request and redirect is intercepted;
- direct-IP and DNS-resolved SSRF cases are blocked;
- unapproved origins/resource types are blocked;
- blocked main document fails; blocked subresources produce explicit incomplete metadata;
- request and redirect budgets fail closed;
- no cross-task cookies or storage state;
- popup/download restrictions;
- rendered DOM and metadata artifact contracts/digests;
- non-root sandboxed Chromium starts in the permanent image workflow;
- frozen lock, generated-contract drift, Ruff, strict mypy, unit tests, browser integration fixtures, architecture checks, compilation, migration tests, image build, and exact-head permanent CI proof.

## 14. Non-goals

Stage 9 does not implement CAPTCHA solving, authentication bypass, anti-bot evasion, proxy rotation, fingerprint spoofing, credentialed browsing, form submission, downloads, screenshots, public browsing APIs, automatic escalation, extraction, entity resolution, review UI, or export. It does not bypass robots, source budgets, source disablement, or the existing Berlin boundary blocker.
