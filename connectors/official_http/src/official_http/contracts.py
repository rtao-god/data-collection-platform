from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from official_http.errors import OfficialHttpError
from official_http.urls import canonical_origin, normalize_http_url

Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
HeaderName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
RequestKind = Literal["robots", "sitemap", "page"]


class PageInterestRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: Token
    tokens: tuple[Annotated[str, StringConstraints(min_length=1, max_length=100)], ...]
    priority: Annotated[int, Field(ge=1, le=1_000)]

    @model_validator(mode="after")
    def validate_canonical(self) -> Self:
        normalized = tuple(sorted({token.casefold() for token in self.tokens}))
        if not normalized or normalized != self.tokens:
            raise ValueError("page-interest tokens must be unique, case-folded, and sorted")
        return self


class OfficialHttpRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    contract: Literal["official-http-request"] = "official-http-request"
    contract_revision: Literal["official-http-request-v1"] = Field(
        default="official-http-request-v1", alias="contractRevision"
    )
    request_id: Token = Field(alias="requestId")
    source_key: Token = Field(alias="sourceKey")
    source_policy_digest: Digest = Field(alias="sourcePolicyDigest")
    request_kind: RequestKind = Field(alias="requestKind")
    url: Annotated[str, StringConstraints(min_length=1, max_length=4_096)]
    allowed_origin: Annotated[str, StringConstraints(min_length=1, max_length=512)] = Field(
        alias="allowedOrigin"
    )
    user_agent: Annotated[str, StringConstraints(min_length=1, max_length=512)] = Field(
        alias="userAgent"
    )
    timeout_seconds: Annotated[int, Field(ge=1, le=180)] = Field(alias="timeoutSeconds")
    maximum_encoded_bytes: Annotated[int, Field(ge=1_024, le=64 * 1024 * 1024)] = Field(
        alias="maximumEncodedBytes"
    )
    maximum_decoded_bytes: Annotated[int, Field(ge=1_024, le=128 * 1024 * 1024)] = Field(
        alias="maximumDecodedBytes"
    )
    depth: Annotated[int, Field(ge=0, le=10)] = 0
    maximum_discovered_urls: Annotated[int, Field(ge=0, le=2_000)] = Field(
        default=100, alias="maximumDiscoveredUrls"
    )
    tracking_query_parameters: tuple[Token, ...] = Field(
        default=(), alias="trackingQueryParameters"
    )
    page_interests: tuple[PageInterestRule, ...] = Field(default=(), alias="pageInterests")
    robots_allowed: bool | None = Field(default=None, alias="robotsAllowed")
    robots_artifact_id: UUID | None = Field(default=None, alias="robotsArtifactId")
    robots_decision_digest: Digest | None = Field(default=None, alias="robotsDecisionDigest")
    if_none_match: Annotated[str, StringConstraints(min_length=1, max_length=1_024)] | None = Field(
        default=None, alias="ifNoneMatch"
    )
    if_modified_since: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = (
        Field(default=None, alias="ifModifiedSince")
    )
    prior_artifact_id: UUID | None = Field(default=None, alias="priorArtifactId")
    prior_content_digest: Digest | None = Field(default=None, alias="priorContentDigest")

    @model_validator(mode="after")
    def validate_owner_contract(self) -> Self:
        tracking = tuple(sorted({item.casefold() for item in self.tracking_query_parameters}))
        if tracking != self.tracking_query_parameters:
            raise ValueError("tracking query parameters must be unique, case-folded, and sorted")
        normalized_url = normalize_http_url(
            self.url, tracking_parameters=self.tracking_query_parameters
        )
        if normalized_url != self.url:
            raise ValueError(f"url must be canonical; expected {normalized_url}")
        if canonical_origin(self.url) != self.allowed_origin:
            raise ValueError("allowedOrigin must equal the canonical request origin")
        if self.maximum_decoded_bytes < self.maximum_encoded_bytes:
            raise ValueError("decoded byte limit cannot be smaller than encoded byte limit")
        if "\r" in self.user_agent or "\n" in self.user_agent:
            raise ValueError("user agent contains a forbidden line break")
        for value in (self.if_none_match, self.if_modified_since):
            if value is not None and ("\r" in value or "\n" in value):
                raise ValueError("conditional request header contains a forbidden line break")
        prior_values = (self.prior_artifact_id, self.prior_content_digest)
        if (prior_values[0] is None) != (prior_values[1] is None):
            raise ValueError("prior artifact identity and digest must be supplied together")
        if (self.if_none_match is not None or self.if_modified_since is not None) and (
            self.prior_artifact_id is None
        ):
            raise ValueError("conditional request headers require an exact prior artifact")
        if self.request_kind == "robots":
            if any(
                value is not None
                for value in (
                    self.robots_allowed,
                    self.robots_artifact_id,
                    self.robots_decision_digest,
                )
            ):
                raise ValueError("robots work must not claim a prior robots decision")
            if self.page_interests:
                raise ValueError("robots work must not contain page-interest rules")
        elif (
            self.robots_allowed is not True
            or self.robots_artifact_id is None
            or self.robots_decision_digest is None
        ):
            raise ValueError("non-robots work requires an exact allowed robots decision artifact")
        if self.request_kind == "page" and not self.page_interests:
            raise ValueError("page work requires at least one page-interest rule")
        interest_keys = tuple(rule.key for rule in self.page_interests)
        if len(set(interest_keys)) != len(interest_keys):
            raise ValueError("page-interest keys must be unique")
        return self

    def to_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", by_alias=True))

    @property
    def digest(self) -> str:
        return _digest(self.to_bytes())


class ResponseHeader(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: HeaderName
    value: Annotated[str, StringConstraints(max_length=4_096)]


class DiscoveredResource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    url: Annotated[str, StringConstraints(min_length=1, max_length=4_096)]
    resource_kind: Literal["sitemap", "page"] = Field(alias="resourceKind")
    interest_key: Token | None = Field(default=None, alias="interestKey")
    score: Annotated[int, Field(ge=0, le=1_000)]


class HttpAcquisitionManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    contract: Literal["official-http-acquisition"] = "official-http-acquisition"
    contract_revision: Literal["official-http-acquisition-v1"] = Field(
        default="official-http-acquisition-v1", alias="contractRevision"
    )
    request_id: Token = Field(alias="requestId")
    source_key: Token = Field(alias="sourceKey")
    source_policy_digest: Digest = Field(alias="sourcePolicyDigest")
    request_kind: RequestKind = Field(alias="requestKind")
    requested_url: str = Field(alias="requestedUrl")
    final_url: str = Field(alias="finalUrl")
    outcome: Literal["fetched", "empty", "unchanged", "redirect", "not_found"]
    status_code: Annotated[int, Field(ge=100, le=599)] = Field(alias="statusCode")
    response_headers: tuple[ResponseHeader, ...] = Field(alias="responseHeaders")
    remote_ip_address: str = Field(alias="remoteIpAddress")
    encoded_size_bytes: Annotated[int, Field(ge=0)] = Field(alias="encodedSizeBytes")
    decoded_size_bytes: Annotated[int, Field(ge=0)] = Field(alias="decodedSizeBytes")
    observed_at_utc: datetime = Field(alias="observedAtUtc")
    raw_artifact_digest: Digest | None = Field(default=None, alias="rawArtifactDigest")
    reused_artifact_id: UUID | None = Field(default=None, alias="reusedArtifactId")
    reused_content_digest: Digest | None = Field(default=None, alias="reusedContentDigest")
    redirect_location: str | None = Field(default=None, alias="redirectLocation")
    discovered_resources: tuple[DiscoveredResource, ...] = Field(
        default=(), alias="discoveredResources"
    )
    robots_allowed: bool | None = Field(default=None, alias="robotsAllowed")
    robots_artifact_id: UUID | None = Field(default=None, alias="robotsArtifactId")
    robots_decision_digest: Digest | None = Field(default=None, alias="robotsDecisionDigest")

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> Self:
        if self.observed_at_utc.tzinfo is None or self.observed_at_utc.utcoffset() != UTC.utcoffset(
            self.observed_at_utc
        ):
            raise ValueError("observedAtUtc must be UTC")
        header_names = tuple(item.name for item in self.response_headers)
        if header_names != tuple(sorted(set(header_names))):
            raise ValueError("response headers must be unique and sorted")
        resource_order = tuple((-item.score, item.url) for item in self.discovered_resources)
        if resource_order != tuple(sorted(resource_order)):
            raise ValueError("discovered resources must be sorted by score and URL")
        if self.outcome == "fetched":
            if not 200 <= self.status_code <= 299 or self.raw_artifact_digest is None:
                raise ValueError("fetched outcome requires a 2xx response and raw artifact digest")
            if any(
                value is not None
                for value in (
                    self.reused_artifact_id,
                    self.reused_content_digest,
                    self.redirect_location,
                )
            ):
                raise ValueError("fetched outcome contains incompatible artifact fields")
        elif self.outcome == "empty":
            if not 200 <= self.status_code <= 299 or self.decoded_size_bytes != 0:
                raise ValueError("empty outcome requires a bodyless 2xx response")
            if any(
                value is not None
                for value in (
                    self.raw_artifact_digest,
                    self.reused_artifact_id,
                    self.reused_content_digest,
                    self.redirect_location,
                )
            ):
                raise ValueError("empty outcome contains incompatible artifact fields")
        elif self.outcome == "unchanged":
            if self.status_code != 304:
                raise ValueError("unchanged outcome requires HTTP 304")
            if self.reused_artifact_id is None or self.reused_content_digest is None:
                raise ValueError("unchanged outcome requires the exact prior artifact")
            if self.raw_artifact_digest is not None or self.redirect_location is not None:
                raise ValueError("unchanged outcome contains incompatible fields")
        elif self.outcome == "redirect":
            if not 300 <= self.status_code <= 399 or self.status_code == 304:
                raise ValueError("redirect outcome requires a redirect response")
            if self.redirect_location is None:
                raise ValueError("redirect outcome requires a normalized location")
            if any(
                value is not None
                for value in (
                    self.raw_artifact_digest,
                    self.reused_artifact_id,
                    self.reused_content_digest,
                )
            ):
                raise ValueError("redirect outcome contains incompatible artifact fields")
        else:
            if self.status_code not in {404, 410}:
                raise ValueError("not_found outcome requires HTTP 404 or 410")
            if any(
                value is not None
                for value in (
                    self.raw_artifact_digest,
                    self.reused_artifact_id,
                    self.reused_content_digest,
                    self.redirect_location,
                )
            ):
                raise ValueError("not_found outcome contains incompatible artifact fields")
        return self

    def to_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json", by_alias=True))

    @property
    def digest(self) -> str:
        return _digest(self.to_bytes())


def decode_http_request(body: bytes, *, maximum_bytes: int = 1024 * 1024) -> OfficialHttpRequest:
    if len(body) > maximum_bytes:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_REQUEST_TOO_LARGE",
            message="The official HTTP request artifact exceeds the byte limit.",
        )
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_REQUEST_ENCODING_INVALID",
            message="The official HTTP request artifact is not valid UTF-8.",
        ) from exc
    try:
        json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_non_finite)
        return OfficialHttpRequest.model_validate_json(text, strict=True)
    except OfficialHttpError:
        raise
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
        raise OfficialHttpError(
            code="OFFICIAL_HTTP_REQUEST_CONTRACT_INVALID",
            message="The official HTTP request artifact violates its contract.",
            context={"detail": _bounded_detail(exc)},
        ) from exc


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OfficialHttpError(
                code="OFFICIAL_HTTP_REQUEST_DUPLICATE_KEY",
                message="The official HTTP request contains a duplicate JSON key.",
                context={"key": key},
            )
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise OfficialHttpError(
        code="OFFICIAL_HTTP_REQUEST_NON_FINITE_NUMBER",
        message="The official HTTP request contains a non-finite number.",
        context={"value": value},
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return f"sha256:{sha256(value).hexdigest()}"


def _bounded_detail(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:1_000]
