from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from review_application.errors import ReviewForbidden, ReviewInputInvalid
from review_application.models import (
    Permission,
    ReviewCaseDetail,
    ReviewerPrincipal,
    ReviewQueueCursor,
    ReviewQueuePage,
)
from review_application.ports import Clock, ReviewRepository
from review_contracts import (
    ManualObservation,
    ManualObservationCommand,
    ReviewCase,
    ReviewDecision,
    ReviewDecisionCommand,
    ReviewOutcome,
    SuppressionCommand,
    SuppressionRevision,
    SuppressionScope,
    SuppressionTargetKind,
    manual_observation_command_digest,
    review_decision_command_digest,
    suppression_command_digest,
)


class ReviewService:
    def __init__(self, repository: ReviewRepository, *, clock: Clock) -> None:
        self._repository = repository
        self._clock = clock

    def list_cases(
        self,
        principal: ReviewerPrincipal,
        *,
        state: str,
        limit: int,
        cursor: ReviewQueueCursor | None,
    ) -> ReviewQueuePage:
        _require(principal, "review:read")
        if state not in {"open", "decided"}:
            raise ReviewInputInvalid(
                "Review state must be open or decided.",
                "Use a supported review queue state.",
            )
        if not 1 <= limit <= 100:
            raise ReviewInputInvalid(
                "Review page size must be between 1 and 100.",
                "Use a supported page size.",
            )
        return self._repository.list_cases(state=state, limit=limit, cursor=cursor)

    def get_case(
        self,
        principal: ReviewerPrincipal,
        case_id: UUID,
    ) -> ReviewCaseDetail:
        _require(principal, "review:read")
        return self._repository.get_case(case_id)

    def submit_decision(
        self,
        principal: ReviewerPrincipal,
        *,
        case_id: UUID,
        expected_revision: int,
        outcome: ReviewOutcome,
        rationale: str,
        evidence_references: tuple[str, ...],
        supersedes_decision_id: UUID | None,
        correlation_id: str,
    ) -> tuple[ReviewCase, ReviewDecision]:
        _require(principal, "review:decide")
        digest = review_decision_command_digest(
            case_id=case_id,
            expected_case_revision=expected_revision,
            outcome=outcome,
            actor_id=principal.actor_id,
            rationale=rationale,
            evidence_references=evidence_references,
            supersedes_decision_id=supersedes_decision_id,
        )
        command = ReviewDecisionCommand(
            case_id=case_id,
            expected_case_revision=expected_revision,
            outcome=outcome,
            actor_id=principal.actor_id,
            rationale=rationale,
            evidence_references=evidence_references,
            supersedes_decision_id=supersedes_decision_id,
            command_digest=digest,
            correlation_id=correlation_id,
        )
        return self._repository.submit_decision(command, now_utc=self._now())

    def add_manual_observation(
        self,
        principal: ReviewerPrincipal,
        *,
        candidate_id: UUID,
        candidate_revision: int,
        field_key: str,
        value_text: str,
        reason_code: str,
        supersedes_observation_id: UUID | None,
        correlation_id: str,
    ) -> ManualObservation:
        _require(principal, "review:observe")
        digest = manual_observation_command_digest(
            candidate_id=candidate_id,
            candidate_revision=candidate_revision,
            field_key=field_key,
            value_text=value_text,
            actor_id=principal.actor_id,
            reason_code=reason_code,
            supersedes_observation_id=supersedes_observation_id,
        )
        command = ManualObservationCommand(
            candidate_id=candidate_id,
            candidate_revision=candidate_revision,
            field_key=field_key,
            value_text=value_text,
            actor_id=principal.actor_id,
            reason_code=reason_code,
            supersedes_observation_id=supersedes_observation_id,
            command_digest=digest,
            correlation_id=correlation_id,
        )
        return self._repository.add_manual_observation(command, now_utc=self._now())

    def activate_suppression(
        self,
        principal: ReviewerPrincipal,
        *,
        target_kind: SuppressionTargetKind,
        target_id: str,
        scopes: tuple[SuppressionScope, ...],
        reason_code: str,
        evidence_reference: str,
        expires_at_utc: datetime | None,
        correlation_id: str,
    ) -> SuppressionRevision:
        _require(principal, "review:suppress")
        digest = suppression_command_digest(
            target_kind=target_kind,
            target_id=target_id,
            scopes=scopes,
            reason_code=reason_code,
            actor_id=principal.actor_id,
            evidence_reference=evidence_reference,
            expected_revision=None,
            expires_at_utc=expires_at_utc,
        )
        command = SuppressionCommand(
            target_kind=target_kind,
            target_id=target_id,
            scopes=scopes,
            reason_code=reason_code,
            actor_id=principal.actor_id,
            evidence_reference=evidence_reference,
            expected_revision=None,
            expires_at_utc=expires_at_utc,
            command_digest=digest,
            correlation_id=correlation_id,
        )
        return self._repository.activate_suppression(command, now_utc=self._now())

    def resolve_suppression(
        self,
        principal: ReviewerPrincipal,
        *,
        suppression_id: UUID,
        expected_revision: int,
        evidence_reference: str,
        correlation_id: str,
    ) -> SuppressionRevision:
        _require(principal, "review:suppress")
        current = self._repository.get_suppression(suppression_id)
        digest = suppression_command_digest(
            target_kind=current.target_kind,
            target_id=current.target_id,
            scopes=current.scopes,
            reason_code=current.reason_code,
            actor_id=principal.actor_id,
            evidence_reference=evidence_reference,
            expected_revision=expected_revision,
            expires_at_utc=current.expires_at_utc,
        )
        command = SuppressionCommand(
            target_kind=current.target_kind,
            target_id=current.target_id,
            scopes=current.scopes,
            reason_code=current.reason_code,
            actor_id=principal.actor_id,
            evidence_reference=evidence_reference,
            expected_revision=expected_revision,
            expires_at_utc=current.expires_at_utc,
            command_digest=digest,
            correlation_id=correlation_id,
        )
        return self._repository.resolve_suppression(command, now_utc=self._now())

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("review application clock must be timezone-aware")
        return value.astimezone(UTC)


def _require(principal: ReviewerPrincipal, permission: Permission) -> None:
    if permission not in principal.permissions:
        raise ReviewForbidden(
            f"Reviewer {principal.actor_id} lacks {permission}.",
            "Use a principal with the required review permission.",
        )
