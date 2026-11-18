"""Bounded, policy-honouring read of one Wikidata entity request.

The endpoint is not discovered and not configurable into a crawl: the request contract
already fixes the host and the exact `wbgetentities` question, this module only performs
that one GET. Every non-200 answer is a typed refusal, and the response is read into a
bounded buffer so a huge body cannot exhaust the worker.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from hashlib import sha256
from types import TracebackType
from typing import Literal, Protocol, Self, cast
from urllib.parse import SplitResult, parse_qsl, urlsplit

import httpx

from wikidata_studios.request import WikidataEntityRequest

type WikidataFailureKind = Literal[
    "transient",
    "permanent",
    "policy_blocked",
    "contract_invalid",
]

_DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_API_PATH = "/w/api.php"
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_CONTRACT_STATUSES = frozenset({400, 404, 405, 413, 414, 422})
_POLICY_STATUSES = frozenset({401, 403, 407, 451})


class AddressResolver(Protocol):
    def resolve(self, host: str, port: int) -> Sequence[str]: ...


class PeerAddressReader(Protocol):
    def __call__(self, response: httpx.Response) -> Sequence[str]: ...


class NetworkStreamInfo(Protocol):
    def get_extra_info(self, info: str) -> object: ...


class SystemAddressResolver:
    def resolve(self, host: str, port: int) -> Sequence[str]:
        addresses = {
            str(item[4][0])
            for item in socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        }
        return tuple(sorted(addresses))


@dataclass(frozen=True, slots=True)
class WikidataEndpointPolicy:
    """The pinned transport contract for the entity read."""

    allowed_hosts: tuple[str, ...]
    user_agent: str
    timeout_seconds: float = 60.0
    maximum_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        normalized_hosts = tuple(sorted(set(self.allowed_hosts)))
        if not normalized_hosts or normalized_hosts != self.allowed_hosts:
            raise ValueError("Wikidata allowed hosts must be unique, sorted, and non-empty")
        if not self.user_agent or len(self.user_agent) > 256:
            raise ValueError("Wikidata User-Agent is missing or too large")
        if "\r" in self.user_agent or "\n" in self.user_agent:
            raise ValueError("Wikidata User-Agent contains a forbidden character")
        if not 1 <= self.timeout_seconds <= 300:
            raise ValueError("Wikidata timeout must be between 1 and 300 seconds")
        if not 1_024 <= self.maximum_response_bytes <= _DEFAULT_MAX_RESPONSE_BYTES:
            raise ValueError("Wikidata response byte limit is outside the supported range")


@dataclass(frozen=True, slots=True)
class WikidataFetchResult:
    body: bytes
    content_digest: str
    content_type: str | None

    def __post_init__(self) -> None:
        expected_digest = f"sha256:{sha256(self.body).hexdigest()}"
        if self.content_digest != expected_digest:
            raise ValueError("Wikidata fetch digest does not match the response body")


class WikidataFetchFailure(RuntimeError):
    def __init__(
        self,
        *,
        kind: WikidataFailureKind,
        code: str,
        message: str,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.kind = kind
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class WikidataHttpClient:
    def __init__(
        self,
        policy: WikidataEndpointPolicy,
        *,
        resolver: AddressResolver | None = None,
        peer_address_reader: PeerAddressReader | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._policy = policy
        self._resolver = resolver or SystemAddressResolver()
        self._peer_address_reader = peer_address_reader or _httpx_peer_addresses
        self._client = httpx.Client(
            timeout=httpx.Timeout(policy.timeout_seconds),
            follow_redirects=False,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": policy.user_agent,
            },
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch(self, request: WikidataEntityRequest) -> WikidataFetchResult:
        url = request.entity_ids_query()
        parsed = urlsplit(url)
        host = _require_allowed_endpoint(parsed, policy=self._policy)
        port = parsed.port or 443
        _require_public_addresses(self._resolver.resolve(host, port), owner="DNS")
        try:
            with self._client.stream("GET", url) as response:
                _require_public_addresses(
                    self._peer_address_reader(response),
                    owner="connected peer",
                )
                failure = _status_failure(response)
                if failure is not None:
                    raise failure
                content_type = response.headers.get("content-type", "")
                content_length = _content_length(response)
                if (
                    content_length is not None
                    and content_length > self._policy.maximum_response_bytes
                ):
                    raise _response_too_large()
                body = _read_bounded(
                    response.iter_bytes(),
                    maximum_bytes=self._policy.maximum_response_bytes,
                )
        except WikidataFetchFailure:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise WikidataFetchFailure(
                kind="transient",
                code="WIKIDATA_TRANSPORT_FAILURE",
                message="The Wikidata entity endpoint could not be reached.",
            ) from exc
        normalized_content_type = content_type.split(";", 1)[0].strip().lower()
        # MediaWiki answers JSON for `format=json` but may label it as text/plain; any
        # other declared type means the request was not served as asked.
        if normalized_content_type and normalized_content_type not in {
            "application/json",
            "text/json",
            "text/plain",
        }:
            raise WikidataFetchFailure(
                kind="contract_invalid",
                code="WIKIDATA_CONTENT_TYPE_INVALID",
                message="The Wikidata endpoint returned an unsupported content type.",
                status_code=200,
            )
        return WikidataFetchResult(
            body=body,
            content_digest=f"sha256:{sha256(body).hexdigest()}",
            content_type=normalized_content_type or None,
        )


def _require_allowed_endpoint(parsed: SplitResult, *, policy: WikidataEndpointPolicy) -> str:
    """Refuse any URL the connector contract does not pin exactly."""

    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        raise WikidataFetchFailure(
            kind="policy_blocked",
            code="WIKIDATA_ENDPOINT_BLOCKED",
            message="The Wikidata request address is not an allowed HTTPS endpoint.",
        )
    if not parsed.hostname or parsed.hostname not in policy.allowed_hosts:
        raise WikidataFetchFailure(
            kind="policy_blocked",
            code="WIKIDATA_ENDPOINT_BLOCKED",
            message="The Wikidata request host is not allowlisted.",
        )
    if parsed.path != _API_PATH or parsed.fragment:
        raise WikidataFetchFailure(
            kind="policy_blocked",
            code="WIKIDATA_ENDPOINT_BLOCKED",
            message="The Wikidata request path is not the exact entity API endpoint.",
        )
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if set(query) != {"action", "format", "ids", "props"} or query["action"] != "wbgetentities":
        raise WikidataFetchFailure(
            kind="contract_invalid",
            code="WIKIDATA_REQUEST_SHAPE_INVALID",
            message="The Wikidata request does not describe one bounded entity read.",
        )
    hostname = parsed.hostname
    return hostname


def _status_failure(response: httpx.Response) -> WikidataFetchFailure | None:
    status = response.status_code
    if status == 200:
        return None
    retry_after = _retry_after_seconds(response.headers.get("retry-after"))
    if status in _TRANSIENT_STATUSES:
        return WikidataFetchFailure(
            kind="transient",
            code="WIKIDATA_TEMPORARILY_UNAVAILABLE",
            message="The Wikidata endpoint is temporarily unavailable.",
            status_code=status,
            retry_after_seconds=retry_after,
        )
    if status in _CONTRACT_STATUSES:
        return WikidataFetchFailure(
            kind="contract_invalid",
            code="WIKIDATA_REQUEST_REJECTED",
            message="The Wikidata endpoint rejected the typed request.",
            status_code=status,
        )
    if status in _POLICY_STATUSES:
        return WikidataFetchFailure(
            kind="policy_blocked",
            code="WIKIDATA_ACCESS_BLOCKED",
            message="The Wikidata endpoint rejected access by policy.",
            status_code=status,
        )
    return WikidataFetchFailure(
        kind="permanent",
        code="WIKIDATA_HTTP_FAILURE",
        message="The Wikidata endpoint returned an unsupported status.",
        status_code=status,
    )


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None:
        return None
    try:
        result = int(value)
    except ValueError as exc:
        raise WikidataFetchFailure(
            kind="contract_invalid",
            code="WIKIDATA_CONTENT_LENGTH_INVALID",
            message="The Wikidata endpoint returned an invalid Content-Length header.",
            status_code=response.status_code,
        ) from exc
    if result < 0:
        raise WikidataFetchFailure(
            kind="contract_invalid",
            code="WIKIDATA_CONTENT_LENGTH_INVALID",
            message="The Wikidata endpoint returned an invalid Content-Length header.",
            status_code=response.status_code,
        )
    return result


def _read_bounded(chunks: Iterator[bytes], *, maximum_bytes: int) -> bytes:
    body = bytearray()
    for chunk in chunks:
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise _response_too_large()
    return bytes(body)


def _response_too_large() -> WikidataFetchFailure:
    return WikidataFetchFailure(
        kind="contract_invalid",
        code="WIKIDATA_RESPONSE_TOO_LARGE",
        message="The Wikidata response exceeds the configured byte limit.",
    )


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except ValueError:
        return None
    return result if 0 <= result <= 86_400 else None


def _require_public_addresses(addresses: Sequence[str], *, owner: str) -> None:
    if not addresses:
        raise WikidataFetchFailure(
            kind="policy_blocked",
            code="WIKIDATA_ADDRESS_UNRESOLVED",
            message=f"The Wikidata {owner} address could not be verified.",
        )
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise WikidataFetchFailure(
                kind="policy_blocked",
                code="WIKIDATA_ADDRESS_INVALID",
                message=f"The Wikidata {owner} address is invalid.",
            ) from exc
        if not address.is_global:
            raise WikidataFetchFailure(
                kind="policy_blocked",
                code="WIKIDATA_PRIVATE_ADDRESS_BLOCKED",
                message=f"The Wikidata {owner} address is not globally routable.",
            )


def _httpx_peer_addresses(response: httpx.Response) -> Sequence[str]:
    direct = response.extensions.get("server_addr")
    if isinstance(direct, tuple) and direct and isinstance(direct[0], str):
        return (direct[0],)
    stream_value = response.extensions.get("network_stream")
    if stream_value is None or not hasattr(stream_value, "get_extra_info"):
        return ()
    stream = cast(NetworkStreamInfo, stream_value)
    address = stream.get_extra_info("server_addr")
    if isinstance(address, tuple) and address and isinstance(address[0], str):
        return (address[0],)
    return ()
