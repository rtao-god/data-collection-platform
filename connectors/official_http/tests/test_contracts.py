from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from official_http import (
    HttpAcquisitionManifest,
    OfficialHttpError,
    OfficialHttpRequest,
    ResponseHeader,
    decode_http_request,
)


def _document(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "contract": "official-http-request",
        "contractRevision": "official-http-request-v1",
        "requestId": "request-1",
        "sourceKey": "source-official-example",
        "sourcePolicyDigest": "sha256:" + "2" * 64,
        "requestKind": "page",
        "url": "https://example.com/services?a=1",
        "allowedOrigin": "https://example.com",
        "userAgent": "DataCollectionPlatform/1 (+https://example.invalid/bot)",
        "timeoutSeconds": 30,
        "maximumEncodedBytes": 1048576,
        "maximumDecodedBytes": 2097152,
        "depth": 0,
        "maximumDiscoveredUrls": 25,
        "trackingQueryParameters": ["utm_source"],
        "pageInterests": [
            {"key": "services", "tokens": ["leistungen", "services"], "priority": 100}
        ],
        "robotsAllowed": True,
        "robotsArtifactId": "00000000-0000-0000-0000-000000000006",
        "robotsDecisionDigest": "sha256:" + "6" * 64,
        "ifNoneMatch": None,
        "ifModifiedSince": None,
        "priorArtifactId": None,
        "priorContentDigest": None,
    }
    value.update(overrides)
    return value


def _body(**overrides: object) -> bytes:
    return json.dumps(_document(**overrides), separators=(",", ":")).encode()


def test_request_round_trip_is_canonical_and_deterministic() -> None:
    request = decode_http_request(_body())
    assert request == OfficialHttpRequest.model_validate_json(request.to_bytes(), strict=True)
    assert request.digest.startswith("sha256:")
    assert request.to_bytes() == decode_http_request(request.to_bytes()).to_bytes()


def test_request_rejects_duplicate_keys_before_pydantic_validation() -> None:
    body = b'{"contract":"official-http-request","contract":"other"}'
    with pytest.raises(OfficialHttpError) as failure:
        decode_http_request(body)
    assert failure.value.code == "OFFICIAL_HTTP_REQUEST_DUPLICATE_KEY"


def test_non_robots_request_requires_explicit_allowed_decision() -> None:
    with pytest.raises(OfficialHttpError) as failure:
        decode_http_request(_body(robotsAllowed=False))
    assert failure.value.code == "OFFICIAL_HTTP_REQUEST_CONTRACT_INVALID"


def test_conditional_request_requires_exact_prior_artifact_pair() -> None:
    with pytest.raises(OfficialHttpError):
        decode_http_request(_body(ifNoneMatch='"etag"'))

    request = decode_http_request(
        _body(
            ifNoneMatch='"etag"',
            priorArtifactId="00000000-0000-0000-0000-000000000001",
            priorContentDigest="sha256:" + "1" * 64,
        )
    )
    assert request.prior_artifact_id == UUID(int=1)


def test_manifest_enforces_unchanged_prior_identity() -> None:
    manifest = HttpAcquisitionManifest(
        requestId="request-1",
        sourceKey="source-official-example",
        sourcePolicyDigest="sha256:" + "2" * 64,
        requestKind="page",
        requestedUrl="https://example.com/services",
        finalUrl="https://example.com/services",
        outcome="unchanged",
        statusCode=304,
        responseHeaders=(ResponseHeader(name="etag", value='"etag"'),),
        remoteIpAddress="93.184.216.34",
        encodedSizeBytes=0,
        decodedSizeBytes=0,
        observedAtUtc=datetime(2026, 8, 14, tzinfo=UTC),
        reusedArtifactId=UUID(int=1),
        reusedContentDigest="sha256:" + "1" * 64,
    )
    assert json.loads(manifest.to_bytes())["outcome"] == "unchanged"


def test_manifest_rejects_fetched_without_raw_artifact() -> None:
    with pytest.raises(ValueError):
        HttpAcquisitionManifest(
            requestId="request-1",
            sourceKey="source-official-example",
            requestKind="page",
            requestedUrl="https://example.com/services",
            finalUrl="https://example.com/services",
            outcome="fetched",
            statusCode=200,
            responseHeaders=(),
            remoteIpAddress="93.184.216.34",
            encodedSizeBytes=1,
            decodedSizeBytes=1,
            observedAtUtc=datetime(2026, 8, 14, tzinfo=UTC),
        )
