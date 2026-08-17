from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from collection_infrastructure.postgres.manual_import_metadata import plan_admissions


def test_manual_import_admission_metadata_owns_canonical_plan_and_record_route() -> None:
    sql = str(CreateTable(plan_admissions).compile(dialect=postgresql.dialect()))

    assert "source_artifact_role TEXT NOT NULL" in sql
    assert "plan_disposition TEXT NOT NULL" in sql
    assert "valid_record_count INTEGER NOT NULL" in sql
    assert "issue_count INTEGER NOT NULL" in sql
    assert "total_record_count" not in sql
    assert "plan_status" not in sql
    assert "CONSTRAINT ck_plan_admissions_source_role" in sql
    assert "CONSTRAINT ck_plan_admissions_disposition_shape" in sql
    assert "CONSTRAINT ck_plan_admissions_target_owner" in sql
    assert "target_stage = 'discovery'" in sql
    assert "target_capability = 'manual_record'" in sql
    assert "target_output_contract = 'manual-import-record@1'" in sql
