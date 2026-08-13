from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest
from osm_overpass.http import (
    OverpassEndpointPolicy,
    OverpassFetchFailure,
    OverpassHttpClient,
)

from osm_overpass import (
    GeoPoint,
    OsmTagFilter,
    OverpassPolygon,
    OverpassQuerySpec,
)


class Resolver:
    def __init__(self, addresses: Sequence[str]) -> None:
        self.addresses = tuple(addresses)
        self.calls: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int) -> Sequence[str]:
        self.calls.append((host, port))
        return self.addresses


def policy(*, maximum_response_bytes: int = 1024) -> OverpassEndpointPolicy:
    return OverpassEndpointPolicy(
        endpoint_url="https://overpass.example/api/interpreter",
        allowed_hosts=("overpass.example",),
        user_agent="data-collection-platform/0.1 contact=operator@example.test",
        maximum_response_bytes=maximum_response_bytes,
    )


def spec() -> OverpassQuerySpec:
    return OverpassQuerySpec(
        polygon=OverpassPolygon(points=(GeoPoint(1, 1), GeoPoint(1, 2), GeoPoint(2, 1))),
        element_types=("node",),
        tag_filters=(OsmTagFilter(key="amenity", values=("studio",)),),
    )


def peer(_: httpx.Response) -> Sequence[str]:
    return ("1.1.1.1",)


def test_client_posts_typed_query_without_redirects_or_credential_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"elements":[]}',
        )

    resolver = Resolver(("1.1.1.1",))
    with OverpassHttpClient(
        policy(),
        resolver=resolver,
        peer_address_reader=peer,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.fetch(spec())

    assert result.body == b'{"elements":[]}'
    assert resolver.calls == [("overpass.example", 443)]
    request = requests[0]
    assert request.method == "POST"
    assert request.url == httpx.URL("https://overpass.example/api/interpreter")
    assert b"data=%5Bout%3Ajson%5D" in request.content
    assert "authorization" not in request.headers


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "::1", "169.254.1.1"])
def test_client_blocks_non_global_dns_addresses_before_transport(address: str) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"elements": []})

    with (
        OverpassHttpClient(
            policy(),
            resolver=Resolver((address,)),
            peer_address_reader=peer,
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(OverpassFetchFailure) as error,
    ):
        client.fetch(spec())
    assert error.value.code == "OVERPASS_PRIVATE_ADDRESS_BLOCKED"
    assert called is False


def test_client_revalidates_the_connected_peer_address() -> None:
    with (
        OverpassHttpClient(
            policy(),
            resolver=Resolver(("1.1.1.1",)),
            peer_address_reader=lambda _: ("127.0.0.1",),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"elements": []})),
        ) as client,
        pytest.raises(OverpassFetchFailure) as error,
    ):
        client.fetch(spec())
    assert error.value.kind == "policy_blocked"
    assert error.value.code == "OVERPASS_PRIVATE_ADDRESS_BLOCKED"


def test_429_is_typed_transient_and_does_not_expose_the_body() -> None:
    secret_body = "upstream internal diagnostic"
    with (
        OverpassHttpClient(
            policy(),
            resolver=Resolver(("1.1.1.1",)),
            peer_address_reader=peer,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    429,
                    headers={"Retry-After": "30"},
                    text=secret_body,
                )
            ),
        ) as client,
        pytest.raises(OverpassFetchFailure) as error,
    ):
        client.fetch(spec())
    assert error.value.kind == "transient"
    assert error.value.retry_after_seconds == 30
    assert secret_body not in str(error.value)


def test_decompressed_response_limit_is_enforced_while_streaming() -> None:
    with (
        OverpassHttpClient(
            policy(maximum_response_bytes=1024),
            resolver=Resolver(("1.1.1.1",)),
            peer_address_reader=peer,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    content=b"x" * 1025,
                )
            ),
        ) as client,
        pytest.raises(OverpassFetchFailure) as error,
    ):
        client.fetch(spec())
    assert error.value.code == "OVERPASS_RESPONSE_TOO_LARGE"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://overpass.example/api/interpreter",
        "https://overpass.example/arbitrary",
        "https://user@overpass.example/api/interpreter",
        "https://overpass.example/api/interpreter?query=x",
    ],
)
def test_endpoint_policy_rejects_non_allowlisted_shapes(endpoint: str) -> None:
    with pytest.raises(ValueError):
        OverpassEndpointPolicy(
            endpoint_url=endpoint,
            allowed_hosts=("overpass.example",),
            user_agent="data-collection-platform/0.1",
        )
