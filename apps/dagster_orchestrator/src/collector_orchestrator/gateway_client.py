from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx

from collection_contracts.pipeline_advancement_api import (
    PipelineAdvancementRunRequest,
    PipelineAdvancementRunResponse,
)

_GATEWAY_PATH: Final = "/internal/pipeline-advancement/run-once"
_MAX_RESPONSE_BYTES: Final = 1_048_576
_MIN_TOKEN_LENGTH: Final = 32


class GatewayClientError(RuntimeError):
    def __init__(self, *, code: str, message: str, context: Mapping[str, object]) -> None:
        self.code = code
        self.context = dict(context)
        super().__init__(message)


def run_pipeline_advancement(
    *,
    dagster_execution_id: str,
    dagster_build_id: str,
    environment: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> PipelineAdvancementRunResponse:
    env = os.environ if environment is None else environment
    base_url = env.get("PIPELINE_ADVANCEMENT_GATEWAY_URL", "")
    token_file = env.get("PIPELINE_ADVANCEMENT_TOKEN_FILE", "")
    _validate_base_url(base_url)
    token = _read_service_token(token_file)
    request = PipelineAdvancementRunRequest(
        contract="worker-gateway-pipeline-advancement-run",
        contract_revision="pipeline-advancement-run-v1",
        dagster_execution_id=dagster_execution_id,
        dagster_build_id=dagster_build_id,
    )
    correlation_id = f"dagster-{uuid5(NAMESPACE_URL, dagster_execution_id)}"

    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = client.post(
                f"{base_url.rstrip('/')}{_GATEWAY_PATH}",
                json=request.model_dump(mode="json", by_alias=True),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "X-Correlation-Id": correlation_id,
                },
            )
    except httpx.RequestError as exc:
        raise GatewayClientError(
            code="PIPELINE_ADVANCEMENT_GATEWAY_UNAVAILABLE",
            message="Worker Gateway did not complete the advancement request.",
            context={"causeType": type(exc).__name__},
        ) from None

    if not 200 <= response.status_code < 300:
        raise GatewayClientError(
            code="PIPELINE_ADVANCEMENT_GATEWAY_REJECTED",
            message="Worker Gateway rejected the advancement request.",
            context={"statusCode": response.status_code},
        )
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise GatewayClientError(
            code="PIPELINE_ADVANCEMENT_RESPONSE_TOO_LARGE",
            message="Worker Gateway returned a response above the client size limit.",
            context={"sizeBytes": len(response.content), "maximumBytes": _MAX_RESPONSE_BYTES},
        )
    content_type = response.headers.get("content-type", "").split(";", maxsplit=1)[0].strip()
    if content_type.casefold() != "application/json":
        raise GatewayClientError(
            code="PIPELINE_ADVANCEMENT_RESPONSE_INVALID",
            message="Worker Gateway returned an unsupported response content type.",
            context={"contentType": content_type or "missing"},
        )

    try:
        return PipelineAdvancementRunResponse.model_validate_json(response.content, strict=True)
    except ValueError as exc:
        raise GatewayClientError(
            code="PIPELINE_ADVANCEMENT_RESPONSE_INVALID",
            message="Worker Gateway returned a payload outside the advancement response contract.",
            context={"causeType": type(exc).__name__},
        ) from None


def _validate_base_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        parsed = None
        port = None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise GatewayClientError(
            code="PIPELINE_ADVANCEMENT_CONFIGURATION_INVALID",
            message="The Worker Gateway base URL is missing or invalid.",
            context={"setting": "PIPELINE_ADVANCEMENT_GATEWAY_URL"},
        )


def _read_service_token(value: str) -> str:
    if not value:
        raise GatewayClientError(
            code="PIPELINE_ADVANCEMENT_CONFIGURATION_INVALID",
            message="The Dagster service token file is not configured.",
            context={"setting": "PIPELINE_ADVANCEMENT_TOKEN_FILE"},
        )
    try:
        token = Path(value).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GatewayClientError(
            code="PIPELINE_ADVANCEMENT_CONFIGURATION_INVALID",
            message="The Dagster service token file could not be read.",
            context={"causeType": type(exc).__name__},
        ) from None
    if len(token) < _MIN_TOKEN_LENGTH:
        raise GatewayClientError(
            code="PIPELINE_ADVANCEMENT_CONFIGURATION_INVALID",
            message="The Dagster service token does not meet the minimum length.",
            context={"minimumLength": _MIN_TOKEN_LENGTH},
        )
    return token
