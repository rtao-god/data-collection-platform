from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from review_contracts import (
    CandidateRevision,
    ManualObservation,
    QualityRecord,
    ReviewCase,
    ReviewDecision,
    SuppressionRevision,
)

Permission = Literal[
    "review:read",
    "review:decide",
    "review:observe",
    "review:suppress",
]


@dataclass(frozen=True, slots=True)
class ReviewerPrincipal:
    actor_id: str
    permissions: frozenset[Permission]


@dataclass(frozen=True, slots=True)
class ReviewQueueCursor:
    recorded_at_utc: datetime
    case_id: UUID


@dataclass(frozen=True, slots=True)
class ReviewCaseSummary:
    case_id: UUID
    candidate_id: UUID
    candidate_revision: int
    revision: int
    state: str
    reason_codes: tuple[str, ...]
    current_decision_id: UUID | None
    recorded_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ReviewQueuePage:
    items: tuple[ReviewCaseSummary, ...]
    next_cursor: ReviewQueueCursor | None


@dataclass(frozen=True, slots=True)
class ReviewCaseDetail:
    case: ReviewCase
    candidate: CandidateRevision
    quality: QualityRecord | None
    decisions: tuple[ReviewDecision, ...]
    manual_observations: tuple[ManualObservation, ...]
    active_suppressions: tuple[SuppressionRevision, ...]
