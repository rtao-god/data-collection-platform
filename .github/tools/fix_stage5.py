from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected Stage 5 source fragment is missing")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> int:
    replace_once(
        "connectors/official_http/pyproject.toml",
        '  "source-connector-sdk",\n  "pydantic==2.13.4",\n',
        '  "source-connector-sdk",\n'
        '  "defusedxml==0.7.1",\n'
        '  "pydantic==2.13.4",\n',
    )

    replace_once(
        "connectors/official_http/src/official_http/planning.py",
        "from urllib.robotparser import RobotFileParser\n"
        "from xml.etree import ElementTree\n",
        "from typing import Literal\n"
        "from urllib.robotparser import RobotFileParser\n\n"
        "from defusedxml.ElementTree import (  # type: ignore[import-untyped]\n"
        "    ParseError,\n"
        "    fromstring,\n"
        ")\n",
    )
    replace_once(
        "connectors/official_http/src/official_http/planning.py",
        "        root = ElementTree.fromstring(body)\n"
        "    except ElementTree.ParseError as exc:\n",
        "        root = fromstring(body)\n"
        "    except ParseError as exc:\n",
    )
    replace_once(
        "connectors/official_http/src/official_http/planning.py",
        "    kind: str,\n",
        '    kind: Literal["sitemap", "page"],\n',
    )
    replace_once(
        "connectors/official_http/src/official_http/planning.py",
        "            resourceKind=kind,\n"
        "            interestKey=interest_key,\n",
        "            resource_kind=kind,\n"
        "            interest_key=interest_key,\n",
    )

    replace_once(
        "connectors/official_http/src/official_http/urls.py",
        "from posixpath import normpath\n"
        "from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit\n",
        "from posixpath import normpath\n"
        "from typing import cast\n"
        "from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit\n",
    )
    replace_once(
        "connectors/official_http/src/official_http/urls.py",
        "def resolve_public_addresses(url: str, *, resolver: Resolver = socket.getaddrinfo) "
        "-> tuple[str, ...]:\n"
        "    split = urlsplit(normalize_http_url(url))\n"
        "    assert split.hostname is not None\n"
        "    port = split.port or (80 if split.scheme == \"http\" else 443)\n"
        "    try:\n"
        "        answers = resolver(split.hostname, port, type=socket.SOCK_STREAM)\n",
        "def resolve_public_addresses(\n"
        "    url: str,\n"
        "    *,\n"
        "    resolver: Resolver | None = None,\n"
        ") -> tuple[str, ...]:\n"
        "    split = urlsplit(normalize_http_url(url))\n"
        "    hostname = split.hostname\n"
        "    if hostname is None:\n"
        "        raise _url_error(\n"
        "            \"OFFICIAL_HTTP_URL_HOST_MISSING\",\n"
        "            \"The HTTP URL requires a host.\",\n"
        "        )\n"
        "    port = split.port or (80 if split.scheme == \"http\" else 443)\n"
        "    active_resolver = resolver or cast(Resolver, socket.getaddrinfo)\n"
        "    try:\n"
        "        answers = active_resolver(hostname, port, type=socket.SOCK_STREAM)\n",
    )
    urls = Path("connectors/official_http/src/official_http/urls.py")
    text = urls.read_text(encoding="utf-8")
    text = text.replace('context={"host": split.hostname}', 'context={"host": hostname}')
    urls.write_text(text, encoding="utf-8")

    replace_once(
        "connectors/official_http/src/official_http/fetch.py",
        "from typing import Literal\n",
        "from typing import Literal, cast\n",
    )
    replace_once(
        "connectors/official_http/src/official_http/fetch.py",
        "def _required_string(document: object, key: str) -> str:\n"
        "    if not isinstance(document, dict) or not isinstance(document.get(key), str):\n"
        "        raise OfficialHttpError(\n"
        "            code=\"OFFICIAL_HTTP_CHILD_RESULT_INVALID\",\n"
        "            message=\"The one-shot Scrapy result is missing a required string.\",\n"
        "            kind=\"transient\",\n"
        "            context={\"field\": key},\n"
        "        )\n"
        "    return document[key]\n",
        "def _required_string(document: object, key: str) -> str:\n"
        "    if not isinstance(document, dict):\n"
        "        raise OfficialHttpError(\n"
        "            code=\"OFFICIAL_HTTP_CHILD_RESULT_INVALID\",\n"
        "            message=\"The one-shot Scrapy result is invalid.\",\n"
        "            kind=\"transient\",\n"
        "        )\n"
        "    value = document.get(key)\n"
        "    if not isinstance(value, str):\n"
        "        raise OfficialHttpError(\n"
        "            code=\"OFFICIAL_HTTP_CHILD_RESULT_INVALID\",\n"
        "            message=\"The one-shot Scrapy result is missing a required string.\",\n"
        "            kind=\"transient\",\n"
        "            context={\"field\": key},\n"
        "        )\n"
        "    return value\n",
    )
    replace_once(
        "connectors/official_http/src/official_http/fetch.py",
        "def _failure_kind(value: object) -> "
        'Literal["transient", "permanent", "policy_blocked", "contract_invalid"]:\n'
        "    if value in {\"transient\", \"permanent\", \"policy_blocked\", "
        "\"contract_invalid\"}:\n"
        "        return value  # type: ignore[return-value]\n"
        "    return \"transient\"\n",
        "FailureKind = Literal[\n"
        "    \"transient\",\n"
        "    \"permanent\",\n"
        "    \"policy_blocked\",\n"
        "    \"contract_invalid\",\n"
        "]\n\n\n"
        "def _failure_kind(value: object) -> FailureKind:\n"
        "    if value in {\"transient\", \"permanent\", \"policy_blocked\", "
        "\"contract_invalid\"}:\n"
        "        return cast(FailureKind, value)\n"
        "    return \"transient\"\n",
    )

    replace_once(
        "connectors/official_http/src/official_http/scrapy_child.py",
        "    class OneShotSpider(scrapy.Spider):\n",
        "    class OneShotSpider(scrapy.Spider):  # type: ignore[misc]\n",
    )
    replace_once(
        "connectors/official_http/src/official_http/scrapy_child.py",
        '                "Accept": "text/html,application/xhtml+xml,application/xml,'
        'text/xml,application/json,text/plain,*/*;q=0.1",\n',
        '                "Accept": (\n'
        '                    "text/html,application/xhtml+xml,application/xml,text/xml,"\n'
        '                    "application/json,text/plain,*/*;q=0.1"\n'
        '                ),\n',
    )

    replace_once(
        "apps/http_worker/src/http_worker/gateway.py",
        '    return "Correct the official HTTP request or connector contract before '
        'scheduling replacement work."\n',
        '    return (\n'
        '        "Correct the official HTTP request or connector contract before scheduling "\n'
        '        "replacement work."\n'
        '    )\n',
    )

    replace_once(
        "apps/http_worker/src/http_worker/worker.py",
        "from typing import Protocol\n",
        "from typing import Literal, Protocol\n",
    )
    replace_once(
        "apps/http_worker/src/http_worker/worker.py",
        "from official_http import (\n"
        "    HttpAcquisitionManifest,\n",
        "from official_http import (\n"
        "    DiscoveredResource,\n"
        "    HttpAcquisitionManifest,\n",
    )
    replace_once(
        "apps/http_worker/src/http_worker/worker.py",
        "    discovered = ()\n"
        "    robots_allowed = None\n",
        "    discovered: tuple[DiscoveredResource, ...] = ()\n"
        "    robots_allowed: bool | None = None\n",
    )
    replace_once(
        "apps/http_worker/src/http_worker/worker.py",
        "    raw_body: bytes | None = None\n",
        "    raw_body: bytes | None = None\n"
        "    outcome: Literal[\"fetched\", \"empty\", \"unchanged\", \"redirect\", "
        "\"not_found\"]\n",
    )
    replace_once(
        "apps/http_worker/src/http_worker/worker.py",
        "            discovered = tuple(\n"
        "                {\n"
        "                    \"url\": url,\n"
        "                    \"resourceKind\": \"sitemap\",\n"
        "                    \"interestKey\": None,\n"
        "                    \"score\": 1_000,\n"
        "                }\n"
        "                for url in evaluation.sitemap_urls\n"
        "            )\n",
        "            discovered = tuple(\n"
        "                DiscoveredResource(\n"
        "                    url=url,\n"
        "                    resource_kind=\"sitemap\",\n"
        "                    interest_key=None,\n"
        "                    score=1_000,\n"
        "                )\n"
        "                for url in evaluation.sitemap_urls\n"
        "            )\n",
    )
    replace_once(
        "apps/http_worker/src/http_worker/worker.py",
        "    manifest = HttpAcquisitionManifest(\n"
        "        requestId=request.request_id,\n"
        "        sourceKey=request.source_key,\n"
        "        sourcePolicyDigest=request.source_policy_digest,\n"
        "        requestKind=request.request_kind,\n"
        "        requestedUrl=request.url,\n"
        "        finalUrl=result.final_url,\n"
        "        outcome=outcome,\n"
        "        statusCode=status,\n"
        "        responseHeaders=result.headers,\n"
        "        remoteIpAddress=result.remote_ip_address,\n"
        "        encodedSizeBytes=result.encoded_size_bytes,\n"
        "        decodedSizeBytes=result.decoded_size_bytes,\n"
        "        observedAtUtc=result.observed_at_utc,\n"
        "        rawArtifactDigest=raw_digest,\n"
        "        reusedArtifactId=reused_id,\n"
        "        reusedContentDigest=reused_digest,\n"
        "        redirectLocation=redirect_location,\n"
        "        discoveredResources=discovered,\n"
        "        robotsAllowed=robots_allowed,\n"
        "    )\n",
        "    manifest = HttpAcquisitionManifest(\n"
        "        request_id=request.request_id,\n"
        "        source_key=request.source_key,\n"
        "        source_policy_digest=request.source_policy_digest,\n"
        "        request_kind=request.request_kind,\n"
        "        requested_url=request.url,\n"
        "        final_url=result.final_url,\n"
        "        outcome=outcome,\n"
        "        status_code=status,\n"
        "        response_headers=result.headers,\n"
        "        remote_ip_address=result.remote_ip_address,\n"
        "        encoded_size_bytes=result.encoded_size_bytes,\n"
        "        decoded_size_bytes=result.decoded_size_bytes,\n"
        "        observed_at_utc=result.observed_at_utc,\n"
        "        raw_artifact_digest=raw_digest,\n"
        "        reused_artifact_id=reused_id,\n"
        "        reused_content_digest=reused_digest,\n"
        "        redirect_location=redirect_location,\n"
        "        discovered_resources=discovered,\n"
        "        robots_allowed=robots_allowed,\n"
        "        robots_artifact_id=request.robots_artifact_id,\n"
        "        robots_decision_digest=request.robots_decision_digest,\n"
        "    )\n",
    )

    replace_once(
        "tools/architecture_checks/check_dependencies.py",
        'allowed_external_imports=frozenset({"pydantic", "scrapy"}),',
        'allowed_external_imports=frozenset({"defusedxml", "pydantic", "scrapy"}),',
    )
    replace_once(
        "docs/architecture/dependency-rules.md",
        "| `official_http` | `connectors/official_http` | `source_connector_sdk` | "
        "`pydantic`, `scrapy` |",
        "| `official_http` | `connectors/official_http` | `source_connector_sdk` | "
        "`defusedxml`, `pydantic`, `scrapy` |",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
