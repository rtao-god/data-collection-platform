from __future__ import annotations

from datetime import UTC, datetime

from review_contracts import (
    CandidateRevision,
    ManualObservation,
    ManualObservationCommand,
    ReviewCase,
    ReviewDecision,
    ReviewDecisionCommand,
    SuppressionCommand,
    SuppressionRevision,
    canonical_digest,
    deterministic_case_id,
    deterministic_decision_id,
    deterministic_manual_observation_id,
    deterministic_suppression_id,
)


class ReviewTransitionError(ValueError):
    code: str = "REVIEW_TRANSITION_INVALID"


class StaleReviewRevision(ReviewTransitionError):
    code = "REVIEW_REVISION_STALE"


class ReviewDecisionConflict(ReviewTransitionError):
    code = "REVIEW_DECISION_CONFLICT"


class SuppressionTransitionError(ReviewTransitionError):
    code = "SUPPRESSION_TRANSITION_INVALID"


def open_review_case(
    candidate: CandidateRevision,
    *,
    reason_codes: tuple[str, ...],
    now_utc: datetime,
    correlation_id: str,
) -> ReviewCase:
    canonical_reasons = tuple(sorted(set(reason_codes)))
    return ReviewCase(
        case_id=deterministic_case_id(
            candidate.candidate_id,
            candidate.revision,
            canonical_reasons,
        ),
        candidate_id=candidate.candidate_id,
        candidate_revision=candidate.revision,
        revision=0,
        state="open",
        reason_codes=canonical_reasons,
        current_decision_id=None,
        opened_at_utc=_utc(now_utc),
        recorded_at_utc=_utc(now_utc),
        correlation_id=correlation_id,
    )


def decide_review_case(
    case: ReviewCase,
    command: ReviewDecisionCommand,
    *,
    now_utc: datetime,
) -> tuple[ReviewCase, ReviewDecision]:
    if command.case_id != case.case_id:
        raise ReviewDecisionConflict("decision command targets another review case")
    if command.expected_case_revision != case.revision:
        raise StaleReviewRevision(
            f"expected review revision {command.expected_case_revision}, actual {case.revision}"
        )
    if case.state == "open":
        if command.supersedes_decision_id is not None:
            raise ReviewDecisionConflict("an initial decision cannot supersede another decision")
    else:
        if case.current_decision_id is None:
            raise ReviewDecisionConflict("decided case is missing its current decision")
        if command.supersedes_decision_id != case.current_decision_id:
            raise ReviewDecisionConflict(
                "a replacement decision must supersede the current decision"
            )

    next_revision = case.revision + 1
    decision_id = deterministic_decision_id(
        case.case_id,
        next_revision,
        command.command_digest,
    )
    decision = ReviewDecision(
        decision_id=decision_id,
        case_id=case.case_id,
        case_revision=next_revision,
        outcome=command.outcome,
        actor_id=command.actor_id,
        rationale=command.rationale,
        evidence_references=command.evidence_references,
        supersedes_decision_id=command.supersedes_decision_id,
        command_digest=command.command_digest,
        decided_at_utc=_utc(now_utc),
        correlation_id=command.correlation_id,
    )
    revision = ReviewCase(
        case_id=case.case_id,
        candidate_id=case.candidate_id,
        candidate_revision=case.candidate_revision,
        revision=next_revision,
        state="decided",
        reason_codes=case.reason_codes,
        current_decision_id=decision_id,
        opened_at_utc=case.opened_at_utc,
        recorded_at_utc=_utc(now_utc),
        correlation_id=command.correlation_id,
    )
    return revision, decision


def create_manual_observation(
    command: ManualObservationCommand,
    *,
    now_utc: datetime,
) -> ManualObservation:
    value_digest = canonical_digest({"value": command.value_text})
    return ManualObservation(
        observation_id=deterministic_manual_observation_id(
            command.candidate_id,
            command.field_key,
            value_digest,
            command.command_digest,
        ),
        candidate_id=command.candidate_id,
        candidate_revision=command.candidate_revision,
        field_key=command.field_key,
        value_text=command.value_text,
        value_digest=value_digest,
        actor_id=command.actor_id,
        reason_code=command.reason_code,
        supersedes_observation_id=command.supersedes_observation_id,
        command_digest=command.command_digest,
        recorded_at_utc=_utc(now_utc),
        correlation_id=command.correlation_id,
    )


def activate_suppression(
    command: SuppressionCommand,
    *,
    now_utc: datetime,
) -> SuppressionRevision:
    if command.expected_revision is not None:
        raise StaleReviewRevision("initial suppression activation requires no revision")
    suppression_id = deterministic_suppression_id(
        command.target_kind,
        command.target_id,
        command.scopes,
        command.reason_code,
    )
    return SuppressionRevision(
        suppression_id=suppression_id,
        revision=0,
        state="active",
        target_kind=command.target_kind,
        target_id=command.target_id,
        scopes=command.scopes,
        reason_code=command.reason_code,
        actor_id=command.actor_id,
        evidence_reference=command.evidence_reference,
        starts_at_utc=_utc(now_utc),
        expires_at_utc=command.expires_at_utc,
        resolved_at_utc=None,
        command_digest=command.command_digest,
        correlation_id=command.correlation_id,
    )


def resolve_suppression(
    current: SuppressionRevision,
    command: SuppressionCommand,
    *,
    now_utc: datetime,
) -> SuppressionRevision:
    if command.expected_revision != current.revision:
        raise StaleReviewRevision(
            f"expected suppression revision {command.expected_revision}, actual {current.revision}"
        )
    if current.state != "active":
        raise SuppressionTransitionError("only an active suppression can be resolved")
    identity = deterministic_suppression_id(
        command.target_kind,
        command.target_id,
        command.scopes,
        command.reason_code,
    )
    if identity != current.suppression_id:
        raise SuppressionTransitionError("suppression identity cannot change on resolution")
    if command.expires_at_utc != current.expires_at_utc:
        raise SuppressionTransitionError("suppression expiry cannot change on resolution")
    return SuppressionRevision(
        suppression_id=current.suppression_id,
        revision=current.revision + 1,
        state="resolved",
        target_kind=current.target_kind,
        target_id=current.target_id,
        scopes=current.scopes,
        reason_code=current.reason_code,
        actor_id=command.actor_id,
        evidence_reference=command.evidence_reference,
        starts_at_utc=current.starts_at_utc,
        expires_at_utc=current.expires_at_utc,
        resolved_at_utc=_utc(now_utc),
        command_digest=command.command_digest,
        correlation_id=command.correlation_id,
    )


def suppression_applies(
    suppression: SuppressionRevision,
    *,
    scope: str,
    at_utc: datetime,
) -> bool:
    current_time = _utc(at_utc)
    return (
        suppression.state == "active"
        and scope in suppression.scopes
        and suppression.starts_at_utc <= current_time
        and (suppression.expires_at_utc is None or current_time < suppression.expires_at_utc)
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
