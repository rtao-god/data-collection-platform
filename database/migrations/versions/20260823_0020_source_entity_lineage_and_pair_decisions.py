"""Give one logical source entity a stable candidate lineage and own pair adjudication.

Two durable meanings were missing.

The first is cross-run source identity. Candidate identity used to include the run ID and the
retrieved bytes, so acquiring the same source entity again produced a different candidate and
the same studio could reach the Catalog twice. A candidate is now identified by its campaign,
source binding and canonical source record ID, and each genuinely distinct acquisition is
recorded here as the evidence that produced one immutable candidate revision. Replaying the
same acquisition resolves to the same revision, while changed evidence becomes the next
revision of the same candidate.

The second is operator pair adjudication. Resolution already accepted sealed manual decisions,
but nothing durable owned them: an operator could not record a MATCH/SEPARATE judgement and no
later resolution could reuse it. `resolution_pair_decision_commands` keeps one authenticated
command (its idempotency identity and digest), and `resolution_pair_decisions` keeps the
append-only decision revisions it produced, bound to the exact candidate revisions the operator
judged and to the match-relevant context digest of that pair.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260823_0020"
down_revision: str | None = "20260822_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGEST_CHECK = "^sha256:[0-9a-f]{64}$"
# sha256_json([]): the canonical identity of "no operator decision was reflected".
_EMPTY_DECISION_BASIS = "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
_SOURCE_RECORD_ID_CHECK = "^[A-Za-z][A-Za-z0-9._:@+-]{0,127}$"
_CODE_CHECK = "^[A-Z][A-Z0-9_]{0,99}$"


def upgrade() -> None:
    # A resolution is only authoritative for the operator decisions it actually reflected, so
    # the exact decision basis is recorded with it. Every existing resolution was derived from
    # no decisions at all, which is exactly what the empty-set digest states.
    op.add_column(
        "resolution_runs",
        sa.Column(
            "decision_basis_digest",
            sa.Text(),
            nullable=False,
            server_default=_EMPTY_DECISION_BASIS,
        ),
        schema="candidates",
    )
    op.create_check_constraint(
        "ck_resolution_runs_decision_basis",
        "resolution_runs",
        f"decision_basis_digest ~ '{_DIGEST_CHECK}'",
        schema="candidates",
    )
    op.create_table(
        "source_entity_revisions",
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("acquisition_identity", sa.Text(), nullable=False),
        sa.Column("source_record_id", sa.Text(), nullable=False),
        sa.Column("source_record_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_release", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("acquired_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_source_entity_revisions_revision"),
        sa.CheckConstraint(
            f"acquisition_identity ~ '{_DIGEST_CHECK}'",
            name="ck_source_entity_revisions_acquisition_identity",
        ),
        sa.CheckConstraint(
            f"source_record_id ~ '{_SOURCE_RECORD_ID_CHECK}'",
            name="ck_source_entity_revisions_source_record_id",
        ),
        sa.CheckConstraint(
            "char_length(dataset_release) BETWEEN 1 AND 200",
            name="ck_source_entity_revisions_dataset_release",
        ),
        sa.CheckConstraint(
            "char_length(correlation_id) BETWEEN 1 AND 200",
            name="ck_source_entity_revisions_correlation_id",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "revision"],
            [
                "candidates.candidate_revisions.candidate_id",
                "candidates.candidate_revisions.revision",
            ],
            name="fk_source_entity_revisions_candidate_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.collection_runs.run_id"],
            name="fk_source_entity_revisions_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "candidate_id",
            "revision",
            name="pk_source_entity_revisions",
        ),
        # One acquisition produces at most one revision; replaying it must not add another.
        sa.UniqueConstraint(
            "candidate_id",
            "acquisition_identity",
            name="uq_source_entity_revisions_acquisition",
        ),
        schema="candidates",
    )
    op.create_index(
        "ix_source_entity_revisions_source_record",
        "source_entity_revisions",
        ["source_record_id", "candidate_id"],
        schema="candidates",
    )

    op.create_table(
        "resolution_pair_decision_commands",
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("command_digest", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("decision_count", sa.Integer(), nullable=False),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 200",
            name="ck_pair_decision_commands_idempotency_key",
        ),
        sa.CheckConstraint(
            f"command_digest ~ '{_DIGEST_CHECK}'",
            name="ck_pair_decision_commands_command_digest",
        ),
        sa.CheckConstraint(
            "char_length(actor_id) BETWEEN 1 AND 200",
            name="ck_pair_decision_commands_actor",
        ),
        sa.CheckConstraint(
            "decision_count BETWEEN 1 AND 512",
            name="ck_pair_decision_commands_decision_count",
        ),
        sa.CheckConstraint(
            "char_length(correlation_id) BETWEEN 1 AND 200",
            name="ck_pair_decision_commands_correlation_id",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.collection_runs.run_id"],
            name="fk_pair_decision_commands_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("command_id", name="pk_pair_decision_commands"),
        # The idempotency identity is the client's own stable key, so a replay resolves to the
        # original command instead of writing a second decision batch.
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_pair_decision_commands_idempotency_key",
        ),
        schema="candidates",
    )

    op.create_table(
        "resolution_pair_decisions",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pair_id", sa.Text(), nullable=False),
        sa.Column("left_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("right_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("left_candidate_revision", sa.Integer(), nullable=False),
        sa.Column("right_candidate_revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence_references", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("context_digest", sa.Text(), nullable=False),
        sa.Column("decision_digest", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("supersedes_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_pair_decisions_revision"),
        sa.CheckConstraint("action IN ('match', 'separate')", name="ck_pair_decisions_action"),
        sa.CheckConstraint(
            f"pair_id ~ '{_DIGEST_CHECK}' AND context_digest ~ '{_DIGEST_CHECK}' "
            f"AND decision_digest ~ '{_DIGEST_CHECK}'",
            name="ck_pair_decisions_digests",
        ),
        sa.CheckConstraint(
            f"reason_code ~ '{_CODE_CHECK}'",
            name="ck_pair_decisions_reason_code",
        ),
        sa.CheckConstraint(
            "left_candidate_id <> right_candidate_id AND left_candidate_id < right_candidate_id",
            name="ck_pair_decisions_canonical_pair",
        ),
        sa.CheckConstraint(
            "left_candidate_revision >= 0 AND right_candidate_revision >= 0",
            name="ck_pair_decisions_candidate_revisions",
        ),
        sa.CheckConstraint(
            "char_length(rationale) BETWEEN 1 AND 4000 AND rationale !~ '[<>]'",
            name="ck_pair_decisions_rationale",
        ),
        sa.CheckConstraint(
            "cardinality(evidence_references) >= 1 AND "
            "array_position(evidence_references, NULL) IS NULL",
            name="ck_pair_decisions_evidence",
        ),
        sa.CheckConstraint(
            "(revision = 0 AND supersedes_decision_id IS NULL) OR "
            "(revision > 0 AND supersedes_decision_id IS NOT NULL)",
            name="ck_pair_decisions_supersession_shape",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["candidates.resolution_pair_decision_commands.command_id"],
            name="fk_pair_decisions_command",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["left_candidate_id", "left_candidate_revision"],
            [
                "candidates.candidate_revisions.candidate_id",
                "candidates.candidate_revisions.revision",
            ],
            name="fk_pair_decisions_left_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["right_candidate_id", "right_candidate_revision"],
            [
                "candidates.candidate_revisions.candidate_id",
                "candidates.candidate_revisions.revision",
            ],
            name="fk_pair_decisions_right_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_id"],
            ["candidates.resolution_pair_decisions.decision_id"],
            name="fk_pair_decisions_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("decision_id", name="pk_pair_decisions"),
        # Decision history is a chain: one revision per pair, and the superseded decision is
        # superseded exactly once.
        sa.UniqueConstraint("pair_id", "revision", name="uq_pair_decisions_pair_revision"),
        sa.UniqueConstraint("command_id", "pair_id", name="uq_pair_decisions_command_pair"),
        sa.UniqueConstraint(
            "supersedes_decision_id",
            name="uq_pair_decisions_superseded",
        ),
        schema="candidates",
    )
    op.create_index(
        "ix_pair_decisions_active_pair",
        "resolution_pair_decisions",
        ["pair_id", "revision"],
        schema="candidates",
    )
    op.create_index(
        "ix_pair_decisions_run_pair",
        "resolution_pair_decisions",
        ["run_id", "pair_id"],
        schema="candidates",
    )

    # Decision history is evidence: it is only ever appended to. Reuse the shared immutable
    # history trigger so an update or delete fails with the owner's own diagnostic.
    for table in (
        "source_entity_revisions",
        "resolution_pair_decision_commands",
        "resolution_pair_decisions",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON candidates.{table}
            FOR EACH ROW EXECUTE FUNCTION review.reject_immutable_history_mutation()
            """
        )


def downgrade() -> None:
    op.drop_constraint(
        "ck_resolution_runs_decision_basis",
        "resolution_runs",
        type_="check",
        schema="candidates",
    )
    op.drop_column("resolution_runs", "decision_basis_digest", schema="candidates")
    for table in (
        "resolution_pair_decisions",
        "resolution_pair_decision_commands",
        "source_entity_revisions",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON candidates.{table}")
    op.drop_index(
        "ix_pair_decisions_run_pair",
        table_name="resolution_pair_decisions",
        schema="candidates",
    )
    op.drop_index(
        "ix_pair_decisions_active_pair",
        table_name="resolution_pair_decisions",
        schema="candidates",
    )
    op.drop_table("resolution_pair_decisions", schema="candidates")
    op.drop_table("resolution_pair_decision_commands", schema="candidates")
    op.drop_index(
        "ix_source_entity_revisions_source_record",
        table_name="source_entity_revisions",
        schema="candidates",
    )
    op.drop_table("source_entity_revisions", schema="candidates")
