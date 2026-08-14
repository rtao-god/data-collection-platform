from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from collection_application.campaign_documents import ParsedCampaignDocuments
from collection_application.ports import RawCampaignBundle
from collection_contracts import CampaignSnapshot, ManualSeedRow


@dataclass(frozen=True, slots=True)
class CompiledCampaignBundle:
    snapshot: CampaignSnapshot
    raw_bundle: RawCampaignBundle
    documents: ParsedCampaignDocuments
    seed_rows: Mapping[str, tuple[ManualSeedRow, ...]]
    canonical_documents: Mapping[str, object]
    canonical_content: bytes
