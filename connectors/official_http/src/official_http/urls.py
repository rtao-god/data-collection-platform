from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from posixpath import normpath
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from official_http.errors import OfficialHttpError

Resolver = Callable[..., list[tuple[int, int, int, str, tuple[object, ...]]]]


def normalize_http_url(url: str, *, tracking_parameters: Iterable[str] = ()) -> str:
    if not url or len(url) > 4_096 or any(ord(character) < 32 for character in url):
        raise _url_error("OFFICIAL_HTTP_URL_INVALID", "The HTTP URL is missing or invalid.")
    split = urlsplit(url)
    scheme = split.scheme.lower()
    if scheme not in {"http", "https"}:
        raise _url_error(
            "OFFICIAL_HTTP_URL_SCHEME_FORBIDDEN", "Only HTTP and HTTPS URLs are allowed."
        )
    if split.username is not None or split.password is not None:
        raise _url_error(
            "OFFICIAL_HTTP_URL_USERINFO_FORBIDDEN", "URL user information is forbidden."
        )
    if not split.hostname:
        raise _url_error("OFFICIAL_HTTP_URL_HOST_MISSING", "The HTTP URL requires a host.")
    try:
        host = split.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise _url_error("OFFICIAL_HTTP_URL_HOST_INVALID", "The HTTP URL host is invalid.") from exc
    try:
        port = split.port
    except ValueError as exc:
        raise _url_error("OFFICIAL_HTTP_URL_PORT_INVALID", "The HTTP URL port is invalid.") from exc
    if port is not None and not 1 <= port <= 65_535:
        raise _url_error("OFFICIAL_HTTP_URL_PORT_INVALID", "The HTTP URL port is invalid.")
    default_port = 80 if scheme == "http" else 443
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    path = _normalize_path(split.path)
    tracking = {value.casefold() for value in tracking_parameters}
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=True, strict_parsing=False)
        if key.casefold() not in tracking
    ]
    query_pairs.sort(key=lambda item: (item[0], item[1]))
    return urlunsplit((scheme, netloc, path, urlencode(query_pairs, doseq=True), ""))


def canonical_origin(url: str) -> str:
    normalized = normalize_http_url(url)
    split = urlsplit(normalized)
    return f"{split.scheme}://{split.netloc}"


def require_same_origin(url: str, expected_origin: str) -> None:
    actual = canonical_origin(url)
    if actual != expected_origin:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_ORIGIN_MISMATCH",
            message="The HTTP URL is outside the approved origin.",
            kind="policy_blocked",
            context={"expectedOrigin": expected_origin, "actualOrigin": actual},
        )


def resolve_public_addresses(
    url: str,
    *,
    resolver: Resolver | None = None,
) -> tuple[str, ...]:
    split = urlsplit(normalize_http_url(url))
    hostname = split.hostname
    if hostname is None:
        raise _url_error(
            "OFFICIAL_HTTP_URL_HOST_MISSING",
            "The HTTP URL requires a host.",
        )
    port = split.port or (80 if split.scheme == "http" else 443)
    active_resolver = resolver or cast(Resolver, socket.getaddrinfo)
    try:
        answers = active_resolver(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_DNS_UNAVAILABLE",
            message="The HTTP host could not be resolved.",
            kind="transient",
            context={"host": hostname},
        ) from exc
    addresses = tuple(sorted({str(answer[4][0]) for answer in answers}))
    if not addresses:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_DNS_EMPTY",
            message="The HTTP host resolved without any address.",
            kind="transient",
            context={"host": hostname},
        )
    for address in addresses:
        require_public_address(address)
    return addresses


def require_public_address(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_REMOTE_ADDRESS_INVALID",
            message="The connected remote address is invalid.",
            kind="policy_blocked",
            context={"remoteAddress": value},
        ) from exc
    if not address.is_global:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_REMOTE_ADDRESS_FORBIDDEN",
            message="The HTTP target resolves to a non-public address.",
            kind="policy_blocked",
            context={"remoteAddress": value},
        )


def _normalize_path(value: str) -> str:
    if not value:
        return "/"
    trailing = value.endswith("/")
    normalized = normpath(value.replace("\\", "/"))
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized == "//":
        normalized = "/"
    if trailing and normalized != "/":
        normalized += "/"
    return normalized


def _url_error(code: str, message: str) -> OfficialHttpError:
    return OfficialHttpError(code=code, message=message)
