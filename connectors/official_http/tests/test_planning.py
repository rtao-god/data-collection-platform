from __future__ import annotations

from uuid import UUID

import pytest

from official_http import (
    OfficialHttpError,
    OfficialHttpRequest,
    evaluate_robots,
    plan_html,
    plan_sitemap,
)


def _request(kind: str = "page") -> OfficialHttpRequest:
    return OfficialHttpRequest(
        requestId="request-1",
        sourceKey="source-official-example",
        sourcePolicyDigest="sha256:" + "2" * 64,
        requestKind=kind,
        url={
            "robots": "https://example.com/robots.txt",
            "sitemap": "https://example.com/sitemap.xml",
            "page": "https://example.com/",
        }[kind],
        allowedOrigin="https://example.com",
        userAgent="DataCollectionPlatform/1",
        timeoutSeconds=30,
        maximumEncodedBytes=1048576,
        maximumDecodedBytes=2097152,
        maximumDiscoveredUrls=10,
        trackingQueryParameters=("utm_source",),
        pageInterests=(
            {"key": "contact", "tokens": ("contact", "kontakt"), "priority": 100},
            {"key": "prices", "tokens": ("preise", "prices"), "priority": 90},
        )
        if kind == "page"
        else (),
        robotsAllowed=True if kind != "robots" else None,
        robotsArtifactId=UUID(int=6) if kind != "robots" else None,
        robotsDecisionDigest=("sha256:" + "6" * 64) if kind != "robots" else None,
    )


def test_robots_evaluation_is_explicit_and_discovers_same_origin_sitemap() -> None:
    body = b"User-agent: *\nDisallow: /private\nSitemap: https://example.com/sitemap.xml\n"
    result = evaluate_robots(body, request=_request("robots"))
    assert result.allowed is True
    assert result.sitemap_urls == ("https://example.com/sitemap.xml",)


def test_sitemap_planning_is_same_origin_bounded_and_deterministic() -> None:
    body = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>https://example.com/contact?utm_source=x</loc></url>
    <url><loc>https://other.example/prices</loc></url>
    <url><loc>https://example.com/prices</loc></url>
    </urlset>"""
    request = _request("sitemap").model_copy(
        update={"page_interests": _request("page").page_interests}
    )
    plan = plan_sitemap(body, request=request)
    assert [item.url for item in plan.resources] == [
        "https://example.com/contact",
        "https://example.com/prices",
    ]


def test_sitemap_rejects_dtd() -> None:
    with pytest.raises(OfficialHttpError) as failure:
        plan_sitemap(b"<!DOCTYPE x><urlset />", request=_request("sitemap"))
    assert failure.value.code == "OFFICIAL_HTTP_SITEMAP_DTD_FORBIDDEN"


def test_html_planning_prioritizes_interests_and_keeps_canonical_as_observation() -> None:
    body = b"""<html><head><link rel="canonical" href="/studio" /></head><body>
    <a href="/about">About</a><a href="/kontakt?utm_source=x">Contact</a>
    <a href="https://other.example/prices">Other</a></body></html>"""
    plan = plan_html(body, request=_request("page"))
    assert [item.url for item in plan.resources] == ["https://example.com/kontakt"]
    assert plan.canonical_observation == "https://example.com/studio"
