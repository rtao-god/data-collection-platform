from __future__ import annotations

import pytest
import sqlalchemy as sa
from db_support import database_url
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration

_ARTIFACT_KIND_CONSTRAINTS = (
    ("artifact_uploads", "ck_artifact_uploads_kind"),
    ("artifact_objects", "ck_artifact_objects_kind"),
)


def test_fresh_schema_allows_only_registered_artifact_kinds() -> None:
    engine = sa.create_engine(database_url("derived artifact schema contract"), poolclass=NullPool)
    try:
        inspector = sa.inspect(engine)
        constraints = {
            (table_name, constraint_name): " ".join(str(item["sqltext"]).split())
            for table_name, constraint_name in _ARTIFACT_KIND_CONSTRAINTS
            for item in inspector.get_check_constraints(table_name, schema="sources")
            if item["name"] == constraint_name
        }
    finally:
        engine.dispose()

    assert set(constraints) == set(_ARTIFACT_KIND_CONSTRAINTS)
    for key, contract in constraints.items():
        for kind in ("'raw_artifact'", "'diagnostic_artifact'", "'derived_artifact'"):
            assert kind in contract, f"{key} does not admit {kind}"
