"""Persist entity-resolution outcomes and bind quality evaluations to them.

Stage 7 decided clusters and quality verdicts, but nothing durable owned them: `quality`
held only intake-time placeholders, so no candidate could ever become export-eligible. This
migration adds the resolution owner (one immutable snapshot application per run, superseding
its predecessor explicitly) plus the cluster/pair membership the verdicts refer to, and links
each quality evaluation to the resolution that produced it.

The previous `(candidate_id, candidate_revision, policy_digest)` uniqueness is replaced by
`(resolution_id, candidate_id, candidate_revision)`: the same revision is legitimately
re-evaluated by a later resolution, and the authoritative evaluation is decided by the
explicit `supersedes_resolution_id` chain, never by row order or timestamps.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260822_0019"
down_revision: str | None = "20260821_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGEST_CHECK = "^sha256:[0-9a-f]{64}$"


def upgrade() -> None:
    op.create_table(
        "resolution_runs",
        sa.Column("resolution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_digest", sa.Text(), nullable=False),
        sa.Column("snapshot_digest", sa.Text(), nullable=False),
        sa.Column("market_area_revision", sa.Text(), nullable=False),
        sa.Column("market_area_digest", sa.Text(), nullable=False),
        sa.Column("quality_policy_digest", sa.Text(), nullable=False),
        sa.Column("supersedes_resolution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("applied_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            f"batch_digest ~ '{_DIGEST_CHECK}' AND snapshot_digest ~ '{_DIGEST_CHECK}' "
            f"AND market_area_digest ~ '{_DIGEST_CHECK}' "
            f"AND quality_policy_digest ~ '{_DIGEST_CHECK}'",
            name="ck_resolution_runs_digests",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.collection_runs.run_id"],
            name="fk_resolution_runs_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["work.work_units.work_id"],
            name="fk_resolution_runs_work",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_resolution_id"],
            ["candidates.resolution_runs.resolution_id"],
            name="fk_resolution_runs_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("resolution_id", name="pk_resolution_runs"),
        sa.UniqueConstraint("work_id", name="uq_resolution_runs_work"),
        sa.UniqueConstraint("run_id", "snapshot_digest", name="uq_resolution_runs_run_snapshot"),
        sa.CheckConstraint(
            "supersedes_resolution_id IS NULL OR supersedes_resolution_id <> resolution_id",
            name="ck_resolution_runs_supersedes",
        ),
        schema="candidates",
    )
    # One resolution may supersede at most one predecessor, so the "current" resolution of a
    # run is unique without any ordering over time.
    op.create_index(
        "uq_resolution_runs_superseded",
        "resolution_runs",
        ["supersedes_resolution_id"],
        unique=True,
        schema="candidates",
    )

    op.create_table(
        "resolution_clusters",
        sa.Column("resolution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lineage_kind", sa.Text(), nullable=False),
        sa.Column(
            "parent_cluster_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        sa.Column("parent_membership_digest", sa.Text(), nullable=False),
        sa.Column("blocked_match_edge_ids", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.CheckConstraint(
            "lineage_kind IN ('new', 'unchanged', 'split', 'merge', 'recombined')",
            name="ck_resolution_clusters_lineage_kind",
        ),
        sa.CheckConstraint(
            f"parent_membership_digest ~ '{_DIGEST_CHECK}'",
            name="ck_resolution_clusters_digest",
        ),
        sa.ForeignKeyConstraint(
            ["resolution_id"],
            ["candidates.resolution_runs.resolution_id"],
            name="fk_resolution_clusters_resolution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("resolution_id", "cluster_id", name="pk_resolution_clusters"),
        schema="candidates",
    )

    op.create_table(
        "resolution_cluster_members",
        sa.Column("resolution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["resolution_id", "cluster_id"],
            [
                "candidates.resolution_clusters.resolution_id",
                "candidates.resolution_clusters.cluster_id",
            ],
            name="fk_resolution_cluster_members_cluster",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.candidates.candidate_id"],
            name="fk_resolution_cluster_members_candidate",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "resolution_id",
            "cluster_id",
            "candidate_id",
            name="pk_resolution_cluster_members",
        ),
        sa.UniqueConstraint(
            "resolution_id",
            "candidate_id",
            name="uq_resolution_cluster_members_candidate",
        ),
        schema="candidates",
    )

    op.create_table(
        "resolution_pairs",
        sa.Column("resolution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pair_id", sa.Text(), nullable=False),
        sa.Column("left_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("right_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("reason_codes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("evidence_strength", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            f"pair_id ~ '{_DIGEST_CHECK}'",
            name="ck_resolution_pairs_identity",
        ),
        sa.CheckConstraint(
            "disposition IN ('auto_match', 'manual_match', 'review_required', 'no_match', "
            "'manual_separate')",
            name="ck_resolution_pairs_disposition",
        ),
        sa.CheckConstraint(
            "left_candidate_id <> right_candidate_id",
            name="ck_resolution_pairs_distinct",
        ),
        sa.ForeignKeyConstraint(
            ["resolution_id"],
            ["candidates.resolution_runs.resolution_id"],
            name="fk_resolution_pairs_resolution",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["left_candidate_id"],
            ["candidates.candidates.candidate_id"],
            name="fk_resolution_pairs_left",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["right_candidate_id"],
            ["candidates.candidates.candidate_id"],
            name="fk_resolution_pairs_right",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("resolution_id", "pair_id", name="pk_resolution_pairs"),
        schema="candidates",
    )

    op.create_table(
        "resolution_blocked_edges",
        sa.Column("resolution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("blocked_edge_id", sa.Text(), nullable=False),
        sa.Column("attempted_pair_id", sa.Text(), nullable=False),
        sa.Column("separation_pair_id", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.CheckConstraint(
            f"blocked_edge_id ~ '{_DIGEST_CHECK}' AND attempted_pair_id ~ '{_DIGEST_CHECK}' "
            f"AND separation_pair_id ~ '{_DIGEST_CHECK}'",
            name="ck_resolution_blocked_edges_identities",
        ),
        sa.CheckConstraint(
            "reason_code = 'EXPLICIT_SEPARATION_TRANSITIVE_BLOCK'",
            name="ck_resolution_blocked_edges_reason",
        ),
        sa.ForeignKeyConstraint(
            ["resolution_id"],
            ["candidates.resolution_runs.resolution_id"],
            name="fk_resolution_blocked_edges_resolution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "resolution_id",
            "blocked_edge_id",
            name="pk_resolution_blocked_edges",
        ),
        schema="candidates",
    )

    op.add_column(
        "quality_evaluations",
        sa.Column("resolution_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="quality",
    )
    op.drop_constraint(
        "uq_quality_evaluations_candidate_policy",
        "quality_evaluations",
        schema="quality",
        type_="unique",
    )
    op.create_foreign_key(
        "fk_quality_evaluations_resolution",
        "quality_evaluations",
        "resolution_runs",
        ["resolution_id"],
        ["resolution_id"],
        source_schema="quality",
        referent_schema="candidates",
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_quality_evaluations_resolution_candidate",
        "quality_evaluations",
        ["resolution_id", "candidate_id", "candidate_revision"],
        unique=True,
        schema="quality",
        postgresql_where=sa.text("resolution_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_quality_evaluations_resolution_candidate",
        table_name="quality_evaluations",
        schema="quality",
    )
    op.drop_constraint(
        "fk_quality_evaluations_resolution",
        "quality_evaluations",
        schema="quality",
        type_="foreignkey",
    )
    op.execute(sa.text("DELETE FROM quality.quality_evaluations WHERE resolution_id IS NOT NULL"))
    op.drop_column("quality_evaluations", "resolution_id", schema="quality")
    op.create_unique_constraint(
        "uq_quality_evaluations_candidate_policy",
        "quality_evaluations",
        ["candidate_id", "candidate_revision", "policy_digest"],
        schema="quality",
    )
    for table in (
        "resolution_blocked_edges",
        "resolution_pairs",
        "resolution_cluster_members",
        "resolution_clusters",
    ):
        op.drop_table(table, schema="candidates")
    op.drop_index(
        "uq_resolution_runs_superseded", table_name="resolution_runs", schema="candidates"
    )
    op.drop_table("resolution_runs", schema="candidates")
