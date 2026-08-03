from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RawCampaignBundle:
    campaign_key: str
    files: Mapping[str, bytes]


class CampaignBundleSource(Protocol):
    def read(self, campaign_key: str, correlation_id: str) -> RawCampaignBundle: ...
