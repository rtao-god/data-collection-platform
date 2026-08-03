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
        ("config_bundle_components", "trg_config_bundle_components_immutable"),
        ("config_bundle_blockers", "trg_config_bundle_blockers_immutable"),
    }

    root_checks = {
        constraint["name"] for constraint in inspector.get_check_constraints(
            "config_bundles", schema="config"
        )
    }
    assert {
        "ck_config_bundles_bundle_digest_format",
        "ck_config_bundles_campaign_key_format",
        "ck_config_bundles_contract_identity",
        "ck_config_bundles_contract_revision",
        "ck_config_bundles_readiness",
        "ck_config_bundles_component_count",
        "ck_config_bundles_blocker_count",
        "ck_config_bundles_readiness_blockers",
    }.issubset(root_checks)


def test_config_records_are_insert_only_and_fail_closed() -> None:
    engine = sa.create_engine(_database_url(), poolclass=NullPool)
    bundle_digest = "sha256:" + ("a" * 64)
    component_digest = "sha256:" + ("b" * 64)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO config.config_bundles (
                    bundle_digest,
                    campaign_key,
                    contract,
                    contract_revision,
                    readiness,
                    component_count,
                    blocker_count,
                    recorded_at_utc
                ) VALUES (
                    :bundle_digest,
                    'integration_campaign',
                    'collector-campaign-snapshot',
                    'campaign-snapshot-v1',
                    'ready',
                    1,
                    0,
                    '2026-08-04T00:00:00Z'
                )
                """
            ),
            {"bundle_digest": bundle_digest},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO config.config_bundle_components (
                    bundle_digest,
                    position,
                    path,
                    component_digest
                ) VALUES (:bundle_digest, 0, 'campaign.yaml', :component_digest)
                """
            ),
            {
                "bundle_digest": bundle_digest,
                "component_digest": component_digest,
            },
        )

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

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO config.config_bundles (
                        bundle_digest,
                        campaign_key,
                        contract,
                        contract_revision,
                        readiness,
                        component_count,
                        blocker_count,
                        recorded_at_utc
                    ) VALUES (
                        :bundle_digest,
                        'invalid_ready_campaign',
                        'collector-campaign-snapshot',
                        'campaign-snapshot-v1',
                        'ready',
                        1,
                        1,
                        '2026-08-04T00:00:00Z'
                    )
                    """
                ),
                {"bundle_digest": "sha256:" + ("c" * 64)},
            )
