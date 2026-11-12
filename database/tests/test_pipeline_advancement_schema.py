from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from db_support import database_url

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260815_0013_pipeline_advancement.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pipeline_advancement_migration",
        _MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pipeline_advancement_migration_stays_linear_after_run_control() -> None:
    """The migration graph must stay a single linear chain and follow run control."""
    migration = _load_migration()

    assert migration.down_revision == "20260815_0012"


@pytest.mark.integration
def test_pipeline_advancement_schema_guards_are_installed() -> None:
    """Fail-closed checks and the append-only trigger must exist in the migrated schema."""
    engine = sa.create_engine(database_url("pipeline advancement schema contract"))
    try:
        inspector = sa.inspect(engine)
        checks = {
            item["name"]
            for item in inspector.get_check_constraints(
                "pipeline_advancements",
                schema="work",
            )
        }
        with engine.connect() as connection:
            triggers = {
                row[0]
                for row in connection.execute(
                    sa.text(
                        """
                        SELECT trigger_name
                        FROM information_schema.triggers
                        WHERE event_object_schema = 'work'
                          AND event_object_table = 'pipeline_advancement_attempts'
                        """
                    )
                )
            }
    finally:
        engine.dispose()

    assert {"ck_pipeline_advancements_terminal_payload"} <= checks
    assert {"trg_pipeline_advancement_attempts_immutable"} <= triggers
