from __future__ import annotations

import json
from collections.abc import Sequence
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
    robots_manifest_digest: Digest | None = Field(default=None, alias="robotsManifestDigest")
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
                    self.robots_manifest_digest,
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
        if self.request_kind != "page" and self.robots_manifest_digest is not None:
            raise ValueError("robots manifest digest is supported only for routed page work")
        interest_keys = tuple(rule.key for rule in self.page_interests)
        if len(set(interest_keys)) != len(interest_keys):
            raise ValueError("page-interest keys must be unique")
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
