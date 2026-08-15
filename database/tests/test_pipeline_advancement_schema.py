from __future__ import annotations

from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260815_0013_pipeline_advancement.py"
)


def test_pipeline_advancement_migration_is_linear_and_owner_scoped() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260815_0013"' in source
    assert 'down_revision: str | None = "20260815_0012"' in source
    assert '"pipeline_advancements"' in source
    assert '"pipeline_advancement_attempts"' in source
    assert 'schema="work"' in source
    assert '"work.work_units.work_id"' in source
    assert '"work.work_units.work_unit_id"' not in source
    assert "CASCADE" not in source


def test_pipeline_advancement_migration_enforces_fail_closed_state() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")

    assert "ck_pipeline_advancements_terminal_payload" in source
    assert "ck_pipeline_advancements_lease_payload" in source
    assert "ck_pipeline_advancement_attempts_event_payload" in source
    assert "source_output_digest ~ '^sha256:[0-9a-f]{64}$'" in source
    assert "transition_plan_digest ~ '^sha256:[0-9a-f]{64}$'" in source
    assert "jsonb_typeof(source_input_artifacts) = 'array'" in source


def test_pipeline_advancement_attempt_history_is_append_only() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")

    assert "reject_pipeline_advancement_attempt_mutation" in source
    assert "BEFORE UPDATE OR DELETE ON work.pipeline_advancement_attempts" in source
    assert "pipeline advancement attempt history is immutable" in source
    assert 'ondelete="RESTRICT"' in source
