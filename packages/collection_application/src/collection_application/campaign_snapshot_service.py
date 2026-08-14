from __future__ import annotations

from types import MappingProxyType

from collection_application.campaign_documents import parse_campaign_documents
from collection_application.campaign_validation import validate_campaign_references
from collection_application.canonicalization import (
    bundle_digest,
    canonical_bundle_document,
    canonical_documents,
    canonical_json_bytes,
    component_digests,
)
from collection_application.compiled_campaign import CompiledCampaignBundle
from collection_application.manual_seed import load_manual_seed_rows
from collection_application.ports import CampaignBundleSource
from collection_contracts import CampaignSnapshot, SnapshotBlocker


class CampaignSnapshotService:
    def __init__(self, source: CampaignBundleSource) -> None:
        self._source = source

    def create(self, campaign_key: str, correlation_id: str) -> CampaignSnapshot:
        return self.compile(campaign_key, correlation_id).snapshot

    def compile(self, campaign_key: str, correlation_id: str) -> CompiledCampaignBundle:
        raw_bundle = self._source.read(campaign_key, correlation_id)
        documents = parse_campaign_documents(raw_bundle, correlation_id)
        seed_rows = load_manual_seed_rows(
            raw_bundle,
            documents.source_bindings,
            documents.source_policies,
            correlation_id,
        )
        validate_campaign_references(
            requested_campaign_key=campaign_key,
            documents=documents,
            seed_rows=seed_rows,
            correlation_id=correlation_id,
        )

        canonical = canonical_documents(documents, seed_rows)
        components = component_digests(canonical)
        campaign = documents.campaign
        blockers = tuple(
            SnapshotBlocker(
                code=blocker.code,
                owner=blocker.owner,
                message=blocker.message,
                requiredAction=blocker.required_action,
            )
            for blocker in (
                campaign.readiness.blockers if campaign.readiness.state == "blocked" else ()
            )
        )
        snapshot = CampaignSnapshot(
            contract="collector-campaign-snapshot",
            contract_revision="campaign-snapshot-v1",
            campaign_key=campaign.campaign_key,
            bundle_digest=bundle_digest(campaign.campaign_key, components, canonical),
            components=components,
            readiness=campaign.readiness.state,
            blockers=blockers,
        )
        content = canonical_json_bytes(
            canonical_bundle_document(campaign.campaign_key, components, canonical)
        )
        return CompiledCampaignBundle(
            snapshot=snapshot,
            raw_bundle=raw_bundle,
            documents=documents,
            seed_rows=MappingProxyType(dict(seed_rows)),
            canonical_documents=MappingProxyType(dict(canonical)),
            canonical_content=content,
        )
