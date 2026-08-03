# Foundation architecture

## Result

The repository starts with executable owners rather than empty deployable shells:

```text
entrypoints
    ↓
configuration       application (next owner batch)
    ↓                    ↓
shared ←──────────── domain
                         ↑
              infrastructure (next owner batch)
```

Current physical owners:

- `shared/contracts.py` — UTC, canonical JSON, SHA-256 and base typed violation contracts;
- `domain/model.py` — collection-run and leased-work lifecycle invariants;
- `configuration/compiler.py` — strict campaign source contracts and deterministic bundle compilation;
- `entrypoints/cli.py` — administrative composition only;
- `tools/check_architecture.py` — executable import-boundary policy.

The domain layer is framework-free. It does not import storage, transport, configuration, web frameworks or queue clients.

## Options evaluated

### Empty service topology

Creating Control API, Worker Gateway, scheduler, browser worker and storage projects immediately would reproduce the target directory tree but leave compile-only shells. It would not prove ownership, lifecycle or data integrity.

### Framework-free owner foundation — selected

Implement the domain state machines, campaign artifact contract and dependency checker first. This is the smallest slice that makes invalid transitions and unowned campaign inputs explicitly detectable while remaining reusable by later API and worker adapters.

### Full database/API/worker vertical slice

A complete vertical slice requires approved infrastructure decisions, migrations, object-store compatibility, queue semantics and at least one legally reviewed real source policy. Those inputs are not represented by repository artifacts yet. Implementing them now would either guess contracts or introduce fake production paths.

## Collection run contract

A run is bound to one immutable `campaign_bundle_sha256`. The lifecycle is strict:

```text
created → running → succeeded
    │         │  └→ failed
    │         └→ cancelling → cancelled
    ├──────────────────────→ failed
    └──────────────────────→ cancelled
```

Terminal state cannot be overwritten. All domain timestamps are explicit aware UTC values and must be monotonic. Missing timezone information is a contract error; it is never normalized implicitly.

## Work-unit contract

A work unit starts in `ready`, has a finite attempt budget and can be mutated only by its current lease owner. Lease identity includes worker id and opaque token. An expired lease cannot report success or failure. Expiration or retry release returns the unit to `ready` only while attempt budget remains; otherwise the unit becomes explicitly `failed`.

No queue, retry loop or database adapter may redefine these meanings.

## Campaign source contract

A campaign directory is source material, not executable state. Compilation requires:

```text
campaign.json
geography.geojson
sources/*.json
seeds.ndjson
```

Compilation rejects:

- unknown or missing object fields;
- unsupported schema versions and enum values;
- absolute, parent-traversing, non-portable or escaping paths;
- symbolic-link source artifacts;
- unapproved or robots-blocked source policies;
- missing request budgets, rate limits or exact allowed hosts for network sources;
- seed records without evidence-bearing references;
- invalid GeoJSON geometry, coordinates or open rings;
- NaN and infinite JSON numbers.

The compiler does not repair or enrich source material. It emits one canonical JSON document with an exact source manifest and a SHA-256 identity calculated by the shared digest owner. Output replacement is atomic.

## Real campaign gate

No Berlin polygon, source approval, terms-review date, source budget or seed is included in production configuration. Those artifacts must be added only from verified evidence and explicit source-policy decisions. Unit-test fixtures are synthetic and are named as such; they are not real-source coverage.

## Proof

The foundation is designed to be checked by:

1. Ruff over repository-owned Python;
2. mypy in strict mode over production source and architecture tooling;
3. executable AST dependency-boundary validation;
4. negative and positive `unittest` contracts;
5. Python bytecode compilation;
6. GitHub Actions on `main` and pull requests.
