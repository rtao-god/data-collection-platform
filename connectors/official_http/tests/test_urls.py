from __future__ import annotations

import socket

import pytest

from official_http import (
    OfficialHttpError,
    canonical_origin,
    normalize_http_url,
    resolve_public_addresses,
)


def test_url_normalization_preserves_meaning_and_removes_only_allowlisted_tracking() -> None:
    normalized = normalize_http_url(
        "HTTPS://Exämple.COM:443/a/../services/?z=2&utm_source=x&a=1#fragment",
        tracking_parameters=("utm_source",),
    )
    assert normalized == "https://xn--exmple-cua.com/services/?a=1&z=2"
    assert canonical_origin(normalized) == "https://xn--exmple-cua.com"
    assert normalize_http_url(normalized, tracking_parameters=("utm_source",)) == normalized


def test_url_normalization_rejects_credentials_and_non_http_schemes() -> None:
    with pytest.raises(OfficialHttpError):
        normalize_http_url("file:///etc/passwd")
    with pytest.raises(OfficialHttpError):
        normalize_http_url("https://user:secret@example.com/")


def test_resolution_rejects_private_or_mixed_address_sets() -> None:
    def resolver(
        *args: object, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
        del args, kwargs
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    with pytest.raises(OfficialHttpError) as failure:
        resolve_public_addresses("https://example.com/", resolver=resolver)
    assert failure.value.code == "OFFICIAL_HTTP_REMOTE_ADDRESS_FORBIDDEN"


def test_resolution_returns_sorted_public_addresses() -> None:
    def resolver(
        *args: object, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
        del args, kwargs
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]

    assert resolve_public_addresses("https://example.com/", resolver=resolver) == (
        "1.1.1.1",
        "8.8.8.8",
    )
