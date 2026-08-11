from __future__ import annotations

from collection_infrastructure.postgres import (
    CONFIG_SCHEMA,
    CONFIG_TABLES,
    config_bundle_blockers,
    config_bundle_components,
    config_bundles,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable


def test_config_metadata_has_only_owned_atomically_sealed_tables() -> None:
    assert CONFIG_SCHEMA == "config"
    assert tuple(table.fullname for table in CONFIG_TABLES) == (
        "config.config_bundles",
        "config.config_bundle_components",
        "config.config_bundle_blockers",
    )

    assert config_bundles.primary_key.columns.keys() == ["bundle_digest"]
    assert config_bundle_components.primary_key.columns.keys() == [
        "bundle_digest",
        "position",
    ]
    assert config_bundle_blockers.primary_key.columns.keys() == [
        "bundle_digest",
        "position",
    ]

    for table in CONFIG_TABLES:
        assert all(foreign_key.ondelete is None for foreign_key in table.foreign_keys)
        assert all(column.server_default is None for column in table.columns)

    for table in (config_bundle_components, config_bundle_blockers):
        foreign_key_constraint = next(iter(table.foreign_key_constraints))
        assert foreign_key_constraint.deferrable is True
        assert foreign_key_constraint.initially == "DEFERRED"


def test_config_metadata_compiles_postgresql_contract_constraints() -> None:
    dialect = postgresql.dialect()
    root_sql = str(CreateTable(config_bundles).compile(dialect=dialect))
    component_sql = str(CreateTable(config_bundle_components).compile(dialect=dialect))
    blocker_sql = str(CreateTable(config_bundle_blockers).compile(dialect=dialect))

    assert "collector-campaign-snapshot" in root_sql
    assert "campaign-snapshot-v1" in root_sql
    assert "component_count" not in root_sql
    assert "blocker_count" not in root_sql
    assert "ON DELETE CASCADE" not in component_sql
    assert "ON DELETE CASCADE" not in blocker_sql
    assert "DEFERRABLE INITIALLY DEFERRED" in component_sql
    assert "DEFERRABLE INITIALLY DEFERRED" in blocker_sql
    assert "component_digest ~ '^sha256:[0-9a-f]{64}$'" in component_sql
    assert "code ~ '^[A-Z][A-Z0-9_]+$'" in blocker_sql
