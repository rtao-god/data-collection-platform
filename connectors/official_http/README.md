# Official HTTP connector

`official_http` owns deterministic and bounded official-website request semantics for Stage 5.

It validates and normalizes HTTP(S) URLs, rejects non-public targets, builds explicit conditional headers, executes one Scrapy request in an isolated child process, and interprets robots, sitemap, and same-origin HTML link candidates. It does not persist a crawl queue, follow redirects, retry locally, mutate source budgets, access the database, or access Object Store directly.

The canonical scheduler, lease lifecycle, retry budget, and source permit remain in Work Engine. `apps/http_worker` transfers input and output artifacts only through `source_connector_sdk` and Worker Gateway.

The wire contracts and exact failure semantics are fixed by `docs/specifications/stage-5-official-http-acquisition-v1.md`.
