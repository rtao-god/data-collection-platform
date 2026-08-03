from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from collection_application.campaign_documents import (
    ATTRIBUTES_DOCUMENT,
    CAMPAIGN_DOCUMENT,
    ENTITY_KINDS_DOCUMENT,
    SOURCE_BINDINGS_DOCUMENT,
    TAXONOMY_DOCUMENT,
    ParsedCampaignDocuments,
)
from collection_contracts import ComponentDigest, ManualSeedRow


def canonical_documents(
    documents: ParsedCampaignDocuments,
    seed_rows: Mapping[str, tuple[ManualSeedRow, ...]],
) -> dict[str, object]:
    result: dict[str, object] = {
        CAMPAIGN_DOCUMENT: documents.campaign.model_dump(mode="json"),
        ENTITY_KINDS_DOCUMENT: documents.entity_kinds.model_dump(mode="json"),
        TAXONOMY_DOCUMENT: documents.taxonomy.model_dump(mode="json"),
        ATTRIBUTES_DOCUMENT: documents.attributes.model_dump(mode="json"),
        SOURCE_BINDINGS_DOCUMENT: documents.source_bindings.model_dump(mode="json"),
    }
    for policy in documents.source_policies.values():
        result[f"source_policies/{policy.policy_key}.yaml"] = policy.model_dump(mode="json")
    for path, rows in seed_rows.items():
        result[path] = [row.model_dump(mode="json") for row in rows]
    return result


def component_digests(documents: Mapping[str, object]) -> tuple[ComponentDigest, ...]:
    return tuple(
        ComponentDigest(path=path, digest=sha256_json(value))
        for path, value in sorted(documents.items())
    )


def bundle_digest(
    campaign_key: str,
    components: tuple[ComponentDigest, ...],
    documents: Mapping[str, object],
) -> str:
    payload = {
        "campaignKey": campaign_key,
        "components": [
            {
                "path": component.path,
                "digest": component.digest,
                "content": documents[component.path],
            }
            for component in components
        ],
    }
    return sha256_json(payload)


def sha256_json(value: object) -> str:
    canonical = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
