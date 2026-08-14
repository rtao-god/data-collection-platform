FROM python:3.13.14-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /workspace

RUN pip install --no-cache-dir uv==0.10.0

COPY .python-version pyproject.toml uv.lock ./
COPY apps/resolution_worker ./apps/resolution_worker
COPY packages/entity_resolution_core ./packages/entity_resolution_core
COPY packages/quality_core ./packages/quality_core
COPY packages/resolution_contracts ./packages/resolution_contracts
COPY packages/source_connector_sdk ./packages/source_connector_sdk
COPY packages/collection_contracts ./packages/collection_contracts

RUN uv sync \
    --frozen \
    --no-dev \
    --no-editable \
    --package resolution-worker

FROM python:3.13.14-slim AS runtime

ENV PATH="/workspace/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid worker --create-home worker

WORKDIR /workspace
COPY --from=build --chown=worker:worker /workspace/.venv /workspace/.venv

USER 10001:10001

ENTRYPOINT ["resolution-worker"]
