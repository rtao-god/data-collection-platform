from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from defusedxml.ElementTree import (  # type: ignore[import-untyped]
    ParseError,
    fromstring,
)

from official_http.contracts import DiscoveredResource, OfficialHttpRequest
from official_http.errors import OfficialHttpError
from official_http.urls import canonical_origin, normalize_http_url


@dataclass(frozen=True, slots=True)
class RobotsEvaluation:
    allowed: bool
    sitemap_urls: tuple[str, ...]
    crawl_delay_seconds: float | None


@dataclass(frozen=True, slots=True)
class PagePlan:
    resources: tuple[DiscoveredResource, ...]
    canonical_observation: str | None


def evaluate_robots(body: bytes, *, request: OfficialHttpRequest) -> RobotsEvaluation:
    text = _decode_text(body, owner="robots.txt")
    parser = RobotFileParser()
    parser.set_url(request.url)
    parser.parse(text.splitlines())
    allowed = parser.can_fetch(request.user_agent, request.allowed_origin + "/")
    delay = parser.crawl_delay(request.user_agent)
    sitemaps: list[str] = []
    for line in text.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip().casefold() == "sitemap":
            candidate = _same_origin_candidate(value.strip(), request=request)
            if candidate is not None:
                sitemaps.append(candidate)
    return RobotsEvaluation(
        allowed=allowed,
        sitemap_urls=tuple(sorted(set(sitemaps))),
        crawl_delay_seconds=float(delay) if delay is not None else None,
    )


def plan_sitemap(body: bytes, *, request: OfficialHttpRequest) -> PagePlan:
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_SITEMAP_DTD_FORBIDDEN",
            message="The sitemap contains a forbidden DTD or entity declaration.",
        )
    try:
        root = fromstring(body)
    except ParseError as exc:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_SITEMAP_XML_INVALID",
            message="The sitemap is not valid XML.",
        ) from exc
    root_name = _local_name(root.tag)
    if root_name not in {"urlset", "sitemapindex"}:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_SITEMAP_ROOT_UNSUPPORTED",
            message="The sitemap root element is unsupported.",
            context={"root": root_name},
        )
    kind: Literal["page", "sitemap"] = "page" if root_name == "urlset" else "sitemap"
    urls: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) != "loc" or element.text is None:
            continue
        candidate = _same_origin_candidate(element.text.strip(), request=request)
        if candidate is not None:
            urls.append(candidate)
    return PagePlan(
        resources=_rank_resources(urls, kind=kind, request=request),
        canonical_observation=None,
    )


def plan_html(body: bytes, *, request: OfficialHttpRequest) -> PagePlan:
    parser = _LinkParser()
    parser.feed(_decode_text(body, owner="HTML response"))
    urls: list[str] = []
    for href in parser.links:
        candidate = _same_origin_candidate(urljoin(request.url, href), request=request)
        if candidate is not None:
            urls.append(candidate)
    canonical = None
    if parser.canonical is not None:
        canonical = _same_origin_candidate(urljoin(request.url, parser.canonical), request=request)
    return PagePlan(
        resources=_rank_resources(urls, kind="page", request=request),
        canonical_observation=canonical,
    )


def _rank_resources(
    urls: list[str],
    *,
    kind: Literal["sitemap", "page"],
    request: OfficialHttpRequest,
) -> tuple[DiscoveredResource, ...]:
    by_url: dict[str, DiscoveredResource] = {}
    for url in urls:
        score, interest_key = _interest_score(url, request=request)
        if kind == "page" and score == 0:
            continue
        item = DiscoveredResource(
            url=url,
            resource_kind=kind,
            interest_key=interest_key,
            score=score,
        )
        previous = by_url.get(url)
        if previous is None or item.score > previous.score:
            by_url[url] = item
    ordered = sorted(by_url.values(), key=lambda item: (-item.score, item.url))
    return tuple(ordered[: request.maximum_discovered_urls])


def _interest_score(url: str, *, request: OfficialHttpRequest) -> tuple[int, str | None]:
    searchable = f"{urlsplit(url).path}?{urlsplit(url).query}".casefold()
    matches = [
        (rule.priority, rule.key)
        for rule in request.page_interests
        if any(token in searchable for token in rule.tokens)
    ]
    if not matches:
        return 0, None
    matches.sort(key=lambda item: (-item[0], item[1]))
    return matches[0]


def _same_origin_candidate(value: str, *, request: OfficialHttpRequest) -> str | None:
    try:
        normalized = normalize_http_url(
            value,
            tracking_parameters=request.tracking_query_parameters,
        )
    except OfficialHttpError:
        return None
    if canonical_origin(normalized) != request.allowed_origin:
        return None
    return normalized


def _decode_text(body: bytes, *, owner: str) -> str:
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_TEXT_ENCODING_INVALID",
            message=f"The {owner} is not valid UTF-8.",
        ) from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.canonical: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs if value is not None}
        if tag.casefold() == "a" and "href" in values:
            self.links.append(values["href"])
        if tag.casefold() == "link" and "canonical" in values.get("rel", "").casefold().split():
            self.canonical = values.get("href")
