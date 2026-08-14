from __future__ import annotations

import json
import sys
import zlib
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from official_http.contracts import OfficialHttpRequest, decode_http_request
from official_http.errors import OfficialHttpError
from official_http.urls import require_public_address, resolve_public_addresses

_SELECTED_HEADERS = frozenset(
    {
        "cache-control",
        "content-encoding",
        "content-length",
        "content-type",
        "etag",
        "last-modified",
        "location",
        "retry-after",
    }
)


def scrapy_settings(request: OfficialHttpRequest) -> dict[str, object]:
    return {
        "COOKIES_ENABLED": False,
        "COMPRESSION_ENABLED": False,
        "DOWNLOAD_MAXSIZE": request.maximum_encoded_bytes,
        "DOWNLOAD_TIMEOUT": request.timeout_seconds,
        "JOBDIR": None,
        "LOG_ENABLED": False,
        "METAREFRESH_ENABLED": False,
        "REDIRECT_ENABLED": False,
        "RETRY_ENABLED": False,
        "ROBOTSTXT_OBEY": False,
        "CONCURRENT_REQUESTS": 1,
        "DOWNLOAD_HANDLERS": {
            "data": None,
            "file": None,
            "ftp": None,
            "s3": None,
        },
    }


def main(argv: list[str] | None = None) -> int:
    arguments = argv or sys.argv[1:]
    if len(arguments) != 3:
        return 2
    request_path, result_path, body_path = map(Path, arguments)
    try:
        request = decode_http_request(request_path.read_bytes())
        resolve_public_addresses(request.url)
        result = _crawl(request)
        body = result.pop("body")
        if not isinstance(body, bytes):
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_CHILD_BODY_INVALID",
                message="The one-shot Scrapy result body is invalid.",
            )
        if body:
            body_path.write_bytes(body)
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return 0
    except OfficialHttpError as exc:
        result_path.write_text(
            json.dumps(
                {
                    "state": "failed",
                    "kind": exc.kind,
                    "code": exc.code,
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return 1
    except BaseException:
        result_path.write_text(
            json.dumps(
                {
                    "state": "failed",
                    "kind": "transient",
                    "code": "OFFICIAL_HTTP_CHILD_UNHANDLED_FAILURE",
                    "message": "The one-shot Scrapy process failed before producing a response.",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return 1


def _crawl(request: OfficialHttpRequest) -> dict[str, object]:
    scrapy: Any = import_module("scrapy")
    crawler_module: Any = import_module("scrapy.crawler")
    holder: dict[str, object] = {}

    class OneShotSpider(scrapy.Spider):  # type: ignore[misc]
        name = "official-http-one-shot"

        async def start(self) -> Any:
            headers = {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml,text/xml,"
                    "application/json,text/plain,*/*;q=0.1"
                ),
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": request.user_agent,
            }
            if request.if_none_match is not None:
                headers["If-None-Match"] = request.if_none_match
            if request.if_modified_since is not None:
                headers["If-Modified-Since"] = request.if_modified_since
            yield scrapy.Request(
                request.url,
                method="GET",
                headers=headers,
                callback=self.capture,
                errback=self.capture_failure,
                dont_filter=True,
                meta={
                    "download_timeout": request.timeout_seconds,
                    "download_maxsize": request.maximum_encoded_bytes,
                    "dont_redirect": True,
                    "dont_retry": True,
                    "handle_httpstatus_all": True,
                },
            )

        def capture(self, response: Any) -> None:
            remote = str(response.ip_address)
            require_public_address(remote)
            encoded = bytes(response.body)
            if len(encoded) > request.maximum_encoded_bytes:
                raise OfficialHttpError(
                    code="OFFICIAL_HTTP_ENCODED_BODY_TOO_LARGE",
                    message="The HTTP response exceeds the encoded byte limit.",
                    kind="permanent",
                )
            headers = _selected_headers(response.headers)
            content_encoding = next(
                (item["value"] for item in headers if item["name"] == "content-encoding"),
                None,
            )
            decoded = _decode_body(
                encoded,
                content_encoding=content_encoding,
                maximum_bytes=request.maximum_decoded_bytes,
            )
            holder.update(
                {
                    "state": "succeeded",
                    "statusCode": int(response.status),
                    "finalUrl": str(response.url),
                    "remoteIpAddress": remote,
                    "headers": headers,
                    "encodedSizeBytes": len(encoded),
                    "decodedSizeBytes": len(decoded),
                    "observedAtUtc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "body": decoded,
                }
            )

        def capture_failure(self, failure: Any) -> None:
            holder.update(
                {
                    "state": "failed",
                    "kind": "transient",
                    "code": "OFFICIAL_HTTP_NETWORK_FAILURE",
                    "message": "The official HTTP request failed before receiving a response.",
                }
            )

    process = crawler_module.CrawlerProcess(settings=scrapy_settings(request))
    process.crawl(OneShotSpider)
    process.start(stop_after_crawl=True, install_signal_handlers=False)
    if not holder:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_RESPONSE_MISSING",
            message="The one-shot Scrapy process completed without a response.",
            kind="transient",
        )
    return holder


def _selected_headers(headers: Any) -> list[dict[str, str]]:
    selected: dict[str, str] = {}
    for raw_name, raw_values in headers.items():
        name = bytes(raw_name).decode("latin-1").casefold()
        if name not in _SELECTED_HEADERS:
            continue
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        selected[name] = ", ".join(bytes(value).decode("latin-1") for value in values)[:4_096]
    return [{"name": name, "value": selected[name]} for name in sorted(selected)]


def _decode_body(encoded: bytes, *, content_encoding: str | None, maximum_bytes: int) -> bytes:
    encoding = (content_encoding or "identity").split(",", 1)[0].strip().casefold()
    if encoding in {"", "identity"}:
        decoded = encoded
    elif encoding == "gzip":
        decoded = _bounded_zlib_decompress(
            encoded,
            wbits=16 + zlib.MAX_WBITS,
            maximum_bytes=maximum_bytes,
            error_code="OFFICIAL_HTTP_GZIP_INVALID",
            error_message="The HTTP response has an invalid gzip representation.",
        )
    elif encoding == "deflate":
        decoded = _bounded_zlib_decompress(
            encoded,
            wbits=zlib.MAX_WBITS,
            maximum_bytes=maximum_bytes,
            error_code="OFFICIAL_HTTP_DEFLATE_INVALID",
            error_message="The HTTP response has an invalid deflate representation.",
        )
    else:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_CONTENT_ENCODING_UNSUPPORTED",
            message="The HTTP response content encoding is unsupported.",
            kind="permanent",
            context={"contentEncoding": encoding},
        )
    if len(decoded) > maximum_bytes:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_DECODED_BODY_TOO_LARGE",
            message="The HTTP response exceeds the decoded byte limit.",
            kind="permanent",
        )
    return decoded


def _bounded_zlib_decompress(
    encoded: bytes,
    *,
    wbits: int,
    maximum_bytes: int,
    error_code: str,
    error_message: str,
) -> bytes:
    decoder = zlib.decompressobj(wbits)
    output = bytearray()
    try:
        pending = encoded
        while pending:
            remaining = maximum_bytes - len(output)
            if remaining < 0:
                _raise_decoded_body_too_large()
            chunk = decoder.decompress(pending, remaining + 1)
            output.extend(chunk)
            if len(output) > maximum_bytes:
                _raise_decoded_body_too_large()
            pending = decoder.unconsumed_tail
            if not pending:
                break
        remaining = maximum_bytes - len(output)
        output.extend(decoder.flush(remaining + 1))
    except zlib.error as exc:
        raise OfficialHttpError(
            code=error_code,
            message=error_message,
            kind="permanent",
        ) from exc
    if len(output) > maximum_bytes or not decoder.eof:
        if len(output) > maximum_bytes:
            _raise_decoded_body_too_large()
        raise OfficialHttpError(
            code=error_code,
            message=error_message,
            kind="permanent",
        )
    return bytes(output)


def _raise_decoded_body_too_large() -> None:
    raise OfficialHttpError(
        code="OFFICIAL_HTTP_DECODED_BODY_TOO_LARGE",
        message="The HTTP response exceeds the decoded byte limit.",
        kind="permanent",
    )


if __name__ == "__main__":
    raise SystemExit(main())
