from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def add_list_item(text: str, *, section_start: int, assignment: str, item: str) -> str:
    assignment_start = text.index(assignment, section_start)
    list_start = text.index("[", assignment_start)
    list_end = text.index("\n]", list_start)
    rendered = f'  "{item}",'
    if rendered in text[list_start:list_end]:
        return text
    return text[:list_end] + f"\n{rendered}" + text[list_end:]


def update_workspace() -> None:
    path = ROOT / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    workspace_start = text.index("[tool.uv.workspace]")
    for member in ("packages/review_contracts", "packages/review_core"):
        text = add_list_item(
            text,
            section_start=workspace_start,
            assignment="members = [",
            item=member,
        )
    mypy_start = text.index("[tool.mypy]")
    for file_path in (
        "packages/review_contracts/src/review_contracts",
        "packages/review_core/src/review_core",
        "tools/review_contract_generation/generate.py",
    ):
        text = add_list_item(
            text,
            section_start=mypy_start,
            assignment="files = [",
            item=file_path,
        )
    path.write_text(text, encoding="utf-8")


def update_architecture() -> None:
    checker = ROOT / "tools/architecture_checks/check_dependencies.py"
    text = checker.read_text(encoding="utf-8")
    if '"review_contracts": OwnerPolicy(' not in text:
        marker = '    "collection_infrastructure": OwnerPolicy(\n'
        insertion = '''    "review_contracts": OwnerPolicy(
        project_path="packages/review_contracts",
        distribution_name="review-contracts",
        allowed_internal_imports=(),
        allowed_external_imports=frozenset({"pydantic"}),
    ),
    "review_core": OwnerPolicy(
        project_path="packages/review_core",
        distribution_name="review-core",
        allowed_internal_imports=("review_contracts",),
        allowed_external_imports=frozenset(),
    ),
'''
        if marker not in text:
            raise RuntimeError("architecture policy insertion point is missing")
        checker.write_text(text.replace(marker, insertion + marker, 1), encoding="utf-8")

    policy = subprocess.check_output(
        [sys.executable, str(checker), "--print-policy"],
        text=True,
    ).strip()
    doc = ROOT / "docs/architecture/dependency-rules.md"
    doc_text = doc.read_text(encoding="utf-8")
    start_marker = "<!-- dependency-policy:start -->"
    end_marker = "<!-- dependency-policy:end -->"
    start = doc_text.index(start_marker)
    end = doc_text.index(end_marker, start) + len(end_marker)
    doc.write_text(doc_text[:start] + policy + doc_text[end:], encoding="utf-8")


def update_status() -> None:
    path = ROOT / "docs/implementation-status.md"
    text = path.read_text(encoding="utf-8")
    marker = "## Stage 8A — candidate and review foundation"
    if marker not in text:
        text = text.rstrip() + f'''\n\n{marker}\n\nStatus: **contracts, pure transitions, and append-only schema implemented; runtime adapter and Control API remain**.\n\n- `review_contracts` owns candidate revisions, review commands/decisions, manual observations, and suppression revisions.\n- `review_core` owns optimistic-concurrency transitions and immutable supersession semantics.\n- Migration `20260814_0009` owns candidate, quality, and review history tables with insert-only enforcement.\n- Manual observations append evidence and never mutate source observations or candidate snapshots.\n- Suppression has explicit discovery, normalization, and export scopes.\n- PostgreSQL command adapter, Control API, authentication, and review UI are not claimed by this block.\n'''
        path.write_text(text, encoding="utf-8")


def main() -> int:
    write(
        "database/migrations/versions/20260814_0009_candidate_review_foundation.py",
        '''"""Add append-only candidate, quality, and review ownership.

Revision ID: 20260814_0009
Revises: 20260813_0008
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260814_0009"
down_revision: str | None = "20260813_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGEST_CHECK = "^sha256:[0-9a-f]{64}$"
_KEY_CHECK = "^[a-z][a-z0-9_]{0,99}$"
_CODE_CHECK = "^[A-Z][A-Z0-9_]{0,99}$"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS candidates")
    op.execute("CREATE SCHEMA IF NOT EXISTS quality")
    op.execute("CREATE SCHEMA IF NOT EXISTS review")

    op.create_table(
        "candidates",
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_kind", sa.Text(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            f"entity_kind ~ '{_KEY_CHECK}'",
            name="ck_candidates_entity_kind",
        ),
        sa.CheckConstraint(
            "char_length(correlation_id) BETWEEN 1 AND 200",
            name="ck_candidates_correlation_id",
        ),
        sa.PrimaryKeyConstraint("candidate_id", name="pk_candidates"),
        schema="candidates",
    )
    op.create_table(
        "candidate_revisions",
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resolution_state", sa.Text(), nullable=False),
        sa.Column("snapshot_digest", sa.Text(), nullable=False),
        sa.Column("source_lineage_digest", sa.Text(), nullable=False),
        sa.Column("normalized_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_candidate_revisions_revision"),
        sa.CheckConstraint(
            "resolution_state IN ('resolved', 'review', 'blocked')",
            name="ck_candidate_revisions_resolution_state",
        ),
        sa.CheckConstraint(
            f"snapshot_digest ~ '{_DIGEST_CHECK}' AND "
            f"source_lineage_digest ~ '{_DIGEST_CHECK}'",
            name="ck_candidate_revisions_digests",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object'",
            name="ck_candidate_revisions_payload",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.candidates.candidate_id"],
            name="fk_candidate_revisions_candidate_id_candidates",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "candidate_id",
            "revision",
            name="pk_candidate_revisions",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "snapshot_digest",
            name="uq_candidate_revisions_snapshot",
        ),
        schema="candidates",
    )
    op.create_table(
        "candidate_revision_evidence",
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_revision", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("evidence_kind", sa.Text(), nullable=False),
        sa.Column("evidence_digest", sa.Text(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_candidate_evidence_position"),
        sa.CheckConstraint(
            "evidence_kind IN ('source_observation', 'manual_observation', 'artifact')",
            name="ck_candidate_evidence_kind",
        ),
        sa.CheckConstraint(
            f"evidence_digest ~ '{_DIGEST_CHECK}'",
            name="ck_candidate_evidence_digest",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "candidate_revision"],
            [
                "candidates.candidate_revisions.candidate_id",
                "candidates.candidate_revisions.revision",
            ],
            name="fk_candidate_evidence_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "candidate_id",
            "candidate_revision",
            "position",
            name="pk_candidate_revision_evidence",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "candidate_revision",
            "evidence_digest",
            name="uq_candidate_revision_evidence_digest",
        ),
        schema="candidates",
    )

    op.create_table(
        "quality_evaluations",
        sa.Column("evaluation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_revision", sa.Integer(), nullable=False),
        sa.Column("policy_digest", sa.Text(), nullable=False),
        sa.Column("export_eligible", sa.Boolean(), nullable=False),
        sa.Column("blockers", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("evaluation_digest", sa.Text(), nullable=False),
        sa.Column("evaluated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            f"policy_digest ~ '{_DIGEST_CHECK}' AND "
            f"evaluation_digest ~ '{_DIGEST_CHECK}'",
            name="ck_quality_evaluations_digests",
        ),
        sa.CheckConstraint(
            "array_position(blockers, NULL) IS NULL AND "
            "export_eligible = (cardinality(blockers) = 0)",
            name="ck_quality_evaluations_result",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "candidate_revision"],
            [
                "candidates.candidate_revisions.candidate_id",
                "candidates.candidate_revisions.revision",
            ],
            name="fk_quality_evaluations_candidate_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("evaluation_id", name="pk_quality_evaluations"),
        sa.UniqueConstraint(
            "candidate_id",
            "candidate_revision",
            "policy_digest",
            name="uq_quality_evaluations_candidate_policy",
        ),
        schema="quality",
    )

    op.create_table(
        "review_cases",
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_revision", sa.Integer(), nullable=False),
        sa.Column("opened_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id", "candidate_revision"],
            [
                "candidates.candidate_revisions.candidate_id",
                "candidates.candidate_revisions.revision",
            ],
            name="fk_review_cases_candidate_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("case_id", name="pk_review_cases"),
        sa.UniqueConstraint(
            "candidate_id",
            "candidate_revision",
            name="uq_review_cases_candidate_revision",
        ),
        schema="review",
    )
    op.create_table(
        "review_case_revisions",
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason_codes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("current_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_review_case_revisions_revision"),
        sa.CheckConstraint(
            "state IN ('open', 'decided')",
            name="ck_review_case_revisions_state",
        ),
        sa.CheckConstraint(
            "cardinality(reason_codes) >= 1 AND array_position(reason_codes, NULL) IS NULL",
            name="ck_review_case_revisions_reasons",
        ),
        sa.CheckConstraint(
            "(state = 'open' AND current_decision_id IS NULL) OR "
            "(state = 'decided' AND current_decision_id IS NOT NULL)",
            name="ck_review_case_revisions_decision_shape",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["review.review_cases.case_id"],
            name="fk_review_case_revisions_case_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "case_id",
            "revision",
            name="pk_review_case_revisions",
        ),
        schema="review",
    )
    op.create_table(
        "review_decisions",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_revision", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence_references", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("supersedes_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("command_digest", sa.Text(), nullable=False),
        sa.Column("decided_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('accept_candidate', 'reject_candidate', 'approve_merge', "
            "'reject_merge', 'request_recollection', 'block_export')",
            name="ck_review_decisions_outcome",
        ),
        sa.CheckConstraint(
            "char_length(actor_id) BETWEEN 1 AND 200 AND "
            "char_length(rationale) BETWEEN 1 AND 4000 AND "
            "rationale !~ '[<>]'",
            name="ck_review_decisions_plain_text",
        ),
        sa.CheckConstraint(
            "cardinality(evidence_references) >= 1 AND "
            "array_position(evidence_references, NULL) IS NULL",
            name="ck_review_decisions_evidence",
        ),
        sa.CheckConstraint(
            f"command_digest ~ '{_DIGEST_CHECK}'",
            name="ck_review_decisions_command_digest",
        ),
        sa.ForeignKeyConstraint(
            ["case_id", "case_revision"],
            [
                "review.review_case_revisions.case_id",
                "review.review_case_revisions.revision",
            ],
            name="fk_review_decisions_case_revision",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_id"],
            ["review.review_decisions.decision_id"],
            name="fk_review_decisions_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("decision_id", name="pk_review_decisions"),
        sa.UniqueConstraint("command_digest", name="uq_review_decisions_command_digest"),
        sa.UniqueConstraint(
            "case_id",
            "case_revision",
            name="uq_review_decisions_case_revision",
        ),
        schema="review",
    )
    op.create_foreign_key(
        "fk_review_case_revisions_current_decision",
        "review_case_revisions",
        "review_decisions",
        ["current_decision_id"],
        ["decision_id"],
        source_schema="review",
        referent_schema="review",
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "manual_observations",
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_revision", sa.Integer(), nullable=False),
        sa.Column("field_key", sa.Text(), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=False),
        sa.Column("value_digest", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("supersedes_observation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("command_digest", sa.Text(), nullable=False),
        sa.Column("recorded_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint(f"field_key ~ '{_KEY_CHECK}'", name="ck_manual_observations_field_key"),
        sa.CheckConstraint(
            "char_length(value_text) BETWEEN 1 AND 4000 AND value_text !~ '[<>]'",
            name="ck_manual_observations_plain_text",
        ),
        sa.CheckConstraint(
            f"value_digest ~ '{_DIGEST_CHECK}' AND command_digest ~ '{_DIGEST_CHECK}'",
            name="ck_manual_observations_digests",
        ),
        sa.CheckConstraint(f"reason_code ~ '{_CODE_CHECK}'", name="ck_manual_observations_reason"),
        sa.ForeignKeyConstraint(
            ["candidate_id", "candidate_revision"],
            [
                "candidates.candidate_revisions.candidate_id",
                "candidates.candidate_revisions.revision",
            ],
            name="fk_manual_observations_candidate_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_observation_id"],
            ["review.manual_observations.observation_id"],
            name="fk_manual_observations_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("observation_id", name="pk_manual_observations"),
        sa.UniqueConstraint("command_digest", name="uq_manual_observations_command_digest"),
        schema="review",
    )

    op.create_table(
        "suppression_revisions",
        sa.Column("suppression_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("suppress_discovery", sa.Boolean(), nullable=False),
        sa.Column("suppress_normalization", sa.Boolean(), nullable=False),
        sa.Column("suppress_export", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("evidence_reference", sa.Text(), nullable=False),
        sa.Column("starts_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("command_digest", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_suppression_revisions_revision"),
        sa.CheckConstraint(
            "state IN ('active', 'resolved')",
            name="ck_suppression_revisions_state",
        ),
        sa.CheckConstraint(
            "target_kind IN ('candidate', 'source_observation', 'artifact', 'source')",
            name="ck_suppression_revisions_target_kind",
        ),
        sa.CheckConstraint(
            "suppress_discovery OR suppress_normalization OR suppress_export",
            name="ck_suppression_revisions_scope",
        ),
        sa.CheckConstraint(
            f"reason_code ~ '{_CODE_CHECK}' AND "
            f"evidence_reference ~ '{_DIGEST_CHECK}' AND "
            f"command_digest ~ '{_DIGEST_CHECK}'",
            name="ck_suppression_revisions_identity",
        ),
        sa.CheckConstraint(
            "(state = 'active' AND resolved_at_utc IS NULL) OR "
            "(state = 'resolved' AND resolved_at_utc IS NOT NULL)",
            name="ck_suppression_revisions_state_shape",
        ),
        sa.CheckConstraint(
            "expires_at_utc IS NULL OR expires_at_utc > starts_at_utc",
            name="ck_suppression_revisions_expiry",
        ),
        sa.PrimaryKeyConstraint(
            "suppression_id",
            "revision",
            name="pk_suppression_revisions",
        ),
        sa.UniqueConstraint(
            "command_digest",
            name="uq_suppression_revisions_command_digest",
        ),
        schema="review",
    )
    op.create_index(
        "ix_suppression_revisions_target",
        "suppression_revisions",
        ["target_kind", "target_id", "revision"],
        schema="review",
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION review.reject_immutable_history_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'immutable candidate/review history cannot be updated or deleted'
                USING ERRCODE = '55000', DETAIL = TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME;
        END;
        $$
        """
    )
    immutable_tables = (
        ("candidates", "candidates"),
        ("candidates", "candidate_revisions"),
        ("candidates", "candidate_revision_evidence"),
        ("quality", "quality_evaluations"),
        ("review", "review_cases"),
        ("review", "review_case_revisions"),
        ("review", "review_decisions"),
        ("review", "manual_observations"),
        ("review", "suppression_revisions"),
    )
    for schema, table in immutable_tables:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON {schema}.{table}
            FOR EACH ROW EXECUTE FUNCTION review.reject_immutable_history_mutation()
            """
        )


def downgrade() -> None:
    immutable_tables = (
        ("review", "suppression_revisions"),
        ("review", "manual_observations"),
        ("review", "review_decisions"),
        ("review", "review_case_revisions"),
        ("review", "review_cases"),
        ("quality", "quality_evaluations"),
        ("candidates", "candidate_revision_evidence"),
        ("candidates", "candidate_revisions"),
        ("candidates", "candidates"),
    )
    for schema, table in immutable_tables:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {schema}.{table}")
    op.drop_index(
        "ix_suppression_revisions_target",
        table_name="suppression_revisions",
        schema="review",
    )
    op.drop_table("suppression_revisions", schema="review")
    op.drop_table("manual_observations", schema="review")
    op.drop_constraint(
        "fk_review_case_revisions_current_decision",
        "review_case_revisions",
        schema="review",
        type_="foreignkey",
    )
    op.drop_table("review_decisions", schema="review")
    op.drop_table("review_case_revisions", schema="review")
    op.drop_table("review_cases", schema="review")
    op.drop_table("quality_evaluations", schema="quality")
    op.drop_table("candidate_revision_evidence", schema="candidates")
    op.drop_table("candidate_revisions", schema="candidates")
    op.drop_table("candidates", schema="candidates")
    op.execute("DROP FUNCTION IF EXISTS review.reject_immutable_history_mutation()")
    op.execute("DROP SCHEMA IF EXISTS review")
    op.execute("DROP SCHEMA IF EXISTS quality")
    op.execute("DROP SCHEMA IF EXISTS candidates")
''',
    )

    write(
        "database/tests/test_candidate_review_schema.py",
        '''from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, InternalError
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def database_url() -> str:
    value = os.getenv("COLLECTOR_DATABASE_URL", "").strip()
    if not value:
        pytest.fail("COLLECTOR_DATABASE_URL is required")
    return value


def test_candidate_review_schema_is_exact_and_restrictive() -> None:
    engine = sa.create_engine(database_url(), poolclass=NullPool)
    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names(schema="candidates")) == {
        "candidate_revision_evidence",
        "candidate_revisions",
        "candidates",
    }
    assert set(inspector.get_table_names(schema="quality")) == {
        "quality_evaluations",
    }
    assert set(inspector.get_table_names(schema="review")) == {
        "manual_observations",
        "review_case_revisions",
        "review_cases",
        "review_decisions",
        "suppression_revisions",
    }
    for schema, table in (
        ("candidates", "candidate_revisions"),
        ("quality", "quality_evaluations"),
        ("review", "review_decisions"),
        ("review", "manual_observations"),
        ("review", "suppression_revisions"),
    ):
        assert all(
            foreign_key.get("options", {}).get("ondelete") in {None, "RESTRICT"}
            for foreign_key in inspector.get_foreign_keys(table, schema=schema)
        )


def test_candidate_review_history_is_append_only_and_plain_text() -> None:
    engine = sa.create_engine(database_url(), poolclass=NullPool)
    candidate_id = uuid4()
    cluster_id = uuid4()
    case_id = uuid4()
    decision_id = uuid4()
    observation_id = uuid4()
    suppression_id = uuid4()

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.candidates (
                    candidate_id, entity_kind, created_at_utc, correlation_id
                ) VALUES (:candidate_id, 'place', :now_utc, 'schema-test')
                """
            ),
            {"candidate_id": candidate_id, "now_utc": NOW},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.candidate_revisions (
                    candidate_id, revision, cluster_id, resolution_state,
                    snapshot_digest, source_lineage_digest, normalized_payload,
                    recorded_at_utc, correlation_id
                ) VALUES (
                    :candidate_id, 0, :cluster_id, 'review',
                    :snapshot_digest, :lineage_digest, CAST(:payload AS jsonb),
                    :now_utc, 'schema-test'
                )
                """
            ),
            {
                "candidate_id": candidate_id,
                "cluster_id": cluster_id,
                "snapshot_digest": DIGEST_A,
                "lineage_digest": DIGEST_B,
                "payload": '{"displayName":"Studio"}',
                "now_utc": NOW,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO review.review_cases (
                    case_id, candidate_id, candidate_revision, opened_at_utc, correlation_id
                ) VALUES (:case_id, :candidate_id, 0, :now_utc, 'schema-test')
                """
            ),
            {"case_id": case_id, "candidate_id": candidate_id, "now_utc": NOW},
        )
        connection.execute(sa.text("SET CONSTRAINTS ALL DEFERRED"))
        connection.execute(
            sa.text(
                """
                INSERT INTO review.review_case_revisions (
                    case_id, revision, state, reason_codes, current_decision_id,
                    recorded_at_utc, correlation_id
                ) VALUES (
                    :case_id, 1, 'decided', ARRAY['MATCH_REVIEW'], :decision_id,
                    :now_utc, 'schema-test'
                )
                """
            ),
            {"case_id": case_id, "decision_id": decision_id, "now_utc": NOW},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO review.review_decisions (
                    decision_id, case_id, case_revision, outcome, actor_id, rationale,
                    evidence_references, supersedes_decision_id, command_digest,
                    decided_at_utc, correlation_id
                ) VALUES (
                    :decision_id, :case_id, 1, 'accept_candidate', 'reviewer',
                    'Verified against exact evidence.', ARRAY[:evidence], NULL,
                    :command_digest, :now_utc, 'schema-test'
                )
                """
            ),
            {
                "decision_id": decision_id,
                "case_id": case_id,
                "evidence": DIGEST_A,
                "command_digest": DIGEST_B,
                "now_utc": NOW,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO review.manual_observations (
                    observation_id, candidate_id, candidate_revision, field_key,
                    value_text, value_digest, actor_id, reason_code,
                    supersedes_observation_id, command_digest, recorded_at_utc,
                    correlation_id
                ) VALUES (
                    :observation_id, :candidate_id, 0, 'website',
                    'https://example.test', :value_digest, 'reviewer',
                    'MANUAL_VERIFICATION', NULL, :command_digest, :now_utc,
                    'schema-test'
                )
                """
            ),
            {
                "observation_id": observation_id,
                "candidate_id": candidate_id,
                "value_digest": DIGEST_A,
                "command_digest": DIGEST_C,
                "now_utc": NOW,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO review.suppression_revisions (
                    suppression_id, revision, state, target_kind, target_id,
                    suppress_discovery, suppress_normalization, suppress_export,
                    reason_code, actor_id, evidence_reference, starts_at_utc,
                    expires_at_utc, resolved_at_utc, command_digest, correlation_id
                ) VALUES (
                    :suppression_id, 0, 'active', 'candidate', :target_id,
                    TRUE, FALSE, TRUE, 'LEGAL_REVIEW', 'reviewer', :evidence,
                    :now_utc, NULL, NULL, :command_digest, 'schema-test'
                )
                """
            ),
            {
                "suppression_id": suppression_id,
                "target_id": str(candidate_id),
                "evidence": DIGEST_A,
                "command_digest": "sha256:" + "d" * 64,
                "now_utc": NOW,
            },
        )

    with pytest.raises((InternalError, sa.exc.DBAPIError)), engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE candidates.candidate_revisions "
                "SET resolution_state = 'resolved' "
                "WHERE candidate_id = :candidate_id AND revision = 0"
            ),
            {"candidate_id": candidate_id},
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO review.manual_observations (
                    observation_id, candidate_id, candidate_revision, field_key,
                    value_text, value_digest, actor_id, reason_code,
                    supersedes_observation_id, command_digest, recorded_at_utc,
                    correlation_id
                ) VALUES (
                    :observation_id, :candidate_id, 0, 'website',
                    '<script>unsafe</script>', :value_digest, 'reviewer',
                    'MANUAL_VERIFICATION', NULL, :command_digest, :now_utc,
                    'schema-test'
                )
                """
            ),
            {
                "observation_id": uuid4(),
                "candidate_id": candidate_id,
                "value_digest": DIGEST_A,
                "command_digest": "sha256:" + "e" * 64,
                "now_utc": NOW,
            },
        )
''',
    )

    write(
        "docs/specifications/stage8a-candidate-review-foundation.md",
        '''# Stage 8A — candidate and review foundation

## Owners

`review_contracts` owns versioned candidate revision, review decision, manual observation, and suppression contracts. All command identities and evidence digests are explicit.

`review_core` owns pure optimistic-concurrency transitions. It never imports PostgreSQL, FastAPI, workers, connectors, or object-store adapters.

Migration `20260814_0009` owns append-only candidate, quality, and review history. Every foreign key uses restrictive deletion and every history table rejects update/delete mutations.

## Invariants

- Candidate revisions are immutable snapshots with contiguous, unique evidence lineage.
- A stale expected case or suppression revision fails; it is not silently rebased.
- Decisions are immutable. A replacement must explicitly supersede the current decision.
- Manual edits append manual observations and do not mutate source observations or existing candidate revisions.
- Review text is plain text; markup delimiters are rejected in contracts and PostgreSQL constraints.
- Suppression scopes are explicit across discovery, normalization, and export.
- Export eligibility remains owned by deterministic quality evaluation, not by the review frontend.

## Deferred downstream owners

The PostgreSQL command adapter, authenticated Control API, review queue queries, operator UI, and sealed collector export are separate sequential blocks. This foundation does not claim them.
''',
    )
    write(
        ".codex/modules/candidate-review.md",
        '''# Candidate and review module

- Wire owner: `packages/review_contracts`.
- Pure transition owner: `packages/review_core`.
- Durable schema owner: migration `20260814_0009`.
- Review decisions and manual observations are append-only evidence.
- Optimistic concurrency is explicit through expected revisions.
- Suppression scopes are discovery, normalization, and export.
- Runtime PostgreSQL adapter and Control API are downstream owners.
''',
    )

    update_workspace()
    update_architecture()
    update_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
