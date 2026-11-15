from __future__ import annotations

import pytest
from pydantic import ValidationError

from collection_contracts import (
    AttributeDefinition,
    NetworkAccess,
    NormalizationProfilesDocument,
    SourceBindingsDocument,
    SourcePolicy,
)
from collection_contracts.campaign_config import SeedRoutingConfigured


def test_enum_attribute_requires_explicit_options() -> None:
    with pytest.raises(ValidationError, match="enum attribute requires defined options"):
        AttributeDefinition.model_validate(
            {
                "key": "verification_level",
                "display_name": "Verification level",
                "value_type": "enum",
                "cardinality": "one",
                "unit": {"state": "not_applicable"},
                "options": {"state": "not_applicable"},
                "normalization_rule": "verification-v1",
                "missing_allowed": True,
                "evidence_required": True,
            }
        )


def test_normalization_profiles_document_rejects_duplicate_keys() -> None:
    profile = {
        "normalizer_revision": "website-normalizer-v1",
        "field_rules": [
            {
                "source_field": "website",
                "target_field": "website",
                "value_kind": "url",
            }
        ],
    }
    with pytest.raises(ValidationError, match="duplicate normalization profile keys"):
        NormalizationProfilesDocument.model_validate(
            {
                "schema_revision": "normalization-profiles-v1",
                "items": [
                    {"key": "website_profile", "profile": profile},
                    {"key": "website_profile", "profile": profile},
                ],
            }
        )


def test_source_policy_rejects_allowed_and_prohibited_field_overlap() -> None:
    payload = {
        "schema_revision": "source-policy-v1",
        "policy_key": "manual_policy",
        "policy_revision": "manual-policy-v1",
        "source_key": "manual_seed",
        "source_type": "manual_import",
        "legal_status": "approved",
        "terms_review": {"state": "not_applicable", "reason": "Local operator input."},
        "access": {
            "kind": "manual",
            "accepted_formats": ["csv"],
            "accepted_encodings": ["utf-8"],
            "max_file_bytes": 1000,
            "partial_mode_allowed": False,
        },
        "storage_class": "structured-v1",
        "retention_days": 30,
        "allowed_fields": ["website"],
        "prohibited_fields": ["website"],
        "attribution_required": False,
        "browser": "forbidden",
        "screenshots": "forbidden",
        "retry_budget": 0,
        "stop_on_status_codes": [],
        "reviewer": "owner",
        "approver": "owner",
    }
    with pytest.raises(ValidationError, match="both allowed and prohibited"):
        SourcePolicy.model_validate(payload)


def test_legacy_source_bindings_snapshot_reads_without_enabling_routes() -> None:
    document = SourceBindingsDocument.model_validate(
        {
            "schema_revision": "source-bindings-v1",
            "items": [
                {
                    "key": "manual_seed_import",
                    "source_key": "manual_seed",
                    "connector_key": "manual_seed",
                    "source_policy_key": "manual_policy",
                    "capability": "manual_import",
                    "seed_provider": {
                        "kind": "file",
                        "path": "discovery/manual_seeds.csv",
                        "format": "csv",
                    },
                    "crawl_budget": {"state": "not_applicable"},
                    "schedule_enabled": False,
                    "extraction_profile": "manual-seed-v1",
                }
            ],
        }
    )
    assert document.seed_routing.state == "not_configured"
    assert document.items[0].normalization_profile is None


def test_legacy_network_policy_reads_with_explicitly_missing_user_agent() -> None:
    access = NetworkAccess.model_validate(
        {
            "kind": "network",
            "allowed_methods": ["GET"],
            "allowed_host_patterns": ["studio.example"],
            "max_requests_per_second": 1,
            "max_concurrency": 1,
            "connect_timeout_seconds": 5,
            "read_timeout_seconds": 10,
            "max_encoded_bytes": 10000,
            "max_decoded_bytes": 20000,
            "content_type_allowlist": ["text/html"],
            "redirect_limit": 3,
        }
    )
    assert access.user_agent.state == "not_configured"


def test_multiple_routes_for_one_source_require_explicit_fan_out() -> None:
    route = {
        "source_field": "website",
        "source_binding": "manual_seed_import",
        "route_kind": "official_website_page",
        "page_interests": [{"key": "contact", "tokens": ["contact"], "priority": 1}],
    }
    with pytest.raises(ValidationError, match="require explicit fan_out"):
        SeedRoutingConfigured.model_validate(
            {
                "state": "configured",
                "routes": [
                    {
                        **route,
                        "key": "first",
                        "target_binding": "website_a",
                        "selection": "exclusive",
                    },
                    {
                        **route,
                        "key": "second",
                        "target_binding": "website_b",
                        "selection": "exclusive",
                    },
                ],
            }
        )


def test_exclusive_routes_are_independent_by_binding_and_selection_is_scoped() -> None:
    route = {
        "source_field": "website",
        "route_kind": "official_website_page",
        "page_interests": [{"key": "contact", "tokens": ["contact"], "priority": 1}],
        "selection": "exclusive",
    }
    configured = SeedRoutingConfigured.model_validate(
        {
            "state": "configured",
            "routes": [
                {
                    **route,
                    "key": "first",
                    "source_binding": "manual_source_a",
                    "target_binding": "website_a",
                },
                {
                    **route,
                    "key": "second",
                    "source_binding": "manual_source_b",
                    "target_binding": "website_b",
                },
            ],
        }
    )

    assert [route.key for route in configured.routes_for_source("manual_source_a", "website")] == [
        "first"
    ]
    assert [route.key for route in configured.routes_for_source("manual_source_b", "website")] == [
        "second"
    ]
