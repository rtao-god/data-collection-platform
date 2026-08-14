from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from review_application.models import ReviewCaseDetail, ReviewQueueCursor, ReviewQueuePage
from review_contracts import (
    ManualObservation,
    ManualObservationCommand,
    ReviewCase,
    ReviewDecision,
    ReviewDecisionCommand,
    SuppressionCommand,
    SuppressionRevision,
)


class ReviewRepository(Protocol):
    def list_cases(
        self,
        *,
        state: str,
        limit: int,
        cursor: ReviewQueueCursor | None,
    ) -> ReviewQueuePage: ...

    def get_case(self, case_id: UUID) -> ReviewCaseDetail: ...

    def submit_decision(
        self,
        command: ReviewDecisionCommand,
        *,
        now_utc: datetime,
    ) -> tuple[ReviewCase, ReviewDecision]: ...

    def add_manual_observation(
        self,
        command: ManualObservationCommand,
        *,
        now_utc: datetime,
    ) -> ManualObservation: ...

    def get_suppression(self, suppression_id: UUID) -> SuppressionRevision: ...

    def activate_suppression(
        self,
        command: SuppressionCommand,
        *,
        now_utc: datetime,
    ) -> SuppressionRevision: ...

    def resolve_suppression(
        self,
        command: SuppressionCommand,
        *,
        now_utc: datetime,
    ) -> SuppressionRevision: ...


Clock = Callable[[], datetime]
