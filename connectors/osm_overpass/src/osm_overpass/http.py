from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from hashlib import sha256
from types import TracebackType
from typing import Literal, Protocol, Self, cast
from urllib.parse import urlencode, urlsplit

import httpx

from osm_overpass.contracts import OverpassQuerySpec
from osm_overpass.query import build_overpass_query

type OverpassFailureKind = Literal[
    "transient",
    "permanent",
    "policy_blocked",
    "contract_invalid",
]

_MAX_QUERY_BYTES = 256 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_CONTRACT_STATUSES = frozenset({400, 405, 413, 414, 422})
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
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        }
        return tuple(sorted(addresses))


@dataclass(frozen=True, slots=True)
class OverpassEndpointPolicy:
    endpoint_url: str
    allowed_hosts: tuple[str, ...]
    user_agent: str
    timeout_seconds: float = 90.0
    maximum_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint_url)
        if parsed.scheme != "https":
            raise ValueError("Overpass endpoint must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Overpass endpoint cannot contain user information")
        if not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("Overpass endpoint URL is invalid")
        if parsed.path not in {"/api/interpreter", "/interpreter"}:
            raise ValueError("Overpass endpoint path is not allowlisted")
        normalized_hosts = tuple(sorted(set(self.allowed_hosts)))
        if not normalized_hosts or normalized_hosts != self.allowed_hosts:
            raise ValueError("Overpass allowed hosts must be unique, sorted, and non-empty")
        if parsed.hostname not in self.allowed_hosts:
            raise ValueError("Overpass endpoint host is not allowlisted")
        if not self.user_agent or len(self.user_agent) > 256:
            raise ValueError("Overpass User-Agent is missing or too large")
        if "\r" in self.user_agent or "\n" in self.user_agent:
            raise ValueError("Overpass User-Agent contains a forbidden character")
        if not 1 <= self.timeout_seconds <= 300:
            raise ValueError("Overpass timeout must be between 1 and 300 seconds")
        if not 1_024 <= self.maximum_response_bytes <= _DEFAULT_MAX_RESPONSE_BYTES:
            raise ValueError("Overpass response byte limit is outside the supported range")


@dataclass(frozen=True, slots=True)
class OverpassFetchResult:
    body: bytes
    content_digest: str
    content_type: str

    def __post_init__(self) -> None:
        expected_digest = f"sha256:{sha256(self.body).hexdigest()}"
        if self.content_digest != expected_digest:
            raise ValueError("Overpass fetch digest does not match the response body")
        if self.content_type != "application/json":
            raise ValueError("Overpass fetch content type must be application/json")


class OverpassFetchFailure(RuntimeError):
    def __init__(
        self,
        *,
        kind: OverpassFailureKind,
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


class OverpassHttpClient:
    def __init__(
        self,
        policy: OverpassEndpointPolicy,
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
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
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

    def fetch(self, spec: OverpassQuerySpec) -> OverpassFetchResult:
        query = build_overpass_query(spec)
        query_bytes = query.encode("utf-8")
        if len(query_bytes) > _MAX_QUERY_BYTES:
            raise ValueError("Overpass query exceeds the byte limit")
        parsed = urlsplit(self._policy.endpoint_url)
        host = cast(str, parsed.hostname)
        port = parsed.port or 443
        _require_public_addresses(self._resolver.resolve(host, port), owner="DNS")
        request_body = urlencode({"data": query}).encode("utf-8")
        try:
            with self._client.stream(
                "POST",
                self._policy.endpoint_url,
                content=request_body,
            ) as response:
                _require_public_addresses(
                    self._peer_address_reader(response),
                    owner="connected peer",
                )
                failure = _status_failure(response)
                if failure is not None:
                    raise failure
                content_type = response.headers.get("content-type", "")
                normalized_content_type = content_type.split(";", 1)[0].strip().lower()
                if normalized_content_type != "application/json":
                    raise OverpassFetchFailure(
                        kind="contract_invalid",
                        code="OVERPASS_CONTENT_TYPE_INVALID",
                        message="Overpass returned an unsupported content type.",
                        status_code=response.status_code,
                    )
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
        except OverpassFetchFailure:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise OverpassFetchFailure(
                kind="transient",
                code="OVERPASS_TRANSPORT_FAILURE",
                message="The Overpass endpoint could not be reached.",
            ) from exc
        return OverpassFetchResult(
            body=body,
            content_digest=f"sha256:{sha256(body).hexdigest()}",
            content_type="application/json",
        )


def _status_failure(response: httpx.Response) -> OverpassFetchFailure | None:
    status = response.status_code
    if status == 200:
        return None
    retry_after = _retry_after_seconds(response.headers.get("retry-after"))
    if status in _TRANSIENT_STATUSES:
        return OverpassFetchFailure(
            kind="transient",
            code="OVERPASS_TEMPORARILY_UNAVAILABLE",
            message="The Overpass endpoint is temporarily unavailable.",
            status_code=status,
            retry_after_seconds=retry_after,
        )
    if status in _CONTRACT_STATUSES:
        return OverpassFetchFailure(
            kind="contract_invalid",
            code="OVERPASS_REQUEST_REJECTED",
            message="The Overpass endpoint rejected the typed request.",
            status_code=status,
        )
    if status in _POLICY_STATUSES:
        return OverpassFetchFailure(
            kind="policy_blocked",
            code="OVERPASS_ACCESS_BLOCKED",
            message="The Overpass endpoint rejected access by policy.",
            status_code=status,
        )
    return OverpassFetchFailure(
        kind="permanent",
        code="OVERPASS_HTTP_FAILURE",
        message="The Overpass endpoint returned an unsupported status.",
        status_code=status,
    )


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None:
        return None
    try:
        result = int(value)
    except ValueError as exc:
        raise OverpassFetchFailure(
            kind="contract_invalid",
            code="OVERPASS_CONTENT_LENGTH_INVALID",
            message="Overpass returned an invalid Content-Length header.",
            status_code=response.status_code,
        ) from exc
    if result < 0:
        raise OverpassFetchFailure(
            kind="contract_invalid",
            code="OVERPASS_CONTENT_LENGTH_INVALID",
            message="Overpass returned an invalid Content-Length header.",
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


def _response_too_large() -> OverpassFetchFailure:
    return OverpassFetchFailure(
        kind="contract_invalid",
        code="OVERPASS_RESPONSE_TOO_LARGE",
        message="The Overpass response exceeds the configured byte limit.",
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
        raise OverpassFetchFailure(
            kind="policy_blocked",
            code="OVERPASS_ADDRESS_UNRESOLVED",
            message=f"The Overpass {owner} address could not be verified.",
        )
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise OverpassFetchFailure(
                kind="policy_blocked",
                code="OVERPASS_ADDRESS_INVALID",
                message=f"The Overpass {owner} address is invalid.",
            ) from exc
        if not address.is_global:
            raise OverpassFetchFailure(
                kind="policy_blocked",
                code="OVERPASS_PRIVATE_ADDRESS_BLOCKED",
                message=f"The Overpass {owner} address is not globally routable.",
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
