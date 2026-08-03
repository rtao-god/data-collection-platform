from __future__ import annotations

import sqlalchemy as sa

CONFIG_SCHEMA = "config"

collector_metadata = sa.MetaData()

config_bundles = sa.Table(
    "config_bundles",
    collector_metadata,
    sa.Column("bundle_digest", sa.String(71), nullable=False),
    sa.Column("campaign_key", sa.String(80), nullable=False),
    sa.Column("contract", sa.String(64), nullable=False),
    sa.Column("contract_revision", sa.String(64), nullable=False),
    sa.Column("readiness", sa.String(16), nullable=False),
    sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("bundle_digest", name="pk_config_bundles"),
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
    schema=CONFIG_SCHEMA,
    comment="Immutable metadata for a canonical campaign snapshot.",
)

sa.Index(
    "ix_config_bundles_campaign_recorded_at",
    config_bundles.c.campaign_key,
    config_bundles.c.recorded_at_utc,
)

config_bundle_components = sa.Table(
    "config_bundle_components",
    collector_metadata,
    sa.Column("bundle_digest", sa.String(71), nullable=False),
    sa.Column("position", sa.Integer(), nullable=False),
    sa.Column("path", sa.String(240), nullable=False),
    sa.Column("component_digest", sa.String(71), nullable=False),
    sa.PrimaryKeyConstraint(
        "bundle_digest",
        "position",
        name="pk_config_bundle_components",
    ),
    sa.ForeignKeyConstraint(
        ("bundle_digest",),
        ("config.config_bundles.bundle_digest",),
        name="fk_config_bundle_components_bundle_digest_config_bundles",
        deferrable=True,
        initially="DEFERRED",
    ),
    sa.UniqueConstraint(
        "bundle_digest",
        "path",
        name="uq_config_bundle_components_bundle_path",
    ),
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
    schema=CONFIG_SCHEMA,
    comment="Ordered component identities contained in a campaign snapshot.",
)

config_bundle_blockers = sa.Table(
    "config_bundle_blockers",
    collector_metadata,
    sa.Column("bundle_digest", sa.String(71), nullable=False),
    sa.Column("position", sa.Integer(), nullable=False),
    sa.Column("code", sa.String(100), nullable=False),
    sa.Column("owner", sa.String(100), nullable=False),
    sa.Column("message", sa.String(300), nullable=False),
    sa.Column("required_action", sa.String(300), nullable=False),
    sa.PrimaryKeyConstraint(
        "bundle_digest",
        "position",
        name="pk_config_bundle_blockers",
    ),
    sa.ForeignKeyConstraint(
        ("bundle_digest",),
        ("config.config_bundles.bundle_digest",),
        name="fk_config_bundle_blockers_bundle_digest_config_bundles",
        deferrable=True,
        initially="DEFERRED",
    ),
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
    schema=CONFIG_SCHEMA,
    comment="Ordered explicit blockers that keep a campaign snapshot from production use.",
)

CONFIG_TABLES = (
    config_bundles,
    config_bundle_components,
    config_bundle_blockers,
)
