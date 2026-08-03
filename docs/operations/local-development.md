# Local development

## Bootstrap

```text
uv sync --all-packages --dev
python tools/git_hooks/configure.py
```

## Narrow verification

```text
uv run collector config validate berlin_recording_services
uv run pytest packages/collection_application/tests packages/collection_infrastructure/tests
uv run python tools/architecture_checks/check_dependencies.py
```

## Full current-slice verification

```text
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run python tools/architecture_checks/check_dependencies.py
```

No command in this slice performs network acquisition or writes runtime state.
