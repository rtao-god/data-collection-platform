# Official website HTTP acquisition

## Owners

- `official_http` owns the strict HTTP request and acquisition-manifest contracts, URL
  normalization, public-address enforcement, robots/sitemap interpretation, page-interest planning,
  conditional-request semantics, bounded decompression, and one-request Scrapy execution.
- `http_worker` owns the capability-specific Worker Gateway composition, lease validation,
  heartbeat, raw/manifest artifact publication, and typed completion/failure mapping.
- `source_connector_sdk` remains the only transport path to leases and artifacts. The HTTP worker
  has no PostgreSQL or object-store credentials.

## Atomic runtime path

One leased `http_fetch` work unit contains one `http_request` artifact and, for a conditional
request, the exact previous raw artifact as a second lease input. The worker runs one fresh Scrapy
child process with no `JOBDIR`, cookies, redirect following, local retry, or persistent scheduler.
The Collection database remains the canonical queue and resume owner.

A successful 2xx response produces an immutable raw artifact and an acquisition manifest. HTTP 304
produces only a manifest that references the exact prior input artifact and digest. Redirects are
recorded but not followed; every target must be scheduled as new policy-validated work. HTTP 403 is
policy-blocked and HTTP 429 is a typed transient failure with bounded Retry-After metadata. Neither
condition creates browser work.

## Security and limits

Only HTTP(S) canonical URLs on the approved origin are representable. DNS answers and the connected
remote address must all be globally routable. User information, private/loopback/link-local/reserved
addresses, non-HTTP schemes, redirect following, cookies, downloads, and unsupported content
encodings are rejected. Encoded and decoded response sizes have separate explicit limits.

## Proof

Contract, URL, planning, fetch-adapter, and worker tests cover deterministic normalization,
duplicate-key rejection, private-address denial, same-origin planning, DTD rejection, local Scrapy
scheduler absence, exact 304 reuse, raw-artifact publication, and 403/429 behavior. Permanent CI
builds the HTTP worker image and a separate isolation workflow proves Scrapy is present while
Playwright, SQL, migration, and S3 SDK dependencies are absent.
