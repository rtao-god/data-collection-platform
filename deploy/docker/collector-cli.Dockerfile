FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN useradd --create-home --uid 10001 collector
COPY --from=ghcr.io/astral-sh/uv:0.10.0@sha256:78a7ff97cd27b7124a5f3c2aefe146170793c56a1e03321dd31a289f6d82a04f /uv /uvx /bin/

WORKDIR /workspace
COPY pyproject.toml uv.lock ./
COPY apps/collector_cli/ apps/collector_cli/
COPY packages/collection_application/ packages/collection_application/
COPY packages/collection_contracts/ packages/collection_contracts/
COPY packages/collection_domain/ packages/collection_domain/
COPY packages/collection_infrastructure/ packages/collection_infrastructure/
RUN uv sync --frozen --no-dev --all-packages

COPY campaigns/ campaigns/
RUN chown -R collector:collector /workspace
USER collector

ENTRYPOINT ["/workspace/.venv/bin/collector"]
