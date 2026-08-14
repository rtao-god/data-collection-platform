FROM python:3.13.14-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /workspace
RUN pip install --no-cache-dir uv==0.10.0
COPY .python-version pyproject.toml uv.lock ./
COPY apps ./apps
COPY connectors ./connectors
COPY packages ./packages
RUN uv sync --frozen --no-dev --no-editable --package control-api

FROM python:3.13.14-slim AS runtime
ENV PATH="/workspace/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 control-api \
    && useradd --uid 10001 --gid control-api --create-home control-api
WORKDIR /workspace
COPY --from=build --chown=control-api:control-api /workspace/.venv /workspace/.venv
USER 10001:10001
ENTRYPOINT ["uvicorn", "control_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
