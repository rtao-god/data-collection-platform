from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from review_contracts import (
    CandidateEvidence,
    CandidateRevision,
    ManualObservationCommand,
    ReviewDecisionCommand,
    SuppressionCommand,
    candidate_snapshot_digest,
    manual_observation_command_digest,
    review_decision_command_digest,
    suppression_command_digest,
)
from review_core import (
    ReviewDecisionConflict,
    StaleReviewRevision,
    SuppressionTransitionError,
    activate_suppression,
    create_manual_observation,
    decide_review_case,
    open_review_case,
    resolve_suppression,
    suppression_applies,
)

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def candidate() -> CandidateRevision:
    candidate_id = uuid4()
    cluster_id = uuid4()
    evidence = (
        CandidateEvidence(
            position=0,
            evidence_kind="source_observation",
            evidence_digest=DIGEST_A,
        ),
    )
    digest = candidate_snapshot_digest(
        candidate_id=candidate_id,
        revision=0,
        entity_kind="place",
        cluster_id=cluster_id,
        resolution_state="review",
        normalized_payload={"displayName": "Studio"},
        evidence=evidence,
        source_lineage_digest=DIGEST_B,
    )
    return CandidateRevision(
        candidate_id=candidate_id,
        revision=0,
        entity_kind="place",
        cluster_id=cluster_id,
        resolution_state="review",
        snapshot_digest=digest,
        source_lineage_digest=DIGEST_B,
        normalized_payload={"displayName": "Studio"},
        evidence=evidence,
        recorded_at_utc=NOW,
        correlation_id="candidate-test",
    )


def decision_command(
    case_id: object,
    *,
    expected_revision: int,
    supersedes: object | None = None,
) -> ReviewDecisionCommand:
    digest = review_decision_command_digest(
        case_id=case_id,  # type: ignore[arg-type]
        expected_case_revision=expected_revision,
        outcome="accept_candidate",
        actor_id="reviewer",
        rationale="Verified against exact evidence.",
        evidence_references=(DIGEST_A,),
        supersedes_decision_id=supersedes,  # type: ignore[arg-type]
    )
    return ReviewDecisionCommand(
        case_id=case_id,  # type: ignore[arg-type]
        expected_case_revision=expected_revision,
        outcome="accept_candidate",
        actor_id="reviewer",
        rationale="Verified against exact evidence.",
        evidence_references=(DIGEST_A,),
        supersedes_decision_id=supersedes,  # type: ignore[arg-type]
        command_digest=digest,
        correlation_id="review-test",
    )


def suppression_command(
    *,
    expected_revision: int | None,
    evidence: str = DIGEST_A,
) -> SuppressionCommand:
    values = {
        "target_kind": "candidate",
        "target_id": "candidate-1",
        "scopes": ("discovery", "export"),
        "reason_code": "LEGAL_REVIEW",
        "actor_id": "reviewer",
        "evidence_reference": evidence,
        "expected_revision": expected_revision,
        "expires_at_utc": NOW + timedelta(days=1),
    }
    digest = suppression_command_digest(**values)
    return SuppressionCommand(
        **values,
        command_digest=digest,
        correlation_id="suppression-test",
    )


def test_open_case_has_deterministic_identity() -> None:
    value = candidate()
    first = open_review_case(
        value,
        reason_codes=("MATCH_REVIEW",),
        now_utc=NOW,
        correlation_id="review-test",
    )
    second = open_review_case(
        value,
        reason_codes=("MATCH_REVIEW",),
        now_utc=NOW,
        correlation_id="review-test",
    )
    assert first == second


def test_decision_advances_revision_and_is_immutable_value() -> None:
    case = open_review_case(
        candidate(),
        reason_codes=("MATCH_REVIEW",),
        now_utc=NOW,
        correlation_id="review-test",
    )
    revision, decision = decide_review_case(
        case,
        decision_command(case.case_id, expected_revision=0),
        now_utc=NOW + timedelta(minutes=1),
    )
    assert revision.revision == 1
    assert revision.current_decision_id == decision.decision_id
    assert decision.case_revision == 1


def test_stale_review_revision_is_rejected() -> None:
    case = open_review_case(
        candidate(),
        reason_codes=("MATCH_REVIEW",),
        now_utc=NOW,
        correlation_id="review-test",
    )
    with pytest.raises(StaleReviewRevision):
        decide_review_case(
            case,
            decision_command(case.case_id, expected_revision=1),
            now_utc=NOW + timedelta(minutes=1),
        )


def test_replacement_decision_must_supersede_current_decision() -> None:
    case = open_review_case(
        candidate(),
        reason_codes=("MATCH_REVIEW",),
        now_utc=NOW,
        correlation_id="review-test",
    )
    decided, decision = decide_review_case(
        case,
        decision_command(case.case_id, expected_revision=0),
        now_utc=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ReviewDecisionConflict):
        decide_review_case(
            decided,
            decision_command(decided.case_id, expected_revision=1),
            now_utc=NOW + timedelta(minutes=2),
        )
    replacement, new_decision = decide_review_case(
        decided,
        decision_command(
            decided.case_id,
            expected_revision=1,
            supersedes=decision.decision_id,
        ),
        now_utc=NOW + timedelta(minutes=2),
    )
    assert replacement.revision == 2
    assert new_decision.supersedes_decision_id == decision.decision_id


def test_manual_observation_is_new_evidence_not_candidate_mutation() -> None:
    value = candidate()
    digest = manual_observation_command_digest(
        candidate_id=value.candidate_id,
        candidate_revision=value.revision,
        field_key="website",
        value_text="https://example.test",
        actor_id="reviewer",
        reason_code="MANUAL_VERIFICATION",
        supersedes_observation_id=None,
    )
    observation = create_manual_observation(
        ManualObservationCommand(
            candidate_id=value.candidate_id,
            candidate_revision=value.revision,
            field_key="website",
            value_text="https://example.test",
            actor_id="reviewer",
            reason_code="MANUAL_VERIFICATION",
            command_digest=digest,
            correlation_id="manual-observation-test",
        ),
        now_utc=NOW,
    )
    assert value.normalized_payload == {"displayName": "Studio"}
    assert observation.candidate_revision == value.revision
    assert observation.value_digest.startswith("sha256:")


def test_suppression_applies_only_to_owned_scope_and_time() -> None:
    active = activate_suppression(suppression_command(expected_revision=None), now_utc=NOW)
    assert suppression_applies(active, scope="export", at_utc=NOW) is True
    assert suppression_applies(active, scope="normalization", at_utc=NOW) is False
    assert (
        suppression_applies(
            active,
            scope="export",
            at_utc=NOW + timedelta(days=2),
        )
        is False
    )


def test_suppression_resolution_rejects_expiry_change() -> None:
    active = activate_suppression(suppression_command(expected_revision=None), now_utc=NOW)
    values = {
        "target_kind": "candidate",
        "target_id": "candidate-1",
        "scopes": ("discovery", "export"),
        "reason_code": "LEGAL_REVIEW",
        "actor_id": "reviewer",
        "evidence_reference": DIGEST_B,
        "expected_revision": 0,
        "expires_at_utc": NOW + timedelta(days=2),
    }
    command = SuppressionCommand(
        **values,
        command_digest=suppression_command_digest(**values),
        correlation_id="suppression-test",
    )
    with pytest.raises(SuppressionTransitionError, match="expiry cannot change"):
        resolve_suppression(active, command, now_utc=NOW + timedelta(minutes=1))


def test_suppression_resolution_is_revision_guarded() -> None:
    active = activate_suppression(suppression_command(expected_revision=None), now_utc=NOW)
    with pytest.raises(StaleReviewRevision):
        resolve_suppression(
            active,
            suppression_command(expected_revision=1, evidence=DIGEST_B),
            now_utc=NOW + timedelta(minutes=1),
        )
    resolved = resolve_suppression(
        active,
        suppression_command(expected_revision=0, evidence=DIGEST_B),
        now_utc=NOW + timedelta(minutes=1),
    )
    assert resolved.state == "resolved"
    assert resolved.revision == 1
