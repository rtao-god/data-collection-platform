FROM python:3.13.14-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64 AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.10.0@sha256:78a7ff97cd27b7124a5f3c2aefe146170793c56a1e03321dd31a289f6d82a04f /uv /uvx /bin/

WORKDIR /workspace
COPY pyproject.toml uv.lock ./
COPY apps/ apps/
COPY connectors/ connectors/
COPY packages/ packages/
RUN uv sync --frozen --no-dev --package control-api --no-editable

FROM python:3.13.14-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64 AS runtime

ENV PATH="/workspace/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 control-api \
    && useradd --uid 10001 --gid control-api --create-home control-api
WORKDIR /workspace
COPY --from=build --chown=control-api:control-api /workspace/.venv /workspace/.venv
USER 10001:10001

EXPOSE 8080
ENTRYPOINT ["uvicorn", "control_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
