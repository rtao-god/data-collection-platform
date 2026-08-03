from __future__ import annotations

import pytest
from pydantic import ValidationError

from collection_contracts import AttributeDefinition, SourcePolicy


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
