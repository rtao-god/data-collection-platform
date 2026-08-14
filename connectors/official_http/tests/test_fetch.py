from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

from official_http.scrapy_child import scrapy_settings

from official_http import OfficialHttpRequest, ScrapyChildFetcher


def _request() -> OfficialHttpRequest:
    return OfficialHttpRequest(
        requestId="request-1",
        sourceKey="source-official-example",
        sourcePolicyDigest="sha256:" + "2" * 64,
        requestKind="page",
        url="https://example.com/contact",
        allowedOrigin="https://example.com",
        userAgent="DataCollectionPlatform/1",
        timeoutSeconds=30,
        maximumEncodedBytes=1048576,
        maximumDecodedBytes=2097152,
        pageInterests=({"key": "contact", "tokens": ("contact",), "priority": 100},),
        robotsAllowed=True,
        robotsArtifactId=UUID(int=6),
        robotsDecisionDigest="sha256:" + "6" * 64,
    )


def test_scrapy_settings_have_no_local_queue_retry_redirect_or_cookie_truth() -> None:
    settings = scrapy_settings(_request())
    assert settings["JOBDIR"] is None
    assert settings["COOKIES_ENABLED"] is False
    assert settings["RETRY_ENABLED"] is False
    assert settings["REDIRECT_ENABLED"] is False
    assert settings["CONCURRENT_REQUESTS"] == 1
    assert settings["DOWNLOAD_MAXSIZE"] == 1048576


def test_fetcher_requires_typed_child_result_and_reads_exact_body() -> None:
    def runner(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        request_path, result_path, body_path = map(Path, command[-3:])
        assert json.loads(request_path.read_bytes())["requestId"] == "request-1"
        body_path.write_bytes(b"<html>contact</html>")
        result_path.write_text(
            json.dumps(
                {
                    "state": "succeeded",
                    "statusCode": 200,
                    "finalUrl": "https://example.com/contact",
                    "remoteIpAddress": "93.184.216.34",
                    "headers": [{"name": "content-type", "value": "text/html"}],
                    "encodedSizeBytes": 20,
                    "decodedSizeBytes": 20,
                    "observedAtUtc": "2026-08-14T00:00:00Z",
                }
            )
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = ScrapyChildFetcher(runner=runner, python_executable="python").fetch(_request())
    assert result.status_code == 200
    assert result.body == b"<html>contact</html>"


def test_bounded_decompression_rejects_expansion_beyond_owner_limit() -> None:
    import gzip

    from official_http.errors import OfficialHttpError
    from official_http.scrapy_child import _decode_body

    encoded = gzip.compress(b"x" * 10_000)
    try:
        _decode_body(encoded, content_encoding="gzip", maximum_bytes=1_000)
    except OfficialHttpError as failure:
        assert failure.code == "OFFICIAL_HTTP_DECODED_BODY_TOO_LARGE"
    else:
        raise AssertionError("decompression limit was not enforced")
