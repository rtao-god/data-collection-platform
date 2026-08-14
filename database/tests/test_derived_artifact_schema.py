from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration


def test_fresh_schema_allows_only_registered_artifact_kinds() -> None:
    database_url = os.environ.get("COLLECTOR_DATABASE_URL", "").strip()
    if not database_url:
        pytest.fail("COLLECTOR_DATABASE_URL is required for integration tests")
    inspector = sa.inspect(sa.create_engine(database_url, poolclass=NullPool))

    for table_name, constraint_name in (
        ("artifact_uploads", "ck_artifact_uploads_kind"),
        ("artifact_objects", "ck_artifact_objects_kind"),
    ):
        constraints = {
            item["name"]: " ".join(str(item["sqltext"]).split())
            for item in inspector.get_check_constraints(table_name, schema="sources")
        }
        contract = constraints[constraint_name]
        assert "'raw_artifact'" in contract
        assert "'diagnostic_artifact'" in contract
        assert "'derived_artifact'" in contract
