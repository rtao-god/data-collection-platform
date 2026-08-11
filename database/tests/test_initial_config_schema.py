from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration


def _database_url() -> str:
    value = os.environ.get("COLLECTOR_DATABASE_URL", "").strip()
    if not value:
        pytest.fail(
            "COLLECTOR_DATABASE_URL is required for the PostgreSQL/PostGIS integration contract."
        )
    return value


def _insert_component(connection: sa.Connection, bundle_digest: str, position: int = 0) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO config.config_bundle_components (
                bundle_digest,
                position,
                path,
                component_digest
            ) VALUES (
                :bundle_digest,
                :position,
                :path,
                :component_digest
            )
            """
        ),
        {
            "bundle_digest": bundle_digest,
            "position": position,
            "path": f"component-{position}.yaml",
            "component_digest": "sha256:" + (f"{position + 1:x}" * 64)[:64],
        },
    )


def _insert_blocker(connection: sa.Connection, bundle_digest: str, position: int = 0) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO config.config_bundle_blockers (
                bundle_digest,
                position,
                code,
                owner,
                message,
                required_action
            ) VALUES (
                :bundle_digest,
                :position,
                'TEST_BLOCKER',
                'IntegrationTest',
                'The test bundle is intentionally blocked.',
                'Insert it with blocked readiness.'
            )
            """
        ),
        {"bundle_digest": bundle_digest, "position": position},
    )


def _insert_bundle(
    connection: sa.Connection,
    bundle_digest: str,
    *,
    readiness: str,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO config.config_bundles (
                bundle_digest,
                campaign_key,
                contract,
                contract_revision,
                readiness,
                recorded_at_utc
            ) VALUES (
                :bundle_digest,
                'integration_campaign',
                'collector-campaign-snapshot',
                'campaign-snapshot-v1',
                :readiness,
                '2026-08-04T00:00:00Z'
            )
            """
        ),
        {"bundle_digest": bundle_digest, "readiness": readiness},
    )


def test_fresh_migration_creates_exact_config_owner_contract() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)
    inspector = sa.inspect(engine)

    assert set(inspector.get_table_names(schema="config")) == {
        "config_bundles",
        "config_bundle_components",
        "config_bundle_blockers",
    }

    with engine.connect() as connection:
        postgis_version = connection.scalar(sa.text("SELECT postgis_version()"))
        trigger_rows = connection.execute(
            sa.text(
                """
                SELECT table_class.relname, trigger.tgname
                FROM pg_trigger AS trigger
                JOIN pg_class AS table_class ON table_class.oid = trigger.tgrelid
                JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
                WHERE namespace.nspname = 'config'
                  AND NOT trigger.tgisinternal
                ORDER BY table_class.relname, trigger.tgname
                """
            )
        ).all()

    assert isinstance(postgis_version, str)
    assert postgis_version
    assert {tuple(row) for row in trigger_rows} == {
        ("config_bundles", "trg_config_bundles_immutable"),
        ("config_bundles", "trg_config_bundles_validate_insert"),
        ("config_bundle_components", "trg_config_bundle_components_guard_insert"),
        ("config_bundle_components", "trg_config_bundle_components_immutable"),
        ("config_bundle_blockers", "trg_config_bundle_blockers_guard_insert"),
        ("config_bundle_blockers", "trg_config_bundle_blockers_immutable"),
    }

    root_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("config_bundles", schema="config")
    }
    assert {
        "ck_config_bundles_bundle_digest_format",
        "ck_config_bundles_campaign_key_format",
        "ck_config_bundles_contract_identity",
        "ck_config_bundles_contract_revision",
        "ck_config_bundles_readiness",
    }.issubset(root_checks)

    component_foreign_keys = inspector.get_foreign_keys("config_bundle_components", schema="config")
    assert component_foreign_keys[0]["options"] == {
        "initially": "DEFERRED",
        "deferrable": True,
    }


def test_config_bundle_is_atomically_sealed_and_immutable() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)
    bundle_digest = "sha256:" + ("a" * 64)

    with engine.begin() as connection:
        _insert_component(connection, bundle_digest)
        _insert_bundle(connection, bundle_digest, readiness="ready")

    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            _insert_component(connection, bundle_digest, position=1)

    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    UPDATE config.config_bundles
                    SET campaign_key = 'mutated_campaign'
                    WHERE bundle_digest = :bundle_digest
                    """
                ),
                {"bundle_digest": bundle_digest},
            )

    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    DELETE FROM config.config_bundle_components
                    WHERE bundle_digest = :bundle_digest
                    """
                ),
                {"bundle_digest": bundle_digest},
            )


def test_config_bundle_rejects_incomplete_or_inconsistent_insert() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_bundle(connection, "sha256:" + ("b" * 64), readiness="ready")

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            digest = "sha256:" + ("c" * 64)
            _insert_component(connection, digest)
            _insert_blocker(connection, digest)
            _insert_bundle(connection, digest, readiness="ready")

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            digest = "sha256:" + ("d" * 64)
            _insert_component(connection, digest, position=1)
            _insert_bundle(connection, digest, readiness="ready")
