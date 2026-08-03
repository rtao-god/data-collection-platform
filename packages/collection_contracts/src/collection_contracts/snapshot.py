from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


class ComponentDigest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=240)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SnapshotBlocker(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    owner: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=300)
    required_action: str = Field(
        alias="requiredAction",
        serialization_alias="requiredAction",
        min_length=1,
        max_length=300,
    )


class CampaignSnapshot(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True
    )

    contract: Literal["collector-campaign-snapshot"]
    contract_revision: Literal["campaign-snapshot-v1"] = Field(
        alias="contractRevision", serialization_alias="contractRevision"
    )
    campaign_key: str = Field(
        alias="campaignKey", serialization_alias="campaignKey", pattern=r"^[a-z][a-z0-9_]*$"
    )
    bundle_digest: str = Field(
        alias="bundleDigest", serialization_alias="bundleDigest", pattern=r"^sha256:[0-9a-f]{64}$"
    )
    components: tuple[ComponentDigest, ...] = Field(min_length=1)
    readiness: Literal["ready", "blocked"]
    blockers: tuple[SnapshotBlocker, ...]

    def canonical_output(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True)
