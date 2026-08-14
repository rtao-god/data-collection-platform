from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def main() -> int:
    app_test = ROOT / "apps/control_api/tests/test_app.py"
    text = app_test.read_text(encoding="utf-8")
    text = text.replace(
        '    assert "token" not in str(response.json()).lower()\n',
        '    assert TOKEN not in str(response.json())\n',
    )
    app_test.write_text(text, encoding="utf-8")

    main_path = ROOT / "apps/control_api/src/control_api/main.py"
    text = main_path.read_text(encoding="utf-8")
    if "def main() -> None:" not in text:
        text = text.replace(
            "\n\napp = create_runtime_app()\n",
            '''

def main() -> None:
    import uvicorn

    uvicorn.run("control_api.main:app", host="0.0.0.0", port=8080)


app = create_runtime_app()
''',
        )
        main_path.write_text(text, encoding="utf-8")

    write(
        "database/tests/test_postgres_review_repository.py",
        '''from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from collection_infrastructure.postgres import PostgresReviewRepository
from review_application import ReviewConflict, ReviewService, ReviewerPrincipal
from review_contracts import (
    CandidateEvidence,
    CandidateRevision,
    candidate_snapshot_digest,
)
from review_core import open_review_case
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def database_url() -> str:
    value = os.getenv("COLLECTOR_DATABASE_URL", "").strip()
    if not value:
        pytest.fail("COLLECTOR_DATABASE_URL is required")
    return value


def seed_review_case(engine: sa.Engine):
    candidate_id = uuid4()
    cluster_id = uuid4()
    evidence = (
        CandidateEvidence(
            position=0,
            evidence_kind="source_observation",
            evidence_digest=DIGEST_A,
        ),
    )
    snapshot_digest = candidate_snapshot_digest(
        candidate_id=candidate_id,
        revision=0,
        entity_kind="place",
        cluster_id=cluster_id,
        resolution_state="review",
        normalized_payload={"displayName": "Studio"},
        evidence=evidence,
        source_lineage_digest=DIGEST_B,
    )
    candidate = CandidateRevision(
        candidate_id=candidate_id,
        revision=0,
        entity_kind="place",
        cluster_id=cluster_id,
        resolution_state="review",
        snapshot_digest=snapshot_digest,
        source_lineage_digest=DIGEST_B,
        normalized_payload={"displayName": "Studio"},
        evidence=evidence,
        recorded_at_utc=NOW,
        correlation_id="repository-test",
    )
    case = open_review_case(
        candidate,
        reason_codes=("MATCH_REVIEW",),
        now_utc=NOW,
        correlation_id="repository-test",
    )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.candidates (
                    candidate_id, entity_kind, created_at_utc, correlation_id
                ) VALUES (:candidate_id, :entity_kind, :created_at_utc, :correlation_id)
                """
            ),
            {
                "candidate_id": candidate.candidate_id,
                "entity_kind": candidate.entity_kind,
                "created_at_utc": candidate.recorded_at_utc,
                "correlation_id": candidate.correlation_id,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.candidate_revisions (
                    candidate_id, revision, cluster_id, resolution_state,
                    snapshot_digest, source_lineage_digest, normalized_payload,
                    recorded_at_utc, correlation_id
                ) VALUES (
                    :candidate_id, :revision, :cluster_id, :resolution_state,
                    :snapshot_digest, :source_lineage_digest,
                    CAST(:normalized_payload AS jsonb), :recorded_at_utc, :correlation_id
                )
                """
            ),
            {
                "candidate_id": candidate.candidate_id,
                "revision": candidate.revision,
                "cluster_id": candidate.cluster_id,
                "resolution_state": candidate.resolution_state,
                "snapshot_digest": candidate.snapshot_digest,
                "source_lineage_digest": candidate.source_lineage_digest,
                "normalized_payload": '{"displayName":"Studio"}',
                "recorded_at_utc": candidate.recorded_at_utc,
                "correlation_id": candidate.correlation_id,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.candidate_revision_evidence (
                    candidate_id, candidate_revision, position,
                    evidence_kind, evidence_digest
                ) VALUES (
                    :candidate_id, 0, 0, 'source_observation', :evidence_digest
                )
                """
            ),
            {"candidate_id": candidate.candidate_id, "evidence_digest": DIGEST_A},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO review.review_cases (
                    case_id, candidate_id, candidate_revision, opened_at_utc, correlation_id
                ) VALUES (
                    :case_id, :candidate_id, :candidate_revision,
                    :opened_at_utc, :correlation_id
                )
                """
            ),
            {
                "case_id": case.case_id,
                "candidate_id": case.candidate_id,
                "candidate_revision": case.candidate_revision,
                "opened_at_utc": case.opened_at_utc,
                "correlation_id": case.correlation_id,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO review.review_case_revisions (
                    case_id, revision, state, reason_codes, current_decision_id,
                    recorded_at_utc, correlation_id
                ) VALUES (
                    :case_id, 0, 'open', :reason_codes, NULL,
                    :recorded_at_utc, :correlation_id
                )
                """
            ).bindparams(sa.bindparam("reason_codes", type_=sa.ARRAY(sa.Text()))),
            {
                "case_id": case.case_id,
                "reason_codes": list(case.reason_codes),
                "recorded_at_utc": case.recorded_at_utc,
                "correlation_id": case.correlation_id,
            },
        )
    return candidate, case


def principal() -> ReviewerPrincipal:
    return ReviewerPrincipal(
        actor_id="reviewer-1",
        permissions=frozenset(
            {"review:read", "review:decide", "review:observe", "review:suppress"}
        ),
    )


def test_review_repository_is_atomic_idempotent_and_revision_guarded() -> None:
    engine = sa.create_engine(database_url(), poolclass=NullPool)
    candidate, case = seed_review_case(engine)
    service = ReviewService(PostgresReviewRepository(engine), clock=lambda: NOW)

    queue = service.list_cases(
        principal(),
        state="open",
        limit=20,
        cursor=None,
    )
    assert tuple(item.case_id for item in queue.items) == (case.case_id,)
    detail = service.get_case(principal(), case.case_id)
    assert detail.candidate == candidate
    assert detail.case.state == "open"

    values = {
        "case_id": case.case_id,
        "expected_revision": 0,
        "outcome": "accept_candidate",
        "rationale": "Verified against exact evidence.",
        "evidence_references": (DIGEST_A,),
        "supersedes_decision_id": None,
        "correlation_id": "decision-test",
    }
    first = service.submit_decision(principal(), **values)
    replay = service.submit_decision(principal(), **values)
    assert replay == first
    assert first[0].revision == 1

    with pytest.raises(ReviewConflict):
        service.submit_decision(
            principal(),
            **{
                **values,
                "rationale": "Different command against a stale revision.",
            },
        )

    observation_values = {
        "candidate_id": candidate.candidate_id,
        "candidate_revision": candidate.revision,
        "field_key": "website",
        "value_text": "https://example.test",
        "reason_code": "MANUAL_VERIFICATION",
        "supersedes_observation_id": None,
        "correlation_id": "observation-test",
    }
    observation = service.add_manual_observation(principal(), **observation_values)
    observation_replay = service.add_manual_observation(
        principal(),
        **observation_values,
    )
    assert observation_replay == observation

    suppression_values = {
        "target_kind": "candidate",
        "target_id": str(candidate.candidate_id),
        "scopes": ("discovery", "export"),
        "reason_code": "LEGAL_REVIEW",
        "evidence_reference": DIGEST_A,
        "expires_at_utc": None,
        "correlation_id": "suppression-test",
    }
    active = service.activate_suppression(principal(), **suppression_values)
    active_replay = service.activate_suppression(principal(), **suppression_values)
    assert active_replay == active
    resolved = service.resolve_suppression(
        principal(),
        suppression_id=active.suppression_id,
        expected_revision=0,
        evidence_reference=DIGEST_C,
        correlation_id="suppression-resolve-test",
    )
    resolved_replay = service.resolve_suppression(
        principal(),
        suppression_id=active.suppression_id,
        expected_revision=0,
        evidence_reference=DIGEST_C,
        correlation_id="suppression-resolve-test",
    )
    assert resolved_replay == resolved
    assert resolved.state == "resolved"

    with pytest.raises(ReviewConflict):
        service.resolve_suppression(
            principal(),
            suppression_id=active.suppression_id,
            expected_revision=0,
            evidence_reference=DIGEST_B,
            correlation_id="suppression-stale-test",
        )

    refreshed = service.get_case(principal(), case.case_id)
    assert refreshed.case.revision == 1
    assert refreshed.decisions == (first[1],)
    assert refreshed.manual_observations == (observation,)
    assert refreshed.active_suppressions == ()
''',
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
