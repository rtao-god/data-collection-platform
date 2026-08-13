# OSM Overpass HTTP acquisition

The Overpass client accepts only an operator-owned `OverpassEndpointPolicy`; individual work items cannot select an endpoint or provide arbitrary Overpass QL.

The boundary enforces:

- HTTPS and an exact allowlisted interpreter path;
- an exact, sorted host allowlist;
- DNS resolution to globally routable addresses before every request;
- connected-peer address revalidation after connection establishment;
- disabled redirects;
- explicit `User-Agent`, JSON response type, request timeout, and decompressed response byte limit;
- typed classification of transient, permanent, policy-blocked, and contract-invalid failures;
- no upstream body or generated query in error text.

The client does not use Nominatim and does not have PostgreSQL, Worker Gateway, or object-store credentials. Source capacity, pacing, retries, and lease ownership remain Work Engine responsibilities.
