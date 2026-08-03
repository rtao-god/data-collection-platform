from __future__ import annotations

from collections.abc import Mapping, Sequence

from collection_application.campaign_documents import ParsedCampaignDocuments
from collection_contracts import ManualSeedRow, owner_error


def validate_campaign_references(
    *,
    requested_campaign_key: str,
    documents: ParsedCampaignDocuments,
    seed_rows: Mapping[str, tuple[ManualSeedRow, ...]],
    correlation_id: str,
) -> None:
    campaign = documents.campaign
    violations: list[dict[str, object]] = []
    if campaign.campaign_key != requested_campaign_key:
        violations.append(
            {
                "reference": "campaign_key",
                "expected": requested_campaign_key,
                "actual": campaign.campaign_key,
            }
        )

    kind_keys = {item.key for item in documents.entity_kinds.items}
    category_keys = {item.key for item in documents.taxonomy.items}
    binding_map = {item.key: item for item in documents.source_bindings.items}
    attribute_keys = {item.key for item in documents.attributes.items}

    _append_missing(violations, "campaign.entity_kinds", campaign.entity_kinds, kind_keys)
    _append_missing(
        violations,
        "campaign.target_categories",
        campaign.target_categories,
        category_keys,
    )
    _append_missing(
        violations,
        "campaign.enabled_source_bindings",
        campaign.enabled_source_bindings,
        set(binding_map),
    )
    for category in documents.taxonomy.items:
        _append_missing(
            violations,
            f"taxonomy.{category.key}.applicable_entity_kinds",
            category.applicable_entity_kinds,
            kind_keys,
        )

    for binding_key in campaign.enabled_source_bindings:
        binding = binding_map.get(binding_key)
        if binding is None:
            continue
        policy = documents.source_policies.get(binding.source_policy_key)
        if policy is None:
            violations.append(
                {
                    "reference": f"binding.{binding.key}.source_policy_key",
                    "value": binding.source_policy_key,
                    "reason": "missing",
                }
            )
            continue
        if policy.source_key != binding.source_key:
            violations.append(
                {
                    "reference": f"binding.{binding.key}.source_key",
                    "value": binding.source_key,
                    "policyValue": policy.source_key,
                    "reason": "source_policy_mismatch",
                }
            )
        if policy.legal_status not in {"approved", "reference_only"}:
            violations.append(
                {
                    "reference": f"binding.{binding.key}.source_policy_key",
                    "value": policy.policy_key,
                    "reason": f"legal_status_{policy.legal_status}",
                }
            )
        _append_missing(
            violations,
            f"policy.{policy.policy_key}.allowed_fields",
            policy.allowed_fields,
            attribute_keys,
        )

    for path, rows in seed_rows.items():
        for row in rows:
            if row.expected_entity_kind not in kind_keys:
                violations.append(
                    {
                        "reference": f"{path}:row:{row.row_number}:expected_entity_kind",
                        "value": row.expected_entity_kind,
                        "reason": "missing_entity_kind",
                    }
                )

    if violations:
        raise owner_error(
            error_type="collection/campaign-reference-invalid",
            owner="CampaignConfiguration",
            code="CAMPAIGN_REFERENCE_INVALID",
            message="Campaign documents contain unresolved or inconsistent references.",
            context={"campaignKey": requested_campaign_key, "violations": violations},
            required_action="Correct every named reference and validate the campaign again.",
            correlation_id=correlation_id,
        )


def _append_missing(
    violations: list[dict[str, object]],
    reference: str,
    values: Sequence[str],
    owner_values: set[str],
) -> None:
    for value in values:
        if value not in owner_values:
            violations.append({"reference": reference, "value": value, "reason": "missing"})
