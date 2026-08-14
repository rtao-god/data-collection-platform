from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from official_http.contracts import OfficialHttpRequest, ResponseHeader
from official_http.errors import OfficialHttpError


@dataclass(frozen=True, slots=True)
class HttpFetchResult:
    status_code: int
    final_url: str
    headers: tuple[ResponseHeader, ...]
    remote_ip_address: str
    encoded_size_bytes: int
    decoded_size_bytes: int
    observed_at_utc: datetime
    body: bytes


class ScrapyChildFetcher:
    """Executes exactly one canonical HTTP request in a fresh Scrapy child process."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        python_executable: str = sys.executable,
    ) -> None:
        self._runner = runner
        self._python_executable = python_executable

    def fetch(self, request: OfficialHttpRequest) -> HttpFetchResult:
        with tempfile.TemporaryDirectory(prefix="official-http-") as directory:
            work = Path(directory)
            request_path = work / "request.json"
            result_path = work / "result.json"
            body_path = work / "body.bin"
            request_path.write_bytes(request.to_bytes())
            command = (
                self._python_executable,
                "-m",
                "official_http.scrapy_child",
                str(request_path),
                str(result_path),
                str(body_path),
            )
            try:
                completed = self._runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=request.timeout_seconds + 30,
                )
            except subprocess.TimeoutExpired as exc:
                raise OfficialHttpError(
                    code="OFFICIAL_HTTP_CHILD_TIMEOUT",
                    message="The one-shot Scrapy process exceeded its total timeout.",
                    kind="transient",
                ) from exc
            if not result_path.is_file():
                raise OfficialHttpError(
                    code="OFFICIAL_HTTP_CHILD_RESULT_MISSING",
                    message="The one-shot Scrapy process did not produce a typed result.",
                    kind="transient",
                    context={"exitCode": completed.returncode},
                )
            try:
                document = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise OfficialHttpError(
                    code="OFFICIAL_HTTP_CHILD_RESULT_INVALID",
                    message="The one-shot Scrapy process produced an invalid result.",
                    kind="transient",
                ) from exc
            if document.get("state") == "failed":
                raise OfficialHttpError(
                    code=_required_string(document, "code"),
                    message=_required_string(document, "message"),
                    kind=_failure_kind(document.get("kind")),
                    context={"exitCode": completed.returncode},
                )
            if document.get("state") != "succeeded" or completed.returncode != 0:
                raise OfficialHttpError(
                    code="OFFICIAL_HTTP_CHILD_STATE_INVALID",
                    message="The one-shot Scrapy process returned an unsupported state.",
                    kind="transient",
                    context={"exitCode": completed.returncode},
                )
            body = body_path.read_bytes() if body_path.is_file() else b""
            headers_value = document.get("headers")
            if not isinstance(headers_value, list):
                raise OfficialHttpError(
                    code="OFFICIAL_HTTP_CHILD_HEADERS_INVALID",
                    message="The one-shot Scrapy result headers are invalid.",
                    kind="contract_invalid",
                )
            headers = tuple(
                ResponseHeader.model_validate(item, strict=True) for item in headers_value
            )
            observed_at = datetime.fromisoformat(
                _required_string(document, "observedAtUtc").replace("Z", "+00:00")
            ).astimezone(UTC)
            return HttpFetchResult(
                status_code=_required_integer(document, "statusCode"),
                final_url=_required_string(document, "finalUrl"),
                headers=headers,
                remote_ip_address=_required_string(document, "remoteIpAddress"),
                encoded_size_bytes=_required_integer(document, "encodedSizeBytes"),
                decoded_size_bytes=_required_integer(document, "decodedSizeBytes"),
                observed_at_utc=observed_at,
                body=body,
            )


def selected_header(result: HttpFetchResult, name: str) -> str | None:
    matches = tuple(item.value for item in result.headers if item.name == name.casefold())
    if len(matches) > 1:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_RESPONSE_HEADER_DUPLICATE",
            message="The HTTP response contains a duplicate selected header.",
            context={"header": name.casefold()},
        )
    return matches[0] if matches else None


def _required_string(document: object, key: str) -> str:
    if not isinstance(document, dict):
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_CHILD_RESULT_INVALID",
            message="The one-shot Scrapy result is invalid.",
            kind="transient",
        )
    value = document.get(key)
    if not isinstance(value, str):
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_CHILD_RESULT_INVALID",
            message="The one-shot Scrapy result is missing a required string.",
            kind="transient",
            context={"field": key},
        )
    return value


def _required_integer(document: object, key: str) -> int:
    if not isinstance(document, dict):
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_CHILD_RESULT_INVALID",
            message="The one-shot Scrapy result is invalid.",
            kind="transient",
        )
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_CHILD_RESULT_INVALID",
            message="The one-shot Scrapy result is missing a required integer.",
            kind="transient",
            context={"field": key},
        )
    return value


FailureKind = Literal[
    "transient",
    "permanent",
    "policy_blocked",
    "contract_invalid",
]


def _failure_kind(value: object) -> FailureKind:
    if value in {"transient", "permanent", "policy_blocked", "contract_invalid"}:
        return value
    return "transient"
