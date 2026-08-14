from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


write(
    "connectors/official_website_http/pyproject.toml",
    '''[project]
name = "official-website-http-connector"
version = "0.1.0"
description = "Policy-safe official website HTTP acquisition connector"
requires-python = ">=3.13,<3.14"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/official_website_http"]
''',
)

write(
    "connectors/official_website_http/src/official_website_http/core.py",
    '''from __future__ import annotations

import gzip
import http.client
import ipaddress
import socket
import ssl
import zlib
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal, Protocol, cast
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

type FailureKind = Literal["transient", "permanent", "policy_blocked"]
type FetchOutcome = Literal[
    "fetched",
    "unchanged",
    "blocked_by_robots",
    "challenge",
    "rate_limited",
    "not_found",
    "rejected",
]
type PageKind = Literal[
    "home",
    "contact",
    "about",
    "services",
    "pricing",
    "location",
    "other",
]


@dataclass(frozen=True, slots=True)
class ConnectorFailure(Exception):
    code: str
    kind: FailureKind
    message: str
    required_action: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True, slots=True)
class ConditionalRequest:
    etag: str | None = None
    last_modified: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("etag", self.etag), ("last_modified", self.last_modified)):
            if value is not None and (not value.strip() or "\r" in value or "\n" in value):
                raise ValueError(f"{name} must be a non-empty single-line value")


@dataclass(frozen=True, slots=True)
class FetchRequest:
    url: str
    allowed_domains: tuple[str, ...]
    user_agent: str
    conditional: ConditionalRequest = ConditionalRequest()
    respect_robots: bool = True
    maximum_redirects: int = 5
    maximum_wire_bytes: int = 8 * 1024 * 1024
    maximum_decoded_bytes: int = 16 * 1024 * 1024
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("url is required")
        if not self.allowed_domains or any(not value.strip() for value in self.allowed_domains):
            raise ValueError("allowed_domains must contain non-empty values")
        if not self.user_agent.strip() or "\r" in self.user_agent or "\n" in self.user_agent:
            raise ValueError("user_agent must be a non-empty single-line value")
        if not 0 <= self.maximum_redirects <= 10:
            raise ValueError("maximum_redirects must be between 0 and 10")
        if not 1 <= self.maximum_wire_bytes <= 32 * 1024 * 1024:
            raise ValueError("maximum_wire_bytes is outside the supported range")
        if not self.maximum_wire_bytes <= self.maximum_decoded_bytes <= 64 * 1024 * 1024:
            raise ValueError("maximum_decoded_bytes is outside the supported range")
        if not 0.1 <= self.timeout_seconds <= 120.0:
            raise ValueError("timeout_seconds is outside the supported range")


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    absolute: str
    scheme: Literal["http", "https"]
    host: str
    port: int
    request_target: str


@dataclass(frozen=True, slots=True)
class RawRequest:
    url: NormalizedUrl
    headers: tuple[tuple[str, str], ...]
    timeout_seconds: float
    maximum_wire_bytes: int
    maximum_decoded_bytes: int


@dataclass(frozen=True, slots=True)
class RawResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    wire_bytes: int
    decoded_bytes: int

    def header(self, name: str) -> str | None:
        expected = name.casefold()
        for key, value in self.headers:
            if key.casefold() == expected:
                return value
        return None


@dataclass(frozen=True, slots=True)
class FetchResult:
    outcome: FetchOutcome
    requested_url: str
    final_url: str
    status_code: int | None
    headers: tuple[tuple[str, str], ...]
    body: bytes | None
    wire_bytes: int
    decoded_bytes: int
    robots_url: str | None
    sitemap_urls: tuple[str, ...]
    reason_code: str


@dataclass(frozen=True, slots=True)
class PageCandidate:
    url: str
    anchor_text: str = ""


@dataclass(frozen=True, slots=True)
class PlannedPage:
    url: str
    kind: PageKind
    score: int
    reason: str


class HostResolver(Protocol):
    def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class HttpTransport(Protocol):
    def request(self, request: RawRequest) -> RawResponse: ...


@dataclass(frozen=True, slots=True)
class SystemHostResolver:
    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ConnectorFailure(
                "HTTP_DNS_RESOLUTION_FAILED",
                "transient",
                f"DNS resolution failed for {host}",
                "Restore public DNS resolution and retry the exact work unit.",
            ) from exc
        addresses = tuple(sorted({str(record[4][0]) for record in records}))
        if not addresses:
            raise ConnectorFailure(
                "HTTP_DNS_NO_ADDRESSES",
                "transient",
                f"DNS returned no addresses for {host}",
                "Restore public DNS resolution and retry the exact work unit.",
            )
        return addresses


def normalize_url(url: str, allowed_domains: tuple[str, ...]) -> NormalizedUrl:
    raw = url.strip()
    if any(character in raw for character in ("\r", "\n", "\x00")):
        raise _policy("HTTP_URL_CONTROL_CHARACTER", "URL contains a control character")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise _policy("HTTP_URL_INVALID", "URL cannot be parsed") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise _policy("HTTP_URL_SCHEME_FORBIDDEN", "Only HTTP and HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise _policy("HTTP_URL_USERINFO_FORBIDDEN", "URL user information is forbidden")
    if not parsed.hostname:
        raise _policy("HTTP_URL_HOST_MISSING", "URL host is required")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    except UnicodeError as exc:
        raise _policy("HTTP_URL_HOST_INVALID", "URL host is invalid") from exc
    allowed = tuple(_domain(value) for value in allowed_domains)
    if not any(host == domain or host.endswith(f".{domain}") for domain in allowed):
        raise _policy("HTTP_URL_DOMAIN_FORBIDDEN", f"Host {host} is outside the source allowlist")
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise _policy("HTTP_URL_PORT_INVALID", "URL port is invalid") from exc
    default_port = 443 if scheme == "https" else 80
    port = explicit_port or default_port
    if port not in {80, 443}:
        raise _policy("HTTP_URL_PORT_FORBIDDEN", "Only ports 80 and 443 are allowed")
    path = _dot_segments(parsed.path or "/")
    target = path + (f"?{parsed.query}" if parsed.query else "")
    netloc = host if port == default_port else f"{host}:{port}"
    return NormalizedUrl(
        urlunsplit(SplitResult(scheme, netloc, path, parsed.query, "")),
        cast(Literal["http", "https"], scheme),
        host,
        port,
        target,
    )


def resolve_public_addresses(url: NormalizedUrl, resolver: HostResolver) -> tuple[str, ...]:
    addresses = resolver.resolve(url.host, url.port)
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise _policy("HTTP_DNS_ADDRESS_INVALID", "DNS returned an invalid address") from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global:
            raise _policy(
                "HTTP_SSRF_ADDRESS_FORBIDDEN",
                f"DNS resolved to non-public address {address.compressed}",
            )
    return addresses


@dataclass(slots=True)
class PinnedHttpTransport:
    resolver: HostResolver = field(default_factory=SystemHostResolver)
    ssl_context: ssl.SSLContext | None = None

    def request(self, request: RawRequest) -> RawResponse:
        addresses = resolve_public_addresses(request.url, self.resolver)
        last_error: BaseException | None = None
        for address in addresses:
            try:
                return self._request_address(request, address)
            except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:
                last_error = exc
        raise ConnectorFailure(
            "HTTP_CONNECT_FAILED",
            "transient",
            f"Connection failed for {request.url.host}",
            "Restore the approved endpoint and retry the exact work unit.",
        ) from last_error

    def _request_address(self, request: RawRequest, address: str) -> RawResponse:
        raw_socket = socket.create_connection((address, request.url.port), request.timeout_seconds)
        connection = http.client.HTTPConnection(
            request.url.host,
            request.url.port,
            timeout=request.timeout_seconds,
        )
        try:
            if request.url.scheme == "https":
                context = self.ssl_context or ssl.create_default_context()
                connection.sock = context.wrap_socket(raw_socket, server_hostname=request.url.host)
            else:
                connection.sock = raw_socket
            headers = dict(request.headers)
            headers.setdefault("Host", request.url.host)
            headers.setdefault("Connection", "close")
            headers.setdefault("Accept-Encoding", "gzip, deflate")
            connection.request("GET", request.url.request_target, headers=headers)
            response = connection.getresponse()
            response_headers = _headers(response.getheaders())
            declared = response.getheader("Content-Length")
            if declared is not None:
                try:
                    if int(declared) > request.maximum_wire_bytes:
                        raise _limit("HTTP_WIRE_SIZE_EXCEEDED")
                except ValueError as exc:
                    raise ConnectorFailure(
                        "HTTP_CONTENT_LENGTH_INVALID",
                        "permanent",
                        "Response Content-Length is invalid",
                        "Correct the source response before retrying.",
                    ) from exc
            body, wire = _decode(
                _chunks(response),
                response.getheader("Content-Encoding"),
                request.maximum_wire_bytes,
                request.maximum_decoded_bytes,
            )
            return RawResponse(
                response.status,
                response_headers,
                body,
                wire,
                len(body),
            )
        finally:
            connection.close()


def _decode(
    chunks: Iterable[bytes],
    encoding: str | None,
    wire_limit: int,
    decoded_limit: int,
) -> tuple[bytes, int]:
    normalized = (encoding or "identity").strip().casefold()
    decoder: zlib.Decompress | None
    if normalized in {"", "identity"}:
        decoder = None
    elif normalized == "gzip":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif normalized == "deflate":
        decoder = zlib.decompressobj()
    else:
        raise ConnectorFailure(
            "HTTP_CONTENT_ENCODING_UNSUPPORTED",
            "permanent",
            f"Unsupported content encoding {normalized!r}",
            "Use identity, gzip, or deflate encoding for this source.",
        )
    output = bytearray()
    wire = 0
    try:
        for chunk in chunks:
            wire += len(chunk)
            if wire > wire_limit:
                raise _limit("HTTP_WIRE_SIZE_EXCEEDED")
            output.extend(chunk if decoder is None else decoder.decompress(chunk))
            if len(output) > decoded_limit:
                raise _limit("HTTP_DECODED_SIZE_EXCEEDED")
        if decoder is not None:
            output.extend(decoder.flush())
    except zlib.error as exc:
        raise ConnectorFailure(
            "HTTP_CONTENT_DECODING_FAILED",
            "permanent",
            "Response content encoding is malformed",
            "Correct the source response before retrying.",
        ) from exc
    if len(output) > decoded_limit:
        raise _limit("HTTP_DECODED_SIZE_EXCEEDED")
    return bytes(output), wire


def parse_robots(body: bytes) -> tuple[RobotFileParser, tuple[str, ...]]:
    if len(body) > 512 * 1024:
        raise _limit("HTTP_ROBOTS_SIZE_EXCEEDED")
    try:
        lines = body.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ConnectorFailure(
            "HTTP_ROBOTS_ENCODING_INVALID",
            "permanent",
            "robots.txt is not valid UTF-8",
            "Correct robots.txt before retrying.",
        ) from exc
    parser = RobotFileParser()
    parser.parse(lines)
    sitemaps: list[str] = []
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        name, separator, value = line.partition(":")
        if separator and name.strip().casefold() == "sitemap" and value.strip():
            sitemaps.append(value.strip())
    return parser, tuple(dict.fromkeys(sitemaps))


def parse_sitemap_urls(
    body: bytes,
    allowed_domains: tuple[str, ...],
    maximum_urls: int = 10_000,
) -> tuple[str, ...]:
    if len(body) > 8 * 1024 * 1024:
        raise _limit("HTTP_SITEMAP_SIZE_EXCEEDED")
    lowered = body[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise _policy("HTTP_SITEMAP_DTD_FORBIDDEN", "Sitemap DTD and entities are forbidden")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ConnectorFailure(
            "HTTP_SITEMAP_XML_INVALID",
            "permanent",
            "Sitemap XML is malformed",
            "Correct the sitemap before retrying.",
        ) from exc
    urls: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].casefold() != "loc" or element.text is None:
            continue
        if len(urls) >= maximum_urls:
            raise _limit("HTTP_SITEMAP_URL_COUNT_EXCEEDED")
        urls.append(normalize_url(element.text, allowed_domains).absolute)
    return tuple(dict.fromkeys(urls))


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.href: str | None = None
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            self.href = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self.href is not None:
            self.links.append((self.href, " ".join(self.text).strip()))
            self.href = None
            self.text = []


def extract_links(body: bytes, base_url: str, maximum_links: int = 2_000) -> tuple[PageCandidate, ...]:
    if len(body) > 8 * 1024 * 1024:
        raise _limit("HTTP_HTML_SIZE_EXCEEDED")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConnectorFailure(
            "HTTP_HTML_ENCODING_INVALID",
            "permanent",
            "HTML is not valid UTF-8",
            "Correct the source encoding before retrying.",
        ) from exc
    parser = _Links()
    parser.feed(text)
    if len(parser.links) > maximum_links:
        raise _limit("HTTP_LINK_COUNT_EXCEEDED")
    return tuple(PageCandidate(urljoin(base_url, href), anchor) for href, anchor in parser.links)


_KEYWORDS: dict[PageKind, tuple[str, ...]] = {
    "contact": ("contact", "kontakt", "impressum"),
    "about": ("about", "uber-uns", "ueber-uns", "team"),
    "services": ("service", "leistungen", "studio", "recording"),
    "pricing": ("price", "pricing", "preise", "rates"),
    "location": ("location", "anfahrt", "directions", "map"),
}


def plan_pages(
    candidates: tuple[PageCandidate, ...],
    allowed_domains: tuple[str, ...],
    maximum_pages: int = 50,
) -> tuple[PlannedPage, ...]:
    values: dict[str, PlannedPage] = {}
    for candidate in candidates:
        try:
            normalized = normalize_url(candidate.url, allowed_domains)
        except ConnectorFailure:
            continue
        material = f"{urlsplit(normalized.absolute).path} {candidate.anchor_text}".casefold()
        if urlsplit(normalized.absolute).path in {"", "/"}:
            planned = PlannedPage(normalized.absolute, "home", 100, "root URL")
        else:
            planned = PlannedPage(normalized.absolute, "other", 10, "same-source fallback")
            for kind, keywords in _KEYWORDS.items():
                count = sum(keyword in material for keyword in keywords)
                if count:
                    planned = PlannedPage(
                        normalized.absolute,
                        kind,
                        80 + count,
                        f"matched {kind} keyword",
                    )
                    break
        current = values.get(planned.url)
        if current is None or planned.score > current.score:
            values[planned.url] = planned
    return tuple(sorted(values.values(), key=lambda item: (-item.score, item.url))[:maximum_pages])


@dataclass(slots=True)
class OfficialWebsiteCrawler:
    transport: HttpTransport = field(default_factory=PinnedHttpTransport)

    def fetch(self, request: FetchRequest) -> FetchResult:
        target = normalize_url(request.url, request.allowed_domains)
        robots_url: str | None = None
        sitemaps: tuple[str, ...] = ()
        if request.respect_robots:
            robots_url = urlunsplit((target.scheme, target.host, "/robots.txt", "", ""))
            robots, robots_final = self._follow(
                robots_url,
                request,
                (("User-Agent", request.user_agent),),
                min(request.maximum_wire_bytes, 512 * 1024),
                min(request.maximum_decoded_bytes, 512 * 1024),
            )
            if robots.status_code in {401, 403}:
                return _result(request, target.absolute, "blocked_by_robots", None, robots_final, (), "HTTP_ROBOTS_ACCESS_FORBIDDEN")
            if robots.status_code == 429 or robots.status_code >= 500:
                raise ConnectorFailure(
                    "HTTP_ROBOTS_UNAVAILABLE",
                    "transient",
                    "robots.txt is temporarily unavailable",
                    "Restore robots.txt availability and retry the exact work unit.",
                )
            if 200 <= robots.status_code < 300:
                policy, declared = parse_robots(robots.body)
                sitemaps = tuple(normalize_url(value, request.allowed_domains).absolute for value in declared)
                if not policy.can_fetch(request.user_agent, target.absolute):
                    return _result(request, target.absolute, "blocked_by_robots", None, robots_final, sitemaps, "HTTP_ROBOTS_DISALLOWED")
        headers: list[tuple[str, str]] = [("User-Agent", request.user_agent)]
        if request.conditional.etag is not None:
            headers.append(("If-None-Match", request.conditional.etag))
        if request.conditional.last_modified is not None:
            headers.append(("If-Modified-Since", request.conditional.last_modified))
        response, final_url = self._follow(
            target.absolute,
            request,
            tuple(headers),
            request.maximum_wire_bytes,
            request.maximum_decoded_bytes,
        )
        outcome, reason = _classify(response)
        return _result(request, final_url, outcome, response, robots_url, sitemaps, reason)

    def _follow(
        self,
        url: str,
        request: FetchRequest,
        headers: tuple[tuple[str, str], ...],
        wire_limit: int,
        decoded_limit: int,
    ) -> tuple[RawResponse, str]:
        current = normalize_url(url, request.allowed_domains)
        visited: set[str] = set()
        for redirect_count in range(request.maximum_redirects + 1):
            if current.absolute in visited:
                raise ConnectorFailure(
                    "HTTP_REDIRECT_LOOP",
                    "permanent",
                    "Redirect loop detected",
                    "Correct the source redirect chain before retrying.",
                )
            visited.add(current.absolute)
            response = self.transport.request(
                RawRequest(current, headers, request.timeout_seconds, wire_limit, decoded_limit)
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response, current.absolute
            location = response.header("Location")
            if not location:
                raise ConnectorFailure(
                    "HTTP_REDIRECT_LOCATION_MISSING",
                    "permanent",
                    "Redirect response has no Location header",
                    "Correct the source redirect response before retrying.",
                )
            if redirect_count >= request.maximum_redirects:
                raise _policy("HTTP_REDIRECT_LIMIT_EXCEEDED", "Redirect limit exceeded")
            current = normalize_url(urljoin(current.absolute, location), request.allowed_domains)
        raise AssertionError("redirect loop exhausted unexpectedly")


def _classify(response: RawResponse) -> tuple[FetchOutcome, str]:
    if response.status_code == 304:
        return "unchanged", "HTTP_NOT_MODIFIED"
    if response.status_code == 429:
        return "rate_limited", "HTTP_RATE_LIMITED"
    if response.status_code == 404:
        return "not_found", "HTTP_NOT_FOUND"
    if response.status_code in {401, 403}:
        return "challenge", "HTTP_ACCESS_CHALLENGE"
    if response.status_code >= 500:
        raise ConnectorFailure(
            "HTTP_UPSTREAM_FAILURE",
            "transient",
            f"Website returned HTTP {response.status_code}",
            "Restore the approved endpoint and retry the exact work unit.",
        )
    if 200 <= response.status_code < 300:
        sample = response.body[:256 * 1024].lower()
        if any(marker in sample for marker in (b"captcha", b"cf-chl-", b"verify you are human")):
            return "challenge", "HTTP_BODY_CHALLENGE"
        return "fetched", "HTTP_FETCHED"
    return "rejected", "HTTP_STATUS_REJECTED"


def _result(
    request: FetchRequest,
    final_url: str,
    outcome: FetchOutcome,
    response: RawResponse | None,
    robots_url: str | None,
    sitemaps: tuple[str, ...],
    reason: str,
) -> FetchResult:
    return FetchResult(
        outcome,
        request.url,
        final_url,
        None if response is None else response.status_code,
        () if response is None else response.headers,
        None if response is None else response.body,
        0 if response is None else response.wire_bytes,
        0 if response is None else response.decoded_bytes,
        robots_url,
        sitemaps,
        reason,
    )


def _chunks(response: http.client.HTTPResponse) -> Iterator[bytes]:
    while chunk := response.read(64 * 1024):
        yield chunk


def _headers(values: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    if len(values) > 100 or sum(len(name) + len(value) for name, value in values) > 64 * 1024:
        raise _limit("HTTP_HEADER_LIMIT_EXCEEDED")
    return tuple(values)


def _domain(value: str) -> str:
    domain = value.strip().rstrip(".")
    if "://" in domain or "/" in domain or "@" in domain:
        raise _policy("HTTP_ALLOWED_DOMAIN_INVALID", "Allowed domain is not a host name")
    try:
        canonical = domain.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise _policy("HTTP_ALLOWED_DOMAIN_INVALID", "Allowed domain is invalid") from exc
    if not canonical:
        raise _policy("HTTP_ALLOWED_DOMAIN_INVALID", "Allowed domain is empty")
    return canonical


def _dot_segments(path: str) -> str:
    output: list[str] = []
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if output:
                output.pop()
        else:
            output.append(part)
    suffix = "/" if path.endswith("/") and output else ""
    return "/" + "/".join(output) + suffix


def _policy(code: str, message: str) -> ConnectorFailure:
    return ConnectorFailure(
        code,
        "policy_blocked",
        message,
        "Correct the source policy or target URL before scheduling replacement work.",
    )


def _limit(code: str) -> ConnectorFailure:
    return ConnectorFailure(
        code,
        "policy_blocked",
        "Response exceeds a configured bounded limit",
        "Reduce the source response or approve a bounded source-policy change.",
    )
''',
)

write(
    "connectors/official_website_http/src/official_website_http/__init__.py",
    '''from official_website_http.core import (
    ConditionalRequest,
    ConnectorFailure,
    FetchOutcome,
    FetchRequest,
    FetchResult,
    FailureKind,
    HostResolver,
    HttpTransport,
    NormalizedUrl,
    OfficialWebsiteCrawler,
    PageCandidate,
    PageKind,
    PinnedHttpTransport,
    PlannedPage,
    RawRequest,
    RawResponse,
    SystemHostResolver,
    extract_links,
    normalize_url,
    parse_robots,
    parse_sitemap_urls,
    plan_pages,
    resolve_public_addresses,
)

__all__ = [
    "ConditionalRequest",
    "ConnectorFailure",
    "FailureKind",
    "FetchOutcome",
    "FetchRequest",
    "FetchResult",
    "HostResolver",
    "HttpTransport",
    "NormalizedUrl",
    "OfficialWebsiteCrawler",
    "PageCandidate",
    "PageKind",
    "PinnedHttpTransport",
    "PlannedPage",
    "RawRequest",
    "RawResponse",
    "SystemHostResolver",
    "extract_links",
    "normalize_url",
    "parse_robots",
    "parse_sitemap_urls",
    "plan_pages",
    "resolve_public_addresses",
]
''',
)

write(
    "connectors/official_website_http/tests/test_connector.py",
    '''from __future__ import annotations

import gzip
from collections import deque

import pytest
from official_website_http import (
    ConditionalRequest,
    ConnectorFailure,
    FetchRequest,
    OfficialWebsiteCrawler,
    PageCandidate,
    RawRequest,
    RawResponse,
    extract_links,
    normalize_url,
    parse_robots,
    parse_sitemap_urls,
    plan_pages,
    resolve_public_addresses,
)
from official_website_http.core import _decode


class Resolver:
    def __init__(self, *addresses: str) -> None:
        self.addresses = addresses

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        assert host
        assert port in {80, 443}
        return self.addresses


class Transport:
    def __init__(self, *responses: RawResponse) -> None:
        self.responses = deque(responses)
        self.requests: list[RawRequest] = []

    def request(self, request: RawRequest) -> RawResponse:
        self.requests.append(request)
        return self.responses.popleft()


def response(status: int, body: bytes = b"", *headers: tuple[str, str]) -> RawResponse:
    return RawResponse(status, headers, body, len(body), len(body))


def request(url: str = "https://example.com/") -> FetchRequest:
    return FetchRequest(url, ("example.com",), "CollectorBot/1.0")


def test_normalizes_url_and_rejects_forbidden_shapes() -> None:
    value = normalize_url(
        "HTTPS://Studio.Example.COM:443/a/../contact?x=1#fragment",
        ("example.com",),
    )
    assert value.absolute == "https://studio.example.com/contact?x=1"
    for invalid in (
        "file:///etc/passwd",
        "https://user:pass@example.com/",
        "https://example.com:444/",
        "https://other.test/",
        "https://example.com/\nHost: internal",
    ):
        with pytest.raises(ConnectorFailure):
            normalize_url(invalid, ("example.com",))


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1"],
)
def test_rejects_non_public_dns_answers(address: str) -> None:
    url = normalize_url("https://example.com/", ("example.com",))
    with pytest.raises(ConnectorFailure) as captured:
        resolve_public_addresses(url, Resolver(address))
    assert captured.value.code == "HTTP_SSRF_ADDRESS_FORBIDDEN"


def test_rejects_mixed_public_and_private_dns_answers() -> None:
    url = normalize_url("https://example.com/", ("example.com",))
    assert resolve_public_addresses(url, Resolver("93.184.216.34")) == ("93.184.216.34",)
    with pytest.raises(ConnectorFailure):
        resolve_public_addresses(url, Resolver("93.184.216.34", "127.0.0.1"))


def test_bounded_decoding_handles_gzip_and_limits() -> None:
    body = b"hello" * 100
    encoded = gzip.compress(body)
    assert _decode([encoded], "gzip", 10_000, 10_000)[0] == body
    with pytest.raises(ConnectorFailure) as wire:
        _decode([body], None, 10, 10_000)
    assert wire.value.code == "HTTP_WIRE_SIZE_EXCEEDED"
    with pytest.raises(ConnectorFailure) as decoded:
        _decode([encoded], "gzip", 10_000, 10)
    assert decoded.value.code == "HTTP_DECODED_SIZE_EXCEEDED"
    with pytest.raises(ConnectorFailure):
        _decode([body], "br", 10_000, 10_000)


def test_robots_sitemap_and_page_planning_are_deterministic() -> None:
    robots, sitemaps = parse_robots(
        b"User-agent: *\nDisallow: /private\nSitemap: https://example.com/sitemap.xml\n"
    )
    assert robots.can_fetch("CollectorBot", "https://example.com/contact")
    assert not robots.can_fetch("CollectorBot", "https://example.com/private/a")
    assert sitemaps == ("https://example.com/sitemap.xml",)

    xml = b'''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/contact</loc></url><url><loc>https://example.com/pricing</loc></url></urlset>'''
    assert parse_sitemap_urls(xml, ("example.com",)) == (
        "https://example.com/contact",
        "https://example.com/pricing",
    )
    with pytest.raises(ConnectorFailure):
        parse_sitemap_urls(b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><urlset/>', ("example.com",))

    links = extract_links(
        b'<a href="/kontakt">Kontakt</a><a href="/preise">Preise</a><a href="/">Home</a>',
        "https://example.com/",
    )
    planned = plan_pages(links, ("example.com",))
    assert [item.kind for item in planned] == ["home", "contact", "pricing"]


def test_planning_deduplicates_and_filters_other_domains() -> None:
    planned = plan_pages(
        (
            PageCandidate("https://example.com/contact", "Contact"),
            PageCandidate("https://example.com/contact", "Kontakt"),
            PageCandidate("https://evil.test/contact", "Contact"),
        ),
        ("example.com",),
    )
    assert len(planned) == 1
    assert planned[0].kind == "contact"


def test_crawler_fetches_robots_page_and_sitemap() -> None:
    transport = Transport(
        response(200, b"User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n"),
        response(200, b"<html>Studio</html>", ("ETag", '"abc"')),
    )
    result = OfficialWebsiteCrawler(transport).fetch(request())
    assert result.outcome == "fetched"
    assert result.sitemap_urls == ("https://example.com/sitemap.xml",)
    assert [item.url.request_target for item in transport.requests] == ["/robots.txt", "/"]


def test_robots_disallow_stops_before_page_fetch() -> None:
    transport = Transport(response(200, b"User-agent: *\nDisallow: /private\n"))
    result = OfficialWebsiteCrawler(transport).fetch(request("https://example.com/private"))
    assert result.outcome == "blocked_by_robots"
    assert result.body is None
    assert len(transport.requests) == 1


def test_redirect_revalidation_and_conditional_request() -> None:
    transport = Transport(
        response(404),
        response(301, b"", ("Location", "/contact")),
        response(304),
    )
    value = FetchRequest(
        "https://example.com/",
        ("example.com",),
        "CollectorBot/1.0",
        ConditionalRequest(etag='"abc"'),
    )
    result = OfficialWebsiteCrawler(transport).fetch(value)
    assert result.outcome == "unchanged"
    assert result.final_url == "https://example.com/contact"
    assert dict(transport.requests[1].headers)["If-None-Match"] == '"abc"'


def test_redirect_to_other_domain_is_blocked() -> None:
    transport = Transport(response(404), response(302, b"", ("Location", "https://evil.test/")))
    with pytest.raises(ConnectorFailure) as captured:
        OfficialWebsiteCrawler(transport).fetch(request())
    assert captured.value.code == "HTTP_URL_DOMAIN_FORBIDDEN"


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        (403, b"", "challenge"),
        (429, b"", "rate_limited"),
        (404, b"", "not_found"),
        (200, b"<title>Verify you are human</title>", "challenge"),
    ],
)
def test_challenges_and_rate_limits_never_become_browser_escalation(
    status: int,
    body: bytes,
    outcome: str,
) -> None:
    transport = Transport(response(404), response(status, body))
    assert OfficialWebsiteCrawler(transport).fetch(request()).outcome == outcome


def test_unavailable_robots_fails_closed() -> None:
    with pytest.raises(ConnectorFailure) as captured:
        OfficialWebsiteCrawler(Transport(response(503))).fetch(request())
    assert captured.value.code == "HTTP_ROBOTS_UNAVAILABLE"
    assert captured.value.kind == "transient"
''',
)

write(
    "docs/specifications/stage5-official-website-http.md",
    '''# Stage 5 official website HTTP acquisition

Status: approved connector-owner boundary.

`connectors/official_website_http` owns policy-safe official-website retrieval and deterministic page planning. It does not own durable queue state, PostgreSQL, artifacts, browser escalation, extraction, normalization, matching, quality, review, or export. The Work Engine remains the canonical queue and retry owner.

## Inputs

The connector accepts an exact URL, source allowlisted domains, user agent, conditional validators, robots mode, redirect limit, wire/decoded byte limits, and timeout.

## Security invariants

- only HTTP and HTTPS are accepted;
- user information, control characters, unapproved ports, and non-allowlisted domains are rejected;
- every request and redirect hop resolves DNS again;
- every resolved address must be globally routable;
- production transport connects to a validated address while preserving Host and TLS server name;
- redirects are bounded, loop checked, and revalidated;
- headers, wire bytes, decoded bytes, robots files, sitemaps, and link counts are bounded;
- only identity, gzip, and deflate encodings are accepted;
- robots denial is a typed policy outcome and unavailable robots data fails closed;
- 403, 429, and challenge pages never create browser escalation.

## Outputs

The connector returns a typed outcome, requested/final URL, status, bounded headers/body, byte counts, robots identity, sitemap candidates, and reason code. The future HTTP worker persists raw artifacts only through Worker Gateway.

## Planning

Sitemap and HTML URLs pass the same URL policy. Planning ranks home, contact, about, services, pricing, and location pages with canonical URL ordering as the deterministic tie-breaker.

## Proof

Acceptance requires frozen workspace restore, Ruff, strict mypy, connector tests, architecture dependency checks, generated-contract drift, and Python compilation. Fixture success is not real source coverage.
''',
)

workspace = ROOT / "pyproject.toml"
text = workspace.read_text(encoding="utf-8")
if '"connectors/official_website_http"' not in text:
    text = text.replace(
        '  "connectors/osm_overpass",\n',
        '  "connectors/official_website_http",\n  "connectors/osm_overpass",\n',
        1,
    )
if "connectors/official_website_http/src/official_website_http" not in text:
    marker = '  "connectors/osm_overpass/src/osm_overpass",\n'
    if marker not in text:
        raise RuntimeError("mypy connector marker is missing")
    text = text.replace(
        marker,
        '  "connectors/official_website_http/src/official_website_http",\n' + marker,
        1,
    )
workspace.write_text(text, encoding="utf-8")

checker = ROOT / "tools/architecture_checks/check_dependencies.py"
text = checker.read_text(encoding="utf-8")
if '"official_website_http": OwnerPolicy(' not in text:
    marker = '    "osm_overpass": OwnerPolicy(\n'
    if marker not in text:
        raise RuntimeError("OSM owner marker is missing")
    text = text.replace(
        marker,
        '''    "official_website_http": OwnerPolicy(
        project_path="connectors/official_website_http",
        distribution_name="official-website-http-connector",
        allowed_internal_imports=(),
        allowed_external_imports=frozenset(),
    ),
''' + marker,
        1,
    )
checker.write_text(text, encoding="utf-8")

policy = subprocess.check_output(
    ["python", str(checker), "--print-policy"],
    text=True,
).strip()
policy_path = ROOT / "docs/architecture/dependency-rules.md"
text = policy_path.read_text(encoding="utf-8")
start_marker = "<!-- dependency-policy:start -->"
end_marker = "<!-- dependency-policy:end -->"
start = text.index(start_marker)
end = text.index(end_marker, start) + len(end_marker)
policy_path.write_text(text[:start] + policy + text[end:], encoding="utf-8")
