FROM python:3.13.14-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /workspace

RUN pip install --no-cache-dir uv==0.10.0

COPY .python-version pyproject.toml uv.lock ./
COPY packages/collection_contracts ./packages/collection_contracts
COPY packages/manual_import_core ./packages/manual_import_core
COPY packages/source_connector_sdk ./packages/source_connector_sdk
COPY apps/manual_import_worker ./apps/manual_import_worker

RUN uv sync \
    --frozen \
    --no-dev \
    --no-editable \
    --package manual-import-worker

FROM python:3.13.14-slim AS runtime

ENV PATH="/workspace/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid worker --create-home worker

WORKDIR /workspace
COPY --from=build --chown=worker:worker /workspace/.venv /workspace/.venv

USER 10001:10001

ENTRYPOINT ["manual-import-worker"]
