# Data Collection Platform

Universal collection platform for discovering, acquiring, extracting, normalizing, resolving,
reviewing, and exporting evidence-backed data from external sources.

The first configured campaign targets recording studios, producers, engineers, and adjacent
recording services in Berlin. Berlin- and audio-specific meaning is kept in campaign data, not in
platform packages.

## Current implementation slice

The repository currently implements the first executable foundation slice:

- strict declarative campaign contracts;
- filesystem campaign-boundary validation;
- duplicate-key-safe YAML parsing;
- exact CSV seed contract validation;
- cross-document reference validation;
- deterministic canonical JSON and SHA-256 campaign snapshot identity;
- typed owner-context error envelopes;
- work-unit state-transition rules;
- import-boundary architecture checks;
- tests, Git hooks, and CI bootstrap.

This is not yet the complete platform. Database migrations, Worker Gateway, acquisition workers,
object storage, entity resolution, review console, and sealed export materialization remain later
implementation stages. The Berlin boundary is intentionally not fabricated; the campaign remains
explicitly blocked for production runs until an approved polygon artifact is added.

## Requirements

- Python 3.13
- `uv`
- Git with `core.hooksPath=.githooks`

## Bootstrap

```text
uv sync --all-packages --dev
python tools/git_hooks/configure.py
```

## Commands

```text
uv run collector config validate berlin_recording_services
uv run collector config digest berlin_recording_services
uv run pytest
uv run python tools/architecture_checks/check_dependencies.py
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

The CLI reads only the allowlisted campaign directory under `campaigns/`. It does not create a run,
write runtime state, or perform network acquisition.

## Repository boundary

This repository owns collector state and collector exports. It must not write to a future catalog
database or define public listing, ranking, billing, booking, or SEO meaning. See
`docs/architecture/system-boundary.md`.
