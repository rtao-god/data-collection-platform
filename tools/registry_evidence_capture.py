"""Capture one official-site artifact per requested fact, using the DCPF acquisition owner.

The request contract, URL rules, robots interpretation and the single Scrapy run all belong to
``connectors/official_http``; this tool only supplies the campaign policy limits, keeps the pacing
the policy states, and reports the exact bytes it received. It writes a capture manifest and the
raw bodies; it never edits the registry, never assigns a policy decision and never approves
anything.

Excerpts are bounded spans of the artifact's own text around the facts the caller asked for. A
missing literal is a refusal, not a shorter excerpt: an artifact that does not state the fact
cannot become its evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import pathlib
import re
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

import yaml
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

POLICY = pathlib.Path(
    "D:/Aggregator/data-collection-platform/campaigns/berlin_recording_services/source_policies/"
    "official_site_berlin_studios_policy.yaml"
)
SOURCE_KEY = "official_site"
# The policy digest the operator recorded for this campaign source; a capture never invents one.
POLICY_DIGEST = "78ab20c8c5b13ec81b47a80171dc7912c6af739512092fda60ae47c215dd9d27"
USER_AGENT = "aggregator-catalog-research/1.0 (+https://aggregator.local/source-policy)"
CLIENTS = (
    "aggregator-catalog-research",
    "aggregator-catalog-research/1.0",
    "aggregator-catalog-research/1.0 (+https://aggregator.local/source-policy)",
)
EXCERPT_RADIUS = 300
MAX_EXCERPT = 400


class CaptureError(Exception):
    """A fact cannot be evidenced by this artifact, and the reason is explicit."""


def load_policy() -> dict[str, Any]:
    return yaml.safe_load(POLICY.read_text(encoding="utf-8"))


def _text_of(body: bytes, content_type: str) -> str:
    if "html" in content_type:
        raw = body.decode("utf-8", "replace")
        raw = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
        raw = re.sub(r"(?s)<!--.*?-->", " ", raw)
        raw = re.sub(r"(?s)<[^>]+>", " ", raw)
        return re.sub(r"\s+", " ", html.unescape(raw)).strip()
    return re.sub(r"\s+", " ", body.decode("utf-8", "replace")).strip()


JSONLD_BLOCK = re.compile(
    r"(?is)<script[^>]*\btype\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>"
)


def _normalized_fact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _jsonld_scalars(node: Any, pointer: str = "") -> list[tuple[str, str]]:
    """Every scalar an inline JSON-LD block states, with its JSON pointer.

    ``@context`` is never resolved and nothing outside the block is fetched: this reads the bytes
    the page already carries, nothing else.
    """
    found: list[tuple[str, str]] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}"
            if key == "@context":
                continue
            found.extend(_jsonld_scalars(value, child))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_jsonld_scalars(value, f"{pointer}/{index}"))
    elif isinstance(node, str):
        found.append((pointer, node))
    elif isinstance(node, bool) or node is None:
        return found
    elif isinstance(node, (int, float)):
        found.append((pointer, str(node)))
    return found


def _jsonld_subject_matches(block: Any, subject_name: str | None) -> bool:
    """The block must describe this subject when it names one; an unnamed block claims nothing."""
    if not subject_name:
        return True
    names = [
        value
        for _pointer, value in _jsonld_scalars(block)
        if _pointer.endswith("/name") or _pointer.endswith("/alternateName")
    ]
    if not names:
        return True
    wanted = _normalized_fact(subject_name)
    return any(_normalized_fact(name) == wanted for name in names)


def extract_jsonld_fact(
    body: bytes, literals: Sequence[str], *, subject_name: str | None = None
) -> dict[str, Any] | None:
    """Locate every literal in one inline JSON-LD block and report where each one sits.

    The result is structured, so a reviewer sees the block and the JSON pointer instead of a
    truncated prose quote. A block that names another subject is skipped: a fact of a different
    organization must not become this record's evidence.
    """
    source = body.decode("utf-8", "replace")
    for index, match in enumerate(JSONLD_BLOCK.finditer(source)):
        block_text = match.group(1).strip()
        try:
            block = json.loads(block_text)
        except json.JSONDecodeError:
            continue
        if not _jsonld_subject_matches(block, subject_name):
            continue
        scalars = _jsonld_scalars(block)
        pointers: dict[str, str] = {}
        for literal in literals:
            wanted = _normalized_fact(literal)
            hit = next(
                (pointer for pointer, value in scalars if wanted in _normalized_fact(value)), None
            )
            if hit is None:
                break
            pointers[literal] = hit
        else:
            excerpt = excerpt_around(block_text, literals)
            return {
                "medium": "application/ld+json",
                "blockIndex": index,
                "pointers": pointers,
                "excerpt": excerpt or block_text[:MAX_EXCERPT],
            }
    return None


def excerpt_around(text: str, literals: Sequence[str]) -> str | None:
    """A bounded span that quotes the stated facts, or None when the artifact does not state them.

    Every literal must appear in the artifact; the quote itself is bounded to MAX_EXCERPT and keeps
    the first literal verbatim, because a longer quote would be truncated into a non-quote.
    """
    folded = text.casefold()
    positions: list[int] = []
    for literal in literals:
        index = folded.find(literal.casefold())
        if index < 0:
            return None
        positions.append(index)
    start = max(0, min(positions) - EXCERPT_RADIUS)
    end = min(len(text), max(positions) + len(max(literals, key=len)) + EXCERPT_RADIUS)
    span = text[start:end].strip()
    if len(span) <= MAX_EXCERPT:
        return span
    first = min(positions)
    radius = max(40, (MAX_EXCERPT - len(min(literals, key=len))) // 2)
    window = text[max(0, first - radius) : min(len(text), first + radius)].strip()
    if not any(literal.casefold() in window.casefold() for literal in literals):
        return text[first : first + MAX_EXCERPT].strip()
    return window[:MAX_EXCERPT].strip()


def _robots_rules(body: bytes) -> tuple[list[str], float | None]:
    """Disallow prefixes and crawl delay of the groups that address '*' or this client.

    Groups are separated by a user-agent line that follows directives; a group's rules never leak
    into another group. A site that disallows the path for '*' or for this client is refused.
    """
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] = {"agents": [], "disallowed": [], "allowed": [], "delay": None}
    seen_rule = False
    for line in body.decode("utf-8", "replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        name, value = name.strip().lower(), value.strip()
        if name == "user-agent":
            if seen_rule:
                groups.append(current)
                current = {"agents": [], "disallowed": [], "allowed": [], "delay": None}
                seen_rule = False
            current["agents"].append(value.lower())
            continue
        seen_rule = True
        if name == "disallow" and value:
            current["disallowed"].append(value)
        elif name == "allow" and value:
            current["allowed"].append(value)
        elif name == "crawl-delay":
            try:
                current["delay"] = float(value)
            except ValueError:
                current["delay"] = None
    if current["agents"] or seen_rule:
        groups.append(current)

    disallowed: list[str] = []
    allowed: list[str] = []
    delay: float | None = None
    for group in groups:
        applies = (
            any(agent == "*" or agent in CLIENTS for agent in group["agents"])
            or not group["agents"]
        )
        if not applies:
            continue
        disallowed.extend(group["disallowed"])
        allowed.extend(group["allowed"])
        if delay is None:
            delay = group["delay"]
    kept = [
        prefix
        for prefix in disallowed
        if not any(rule.startswith(prefix) and len(rule) >= len(prefix) for rule in allowed)
    ]
    return kept, delay


def _robots_allows(body: bytes, path: str) -> bool:
    disallowed, _ = _robots_rules(body)
    return not any(path.startswith(prefix) for prefix in disallowed)


@dataclass(frozen=True)
class Fetched:
    url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes
    observed_at_utc: datetime

    @property
    def encoded_size_bytes(self) -> int:
        return len(self.body)


class PolicyFetcher:
    """One GET at a time, under the campaign policy's limits.

    The platform work engine registers robots/page artifacts and owns network acquisition for
    campaigns; registry research has no artifact store, so it reads the same pages directly under
    the same policy limits and records the bytes it observed. A refusal is reported, never retried.
    """

    def __init__(self, policy: Mapping[str, Any]) -> None:
        access = policy["access"]
        self._connect_timeout = float(access["connect_timeout_seconds"])
        self._read_timeout = float(access["read_timeout_seconds"])
        self._max_encoded = int(access["max_encoded_bytes"])
        self._user_agent = USER_AGENT

    def fetch(self, url: str) -> Fetched:
        request = urllib.request.Request(
            url, method="GET", headers={"User-Agent": self._user_agent, "Accept": "text/html"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._read_timeout) as response:
                body = response.read(self._max_encoded + 1)
                if len(body) > self._max_encoded:
                    raise CaptureError(f"response exceeds {self._max_encoded} bytes")
                return Fetched(
                    url=url,
                    final_url=response.geturl(),
                    status_code=response.status,
                    content_type=response.headers.get("content-type", ""),
                    body=body,
                    observed_at_utc=datetime.now(UTC).replace(microsecond=0),
                )
        except urllib.error.HTTPError as error:
            body = error.read(self._max_encoded + 1) if error.fp else b""
            return Fetched(
                url=url,
                final_url=error.geturl() or url,
                status_code=error.code,
                content_type=error.headers.get("content-type", "") if error.headers else "",
                body=body,
                observed_at_utc=datetime.now(UTC).replace(microsecond=0),
            )


class Pacer:
    """The campaign policy allows 0.5 request per second with one request in flight."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._last = 0.0
        self._sleep = time.sleep

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self._interval:
            self._sleep(self._interval - delta)
        self._last = time.monotonic()


def canonical_origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_http_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CaptureError(f"not an http(s) url: {url}")
    return url.strip()


def capture(
    requests: Sequence[Mapping[str, Any]],
    *,
    cache: pathlib.Path,
    fetcher: PolicyFetcher | None = None,
) -> dict[str, Any]:
    policy = load_policy()
    access = policy["access"]
    cache.mkdir(parents=True, exist_ok=True)
    pacer = Pacer(float(access["max_requests_per_second"]))
    allowed_types = tuple(str(item).lower() for item in access["content_type_allowlist"])
    stop_codes = tuple(int(code) for code in policy["stop_on_status_codes"])
    fetcher = fetcher or PolicyFetcher(policy)
    robots_cache: dict[str, tuple[str, str, bool]] = {}
    entries: list[dict[str, Any]] = []

    for index, wanted in enumerate(requests):
        url = str(wanted["url"])
        origin = canonical_origin(normalize_http_url(url))
        path = urlparse(url).path or "/"
        if origin not in robots_cache:
            robots_url = f"{origin}/robots.txt"
            pacer.wait()
            try:
                robots = fetcher.fetch(robots_url)
                robots_digest = hashlib.sha256(robots.body).hexdigest()
                if robots.status_code >= 400:
                    # No published robots means no stated refusal; the policy still governs access.
                    robots_cache[origin] = (robots_url, robots_digest, robots.status_code == 404)
                else:
                    robots_cache[origin] = (
                        robots_url,
                        robots_digest,
                        _robots_allows(robots.body, path),
                    )
                    disallowed, delay = _robots_rules(robots.body)
                    (cache / f"{robots_digest}.bin").write_bytes(robots.body)
                    entries.append(
                        {
                            "registryId": wanted["registryId"],
                            "url": robots_url,
                            "state": "captured",
                            "statusCode": robots.status_code,
                            "contentType": "text/plain",
                            "responseDigestSha256": robots_digest,
                            "observedAtUtc": robots.observed_at_utc.isoformat().replace(
                                "+00:00", "Z"
                            ),
                            "encodedSizeBytes": robots.encoded_size_bytes,
                            "decodedSizeBytes": len(robots.body),
                            "robotsUrl": robots_url,
                            "robotsDigestSha256": robots_digest,
                            "robotsAllowed": robots_cache[origin][2],
                            "fieldPaths": ["source.robots"],
                            "excerpt": (
                                f"robots.txt status {robots.status_code}, "
                                f"disallow_all_for_star={'true' if '/' in disallowed else 'false'}, "
                                f"crawl_delay_seconds={'None' if delay is None else delay}"
                            ),
                            "cachePath": str(cache / f"{robots_digest}.bin"),
                        }
                    )
            except (CaptureError, OSError) as error:
                robots_cache[origin] = (robots_url, "", False)
                entries.append(
                    {
                        "registryId": wanted["registryId"],
                        "url": url,
                        "state": "refused",
                        "code": "robots_unavailable",
                        "detail": str(error),
                    }
                )
                continue
        robots_url, robots_digest, robots_allow = robots_cache[origin]
        if not robots_allow and robots_digest:
            entries.append(
                {
                    "registryId": wanted["registryId"],
                    "url": url,
                    "state": "refused",
                    "code": "robots_disallow",
                    "detail": f"{robots_url} disallows {path}",
                }
            )
            continue

        pacer.wait()
        try:
            result = fetcher.fetch(normalize_http_url(url))
        except (CaptureError, OSError) as error:
            entries.append(
                {
                    "registryId": wanted["registryId"],
                    "url": url,
                    "state": "refused",
                    "code": "fetch_failed",
                    "detail": str(error),
                }
            )
            continue

        content_type = result.content_type
        refusal = None
        if result.status_code in stop_codes:
            refusal = f"status_{result.status_code}"
        elif result.status_code != 200:
            refusal = f"status_{result.status_code}"
        elif not any(item in content_type.lower() for item in allowed_types):
            refusal = f"content_type_not_allowed:{content_type}"
        elif normalize_http_url(result.final_url) != normalize_http_url(url):
            refusal = f"redirect_not_followed:{result.final_url}"
        if refusal is not None:
            entries.append(
                {
                    "registryId": wanted["registryId"],
                    "url": url,
                    "state": "refused",
                    "code": refusal,
                    "detail": f"HTTP {result.status_code}",
                }
            )
            continue

        digest = hashlib.sha256(result.body).hexdigest()
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"{digest}.bin").write_bytes(result.body)
        literals = [str(item) for item in wanted.get("mustContain") or []]
        subject_name = wanted.get("subjectName")
        text = _text_of(result.body, content_type)
        excerpt = excerpt_around(text, literals) if literals else text[: 2 * EXCERPT_RADIUS]
        extraction = None
        if excerpt is None and literals and "html" in content_type:
            # Visible text excludes script content, so a fact stated only in the page's own
            # structured data is read there — with its block and JSON pointer named.
            structured = extract_jsonld_fact(
                result.body,
                literals,
                subject_name=str(subject_name) if isinstance(subject_name, str) else None,
            )
            if structured is not None:
                excerpt = structured.pop("excerpt")
                extraction = structured
        if excerpt is None:
            entries.append(
                {
                    "registryId": wanted["registryId"],
                    "url": url,
                    "state": "refused",
                    "code": "fact_not_stated",
                    "detail": f"artifact does not contain {literals} in visible text or JSON-LD",
                }
            )
            continue
        entries.append(
            {
                "registryId": wanted["registryId"],
                "url": normalize_http_url(url),
                "state": "captured",
                "statusCode": result.status_code,
                "contentType": content_type,
                "responseDigestSha256": digest,
                "observedAtUtc": result.observed_at_utc.isoformat().replace("+00:00", "Z"),
                "encodedSizeBytes": result.encoded_size_bytes,
                "decodedSizeBytes": len(result.body),
                "robotsUrl": robots_url,
                "robotsDigestSha256": robots_digest or None,
                "robotsAllowed": robots_allow,
                "fieldPaths": list(wanted.get("fieldPaths") or []),
                "excerpt": excerpt,
                **({"extraction": extraction} if extraction is not None else {}),
                "cachePath": str(cache / f"{digest}.bin"),
            }
        )
    return {
        "schemaRevision": "studio-evidence-capture-v1",
        "sourceKey": SOURCE_KEY,
        "policyKey": policy["policy_key"],
        "policyRevision": policy["policy_revision"],
        "policyDigestSha256": POLICY_DIGEST,
        "capturedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entries": entries,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture official-site evidence through DCPF acquisition."
    )
    parser.add_argument("--requests", type=pathlib.Path, required=True)
    parser.add_argument(
        "--cache", type=pathlib.Path, default=pathlib.Path("C:/tmp/aggregator/site-cache")
    )
    parser.add_argument("--out", type=pathlib.Path, required=True)
    arguments = parser.parse_args(argv)

    wanted = json.loads(arguments.requests.read_text(encoding="utf-8"))
    document = capture(wanted, cache=arguments.cache)
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    captured = [entry for entry in document["entries"] if entry["state"] == "captured"]
    refused = [entry for entry in document["entries"] if entry["state"] != "captured"]
    print(json.dumps({"captured": len(captured), "refused": len(refused)}))
    for entry in refused:
        print(f"  refused {entry['url']}: {entry['code']} {entry['detail'][:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
