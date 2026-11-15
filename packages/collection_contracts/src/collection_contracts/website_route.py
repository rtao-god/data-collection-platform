from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

RouteToken = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,79}$")]
Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
InterestToken = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class WebsitePageInterest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: RouteToken
    tokens: tuple[InterestToken, ...]
    priority: Annotated[int, Field(ge=1, le=1_000)]

    @model_validator(mode="after")
    def validate_tokens(self) -> Self:
        normalized = tuple(sorted({token.casefold() for token in self.tokens}))
        if not normalized or normalized != self.tokens:
            raise ValueError("website page-interest tokens must be unique, case-folded, and sorted")
        return self


class WebsiteRouteContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, strict=True)

    contract: Literal["collector-website-route-context"] = "collector-website-route-context"
    contract_revision: Literal["website-route-context-v1"] = Field(
        default="website-route-context-v1", alias="contractRevision"
    )
    request_id: Token = Field(alias="requestId")
    request_digest: Digest = Field(alias="requestDigest")
    campaign_bundle_digest: Digest = Field(alias="campaignBundleDigest")
    record_digest: Digest = Field(alias="recordDigest")
    route_key: RouteToken = Field(alias="routeKey")
    source_binding_key: RouteToken = Field(alias="sourceBindingKey")
    target_binding_key: RouteToken = Field(alias="targetBindingKey")
    source_key: Token = Field(alias="sourceKey")
    source_policy_digest: Digest = Field(alias="sourcePolicyDigest")
    source_url: AnyHttpUrl = Field(alias="sourceUrl")
    page_interests: tuple[WebsitePageInterest, ...] = Field(alias="pageInterests", min_length=1)
    retry_budget: Annotated[int, Field(ge=0, le=99)] = Field(default=0, alias="retryBudget")

    @model_validator(mode="after")
    def validate_interest_keys(self) -> Self:
        keys = tuple(item.key for item in self.page_interests)
        if len(keys) != len(set(keys)):
            raise ValueError("website page-interest keys must be unique")
        return self


def decode_website_route_context(
    body: bytes,
    *,
    maximum_bytes: int = 256 * 1024,
) -> WebsiteRouteContext:
    if not 1 <= len(body) <= maximum_bytes:
        raise ValueError("website route context is outside the supported size range")
    try:
        text = body.decode("utf-8")
        json.loads(text, object_pairs_hook=_unique_object)
        context = WebsiteRouteContext.model_validate_json(text, strict=True)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
    ) as exc:
        raise ValueError("website route context violates its owner contract") from exc
    return context


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate website route context key: {key}")
        result[key] = value
    return result
