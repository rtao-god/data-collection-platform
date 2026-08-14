from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
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
                    'Verified against exact evidence.', ARRAY[CAST(:evidence AS text)], NULL,
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

    with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
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
