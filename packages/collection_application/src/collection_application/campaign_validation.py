from __future__ import annotations

from collections.abc import Mapping, Sequence

from collection_application.campaign_documents import ParsedCampaignDocuments
from collection_contracts import (
    ManualSeedRow,
    NormalizationProfile,
    SourcePolicy,
    owner_error,
)

# Acquisition is permission to fetch internally. Redistribution of the resulting
# database output is a strictly stronger, independently gated authorization and is
# never implied by these values.
_ACQUISITION_AUTHORIZED_STATUSES = frozenset({"approved", "reference_only", "research_only"})
_REDISTRIBUTION_AUTHORIZED_STATUSES = frozenset({"approved"})

_NORMALIZATION_ATTRIBUTE_TYPES: dict[str, frozenset[str]] = {
    "text": frozenset({"text", "localized_text"}),
    "boolean": frozenset({"boolean"}),
    "url": frozenset({"url"}),
    "email": frozenset({"email"}),
    "phone": frozenset({"phone"}),
    "structured_address": frozenset({"structured_address"}),
    "money": frozenset({"money"}),
    "string_set": frozenset({"set"}),
}


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
    attribute_types = {item.key: item.value_type for item in documents.attributes.items}
    attribute_keys = set(attribute_types)
    mapping = documents.catalog_mapping
    profiles = _validate_normalization_profiles(documents, attribute_types, violations)

    if campaign.readiness.state == "ready" and documents.geography is None:
        violations.append(
            {
                "reference": "campaign.geography_revision",
                "value": campaign.geography_revision,
                "reason": "geography_document_missing",
            }
        )
    if (
        documents.geography is not None
        and documents.geography.geography_revision != campaign.geography_revision
    ):
        violations.append(
            {
                "reference": "geography.geography_revision",
                "expected": campaign.geography_revision,
                "actual": documents.geography.geography_revision,
            }
        )

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

    for binding in documents.source_bindings.items:
        # A Wikidata classification is operator-authored configuration, so the campaign
        # it targets must already be a declared target category: the mapping may not
        # introduce a meaning the campaign never authorized.
        declaration = binding.wikidata_classification
        if declaration is not None and declaration.campaign_category not in category_keys:
            violations.append(
                {
                    "reference": f"binding.{binding.key}.wikidata_classification",
                    "value": declaration.campaign_category,
                    "reason": "campaign_category_is_not_a_target_category",
                }
            )
        query = binding.wikidata_query
        if query is not None:
            policy_preview = documents.source_policies.get(binding.source_policy_key)
            if (
                policy_preview is not None
                and policy_preview.access.kind == "network"
                and "www.wikidata.org" not in policy_preview.access.allowed_host_patterns
            ):
                violations.append(
                    {
                        "reference": f"binding.{binding.key}.wikidata_query",
                        "value": binding.source_policy_key,
                        "reason": "source_policy_does_not_allow_the_entity_endpoint",
                    }
                )

    for binding_key in campaign.enabled_source_bindings:
        enabled_binding = binding_map.get(binding_key)
        if enabled_binding is None:
            continue
        policy = documents.source_policies.get(enabled_binding.source_policy_key)
        if policy is None:
            violations.append(
                {
                    "reference": f"binding.{enabled_binding.key}.source_policy_key",
                    "value": enabled_binding.source_policy_key,
                    "reason": "missing",
                }
            )
            continue
        if policy.source_key != enabled_binding.source_key:
            violations.append(
                {
                    "reference": f"binding.{enabled_binding.key}.source_key",
                    "value": enabled_binding.source_key,
                    "policyValue": policy.source_key,
                    "reason": "source_policy_mismatch",
                }
            )
        # Acquisition and redistribution are separate authorizations. `research_only`
        # permits internal collection but never redistribution; redistribution is
        # gated independently at export, not here.
        if policy.legal_status not in _ACQUISITION_AUTHORIZED_STATUSES:
            violations.append(
                {
                    "reference": f"binding.{enabled_binding.key}.source_policy_key",
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
        _validate_binding_profile_policy(
            binding_key=enabled_binding.key,
            profile_key=enabled_binding.normalization_profile,
            profiles=profiles,
            policy=policy,
            violations=violations,
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
            # A declared seed classification is source evidence, so it must name a
            # campaign category that this exact bundle both defines and maps. An
            # unmapped category stays a bundle-level violation instead of being dropped.
            if row.category not in category_keys:
                violations.append(
                    {
                        "reference": f"{path}:row:{row.row_number}:category",
                        "value": row.category,
                        "reason": "missing_taxonomy_category",
                    }
                )
            elif row.category not in mapping.pairs:
                violations.append(
                    {
                        "reference": f"{path}:row:{row.row_number}:category",
                        "value": row.category,
                        "reason": "category_has_no_catalog_target",
                    }
                )

    _validate_catalog_mapping(documents, set(category_keys), violations)

    _validate_seed_routing(documents, attribute_keys, violations)

    if documents.quality_profile is not None:
        try:
            documents.quality_profile.require_campaign_binding(campaign)
        except ValueError as exc:
            violations.append(
                {
                    "reference": "quality_profile",
                    "value": documents.quality_profile.profile_key,
                    "reason": str(exc),
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


def _validate_catalog_mapping(
    documents: ParsedCampaignDocuments,
    category_keys: set[str],
    violations: list[dict[str, object]],
) -> None:
    """Prove the declared classification map is complete and unambiguous for this bundle.

    The map is authored against one exact Catalog configuration revision. Every campaign
    category must therefore be either mapped to a distinct Catalog category or explicitly
    listed as unmapped with a reason: a category that is silently absent would let an
    observed classification disappear between export and proposal.
    """

    mapping = documents.catalog_mapping
    declared_target_categories = set(documents.campaign.target_categories)
    unmapped = mapping.unmapped_categories
    for item in mapping.categories:
        if item.campaign_category not in category_keys:
            violations.append(
                {
                    "reference": f"catalog_mapping.categories.{item.campaign_category}",
                    "value": item.catalog_category,
                    "reason": "missing_taxonomy_category",
                }
            )
        if item.campaign_category not in declared_target_categories:
            violations.append(
                {
                    "reference": f"catalog_mapping.categories.{item.campaign_category}",
                    "value": item.catalog_category,
                    "reason": "campaign_target_category_not_declared",
                }
            )
    for campaign_category in sorted(unmapped):
        if campaign_category not in category_keys:
            violations.append(
                {
                    "reference": f"catalog_mapping.unmapped.{campaign_category}",
                    "value": campaign_category,
                    "reason": "missing_taxonomy_category",
                }
            )
    for campaign_category in sorted(category_keys - set(mapping.pairs) - unmapped):
        violations.append(
            {
                "reference": f"catalog_mapping.categories.{campaign_category}",
                "value": campaign_category,
                "reason": "taxonomy_category_unclassified",
            }
        )
    missing_targets = sorted(declared_target_categories - set(mapping.pairs) - unmapped)
    if missing_targets:
        violations.append(
            {
                "reference": "catalog_mapping.categories",
                "value": ",".join(missing_targets),
                "reason": "campaign_target_category_unclassified",
            }
        )


def _validate_profile_attribute_types(
    profile_key: str,
    profile: NormalizationProfile,
    attribute_types: Mapping[str, str],
    violations: list[dict[str, object]],
) -> None:
    for rule in profile.field_rules:
        attribute_type = attribute_types.get(rule.target_field)
        compatible_types = _NORMALIZATION_ATTRIBUTE_TYPES.get(rule.value_kind)
        if attribute_type is not None and (
            compatible_types is None or attribute_type not in compatible_types
        ):
            violations.append(
                {
                    "reference": (
                        f"normalization_profile.{profile_key}.field_rules.{rule.source_field}"
                    ),
                    "value": rule.target_field,
                    "normalizationKind": rule.value_kind,
                    "attributeValueType": attribute_type,
                    "compatibleAttributeTypes": sorted(compatible_types or ()),
                    "reason": "normalization_type_mismatch",
                }
            )
    for boolean_rule in profile.boolean_pattern_rules:
        attribute_type = attribute_types.get(boolean_rule.target_field)
        if attribute_type is not None and attribute_type != "boolean":
            violations.append(
                {
                    "reference": (
                        f"normalization_profile.{profile_key}.boolean_pattern_rules."
                        f"{boolean_rule.target_field}"
                    ),
                    "value": boolean_rule.target_field,
                    "normalizationKind": "boolean",
                    "attributeValueType": attribute_type,
                    "compatibleAttributeTypes": ["boolean"],
                    "reason": "normalization_type_mismatch",
                }
            )


def _validate_binding_profile_policy(
    *,
    binding_key: str,
    profile_key: str | None,
    profiles: Mapping[str, NormalizationProfile],
    policy: SourcePolicy,
    violations: list[dict[str, object]],
) -> None:
    if profile_key is None:
        return
    profile = profiles.get(profile_key)
    if profile is None:
        return
    target_fields = {
        *(rule.target_field for rule in profile.field_rules),
        *(rule.target_field for rule in profile.boolean_pattern_rules),
    }
    _append_missing(
        violations,
        f"binding.{binding_key}.normalization_profile.allowed_fields",
        tuple(sorted(target_fields)),
        set(policy.allowed_fields),
    )


def _validate_normalization_profiles(
    documents: ParsedCampaignDocuments,
    attribute_types: Mapping[str, str],
    violations: list[dict[str, object]],
) -> dict[str, NormalizationProfile]:
    attribute_keys = set(attribute_types)
    definitions = (
        documents.normalization_profiles.items
        if documents.normalization_profiles is not None
        else ()
    )
    profiles = {item.key: item.profile for item in definitions}
    for binding in documents.source_bindings.items:
        if (
            binding.normalization_profile is not None
            and binding.normalization_profile not in profiles
        ):
            violations.append(
                {
                    "reference": f"binding.{binding.key}.normalization_profile",
                    "value": binding.normalization_profile,
                    "reason": "missing",
                }
            )
    for definition in definitions:
        profile = definition.profile
        target_fields = {
            *(rule.target_field for rule in profile.field_rules),
            *(rule.target_field for rule in profile.boolean_pattern_rules),
        }
        _append_missing(
            violations,
            f"normalization_profile.{definition.key}.target_fields",
            tuple(sorted(target_fields)),
            attribute_keys,
        )
        _validate_profile_attribute_types(
            definition.key,
            profile,
            attribute_types,
            violations,
        )
        prohibited_targets = target_fields.intersection(profile.prohibited_fields)
        if prohibited_targets:
            violations.append(
                {
                    "reference": f"normalization_profile.{definition.key}.prohibited_fields",
                    "value": sorted(prohibited_targets),
                    "reason": "normalization_target_is_prohibited",
                }
            )
    return profiles


def _append_missing(
    violations: list[dict[str, object]],
    reference: str,
    values: Sequence[str],
    owner_values: set[str],
) -> None:
    for value in values:
        if value not in owner_values:
            violations.append({"reference": reference, "value": value, "reason": "missing"})


def _validate_seed_routing(
    documents: ParsedCampaignDocuments,
    attribute_keys: set[str],
    violations: list[dict[str, object]],
) -> None:
    routing = documents.source_bindings.seed_routing
    if routing.state != "configured":
        return

    enabled = set(documents.campaign.enabled_source_bindings)
    bindings = {binding.key: binding for binding in documents.source_bindings.items}
    for route in routing.routes:
        source = bindings.get(route.source_binding)
        source_policy = (
            documents.source_policies.get(source.source_policy_key) if source is not None else None
        )
        if (
            source is None
            or source.key not in enabled
            or source.capability != "manual_import"
            or source_policy is None
            or route.source_field not in source_policy.allowed_fields
        ):
            violations.append(
                {
                    "reference": f"seed_route.{route.key}.source_binding",
                    "value": route.source_binding,
                    "reason": "source_binding_policy_does_not_allow_route_field",
                }
            )
        if route.source_field not in attribute_keys:
            violations.append(
                {
                    "reference": f"seed_route.{route.key}.source_field",
                    "value": route.source_field,
                    "reason": "missing_attribute",
                }
            )
        target = bindings.get(route.target_binding)
        if target is None or target.key not in enabled:
            violations.append(
                {
                    "reference": f"seed_route.{route.key}.target_binding",
                    "value": route.target_binding,
                    "reason": "target_binding_not_enabled",
                }
            )
            continue
        policy = documents.source_policies.get(target.source_policy_key)
        if (
            target.capability != "http_fetch"
            or policy is None
            or policy.source_type != "official_website"
            or policy.access.kind != "network"
            or policy.access.user_agent.state != "configured"
            or policy.legal_status != "approved"
        ):
            violations.append(
                {
                    "reference": f"seed_route.{route.key}.target_binding",
                    "value": route.target_binding,
                    "reason": "target_is_not_an_approved_official_website_binding_with_user_agent",
                }
            )
