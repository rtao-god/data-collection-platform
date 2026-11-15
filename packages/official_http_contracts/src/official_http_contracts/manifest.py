from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
HeaderName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
RequestKind = Literal["robots", "sitemap", "page"]


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
    robots_target_url: str | None = Field(default=None, alias="robotsTargetUrl")

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> Self:
        if self.observed_at_utc.tzinfo is None or self.observed_at_utc.utcoffset() != UTC.utcoffset(
            self.observed_at_utc
        ):
            raise ValueError("observedAtUtc must be UTC")
        names = tuple(header.name for header in self.response_headers)
        if names != tuple(sorted(set(names))):
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
            if (
                self.status_code != 304
                or self.reused_artifact_id is None
                or self.reused_content_digest is None
            ):
                raise ValueError("unchanged outcome requires 304 and exact prior artifact")
            if self.raw_artifact_digest is not None or self.redirect_location is not None:
                raise ValueError("unchanged outcome contains incompatible artifact fields")
        elif self.outcome == "redirect":
            if (
                not 300 <= self.status_code <= 399
                or self.status_code == 304
                or self.redirect_location is None
            ):
                raise ValueError("redirect outcome requires redirect status and location")
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
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return f"sha256:{sha256(self.to_bytes()).hexdigest()}"
