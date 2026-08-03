"""Create immutable campaign config bundle metadata.

Revision ID: 20260804_0001
Revises:
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMMUTABLE_TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER trg_config_bundles_immutable
    BEFORE UPDATE OR DELETE ON config.config_bundles
    FOR EACH ROW
    EXECUTE FUNCTION config.reject_immutable_config_mutation()
    """,
    """
    CREATE TRIGGER trg_config_bundle_components_immutable
    BEFORE UPDATE OR DELETE ON config.config_bundle_components
    FOR EACH ROW
    EXECUTE FUNCTION config.reject_immutable_config_mutation()
    """,
    """
    CREATE TRIGGER trg_config_bundle_blockers_immutable
    BEFORE UPDATE OR DELETE ON config.config_bundle_blockers
    FOR EACH ROW
    EXECUTE FUNCTION config.reject_immutable_config_mutation()
    """,
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE SCHEMA config")

    op.create_table(
        "config_bundles",
        sa.Column("bundle_digest", sa.String(length=71), nullable=False),
        sa.Column("campaign_key", sa.String(length=80), nullable=False),
        sa.Column("contract", sa.String(length=64), nullable=False),
        sa.Column("contract_revision", sa.String(length=64), nullable=False),
        sa.Column("readiness", sa.String(length=16), nullable=False),
        sa.Column("component_count", sa.Integer(), nullable=False),
        sa.Column("blocker_count", sa.Integer(), nullable=False),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "bundle_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_config_bundles_bundle_digest_format",
        ),
        sa.CheckConstraint(
            "campaign_key ~ '^[a-z][a-z0-9_]*$'",
            name="ck_config_bundles_campaign_key_format",
        ),
        sa.CheckConstraint(
            "contract = 'collector-campaign-snapshot'",
            name="ck_config_bundles_contract_identity",
        ),
        sa.CheckConstraint(
            "contract_revision = 'campaign-snapshot-v1'",
            name="ck_config_bundles_contract_revision",
        ),
        sa.CheckConstraint(
            "readiness IN ('ready', 'blocked')",
            name="ck_config_bundles_readiness",
        ),
        sa.CheckConstraint(
            "component_count > 0",
            name="ck_config_bundles_component_count",
        ),
        sa.CheckConstraint(
            "blocker_count >= 0",
            name="ck_config_bundles_blocker_count",
        ),
        sa.CheckConstraint(
            "(readiness = 'ready' AND blocker_count = 0) "
            "OR (readiness = 'blocked' AND blocker_count > 0)",
            name="ck_config_bundles_readiness_blockers",
        ),
        sa.PrimaryKeyConstraint("bundle_digest", name="pk_config_bundles"),
        schema="config",
        comment="Immutable metadata for a canonical campaign snapshot.",
    )
    op.create_index(
        "ix_config_bundles_campaign_recorded_at",
        "config_bundles",
        ("campaign_key", "recorded_at_utc"),
        unique=False,
        schema="config",
    )

    op.create_table(
        "config_bundle_components",
        sa.Column("bundle_digest", sa.String(length=71), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=240), nullable=False),
        sa.Column("component_digest", sa.String(length=71), nullable=False),
        sa.CheckConstraint(
            "position >= 0",
            name="ck_config_bundle_components_position",
        ),
        sa.CheckConstraint(
            "length(btrim(path)) > 0",
            name="ck_config_bundle_components_path_non_empty",
        ),
        sa.CheckConstraint(
            "path !~ '(^|/)\\.\\.(/|$)' AND left(path, 1) <> '/'",
            name="ck_config_bundle_components_path_boundary",
        ),
        sa.CheckConstraint(
            "component_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_config_bundle_components_digest_format",
        ),
        sa.ForeignKeyConstraint(
            ("bundle_digest",),
            ("config.config_bundles.bundle_digest",),
            name="fk_config_bundle_components_bundle_digest_config_bundles",
        ),
        sa.PrimaryKeyConstraint(
            "bundle_digest",
            "position",
            name="pk_config_bundle_components",
        ),
        sa.UniqueConstraint(
            "bundle_digest",
            "path",
            name="uq_config_bundle_components_bundle_path",
        ),
        schema="config",
        comment="Ordered component identities contained in a campaign snapshot.",
    )

    op.create_table(
        "config_bundle_blockers",
        sa.Column("bundle_digest", sa.String(length=71), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column("message", sa.String(length=300), nullable=False),
        sa.Column("required_action", sa.String(length=300), nullable=False),
        sa.CheckConstraint(
            "position >= 0",
            name="ck_config_bundle_blockers_position",
        ),
        sa.CheckConstraint(
            "code ~ '^[A-Z][A-Z0-9_]+$'",
            name="ck_config_bundle_blockers_code_format",
        ),
        sa.CheckConstraint(
            "length(btrim(owner)) > 0",
            name="ck_config_bundle_blockers_owner_non_empty",
        ),
        sa.CheckConstraint(
            "length(btrim(message)) > 0",
            name="ck_config_bundle_blockers_message_non_empty",
        ),
        sa.CheckConstraint(
            "length(btrim(required_action)) > 0",
            name="ck_config_bundle_blockers_required_action_non_empty",
        ),
        sa.ForeignKeyConstraint(
            ("bundle_digest",),
            ("config.config_bundles.bundle_digest",),
            name="fk_config_bundle_blockers_bundle_digest_config_bundles",
        ),
        sa.PrimaryKeyConstraint(
            "bundle_digest",
            "position",
            name="pk_config_bundle_blockers",
        ),
        schema="config",
        comment=(
            "Ordered explicit blockers that keep a campaign snapshot from production use."
        ),
    )

    op.execute(
        """
        CREATE FUNCTION config.reject_immutable_config_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'immutable campaign configuration cannot be updated or deleted',
                DETAIL = format('%I.%I is insert-only', TG_TABLE_SCHEMA, TG_TABLE_NAME);
            RETURN NULL;
        END;
        $$
        """
    )
    for statement in _IMMUTABLE_TRIGGER_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("config_bundle_blockers", schema="config")
    op.drop_table("config_bundle_components", schema="config")
    op.drop_index(
        "ix_config_bundles_campaign_recorded_at",
        table_name="config_bundles",
        schema="config",
    )
    op.drop_table("config_bundles", schema="config")
    op.execute("DROP FUNCTION config.reject_immutable_config_mutation()")
    op.execute("DROP SCHEMA config")
    # PostGIS is a database prerequisite and may be shared by later revisions; it is not removed.
