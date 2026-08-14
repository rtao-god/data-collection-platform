from __future__ import annotations

import re
from pathlib import Path

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def clone_project(
    source: str,
    target: str,
    *,
    source_distribution: str,
    target_distribution: str,
    source_module: str,
    target_module: str,
    dependencies: tuple[str, ...],
) -> None:
    text = (ROOT / source / "pyproject.toml").read_text(encoding="utf-8")
    text = text.replace(source_distribution, target_distribution)
    text = text.replace(source_module, target_module)
    rendered = "dependencies = [\n" + "".join(
        f'  "{dependency}",\n' for dependency in dependencies
    ) + "]"
    text, count = re.subn(r"(?ms)^dependencies = \[.*?^\]", rendered, text, count=1)
    if count != 1:
        text, count = re.subn(
            r"(?m)^dependencies = \[[^\n]*\]$", rendered, text, count=1
        )
    if count != 1:
        raise RuntimeError(f"{source}: dependencies declaration was not found")
    target_path = ROOT / target / "pyproject.toml"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(text, encoding="utf-8")


def main() -> int:
    if not (ROOT / "docs/proofs/stage8a-candidate-review-ci.md").exists():
        raise RuntimeError("Stage 8A exact-head proof is required before Stage 8B")

    clone_project(
        "packages/manual_import_core",
        "packages/review_application",
        source_distribution="manual-import-core",
        target_distribution="review-application",
        source_module="manual_import_core",
        target_module="review_application",
        dependencies=("review-contracts", "review-core"),
    )

    write(
        "packages/review_application/src/review_application/models.py",
        '''from __future__ import annotations

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
''',
    )

    write(
        "packages/review_application/src/review_application/errors.py",
        '''from __future__ import annotations


class ReviewApplicationError(RuntimeError):
    code = "REVIEW_OPERATION_FAILED"
    owner = "ReviewApplication"
    status_code = 500

    def __init__(self, message: str, required_action: str) -> None:
        super().__init__(message)
        self.message = message
        self.required_action = required_action


class ReviewNotFound(ReviewApplicationError):
    code = "REVIEW_RESOURCE_NOT_FOUND"
    status_code = 404


class ReviewConflict(ReviewApplicationError):
    code = "REVIEW_CONFLICT"
    status_code = 409


class ReviewForbidden(ReviewApplicationError):
    code = "REVIEW_PERMISSION_DENIED"
    status_code = 403


class ReviewInputInvalid(ReviewApplicationError):
    code = "REVIEW_INPUT_INVALID"
    status_code = 422
''',
    )

    write(
        "packages/review_application/src/review_application/ports.py",
        '''from __future__ import annotations

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
''',
    )

    write(
        "packages/review_application/src/review_application/cursors.py",
        '''from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import UUID

from review_application.errors import ReviewInputInvalid
from review_application.models import ReviewQueueCursor


def encode_cursor(cursor: ReviewQueueCursor | None) -> str | None:
    if cursor is None:
        return None
    payload = json.dumps(
        {
            "caseId": str(cursor.case_id),
            "recordedAtUtc": cursor.recorded_at_utc.isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str | None) -> ReviewQueueCursor | None:
    if value is None:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        if set(payload) != {"caseId", "recordedAtUtc"}:
            raise ValueError("unexpected cursor shape")
        return ReviewQueueCursor(
            recorded_at_utc=datetime.fromisoformat(payload["recordedAtUtc"]),
            case_id=UUID(payload["caseId"]),
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ReviewInputInvalid(
            "The review cursor is malformed.",
            "Restart pagination from the first review page.",
        ) from exc
''',
    )

    write(
        "packages/review_application/src/review_application/service.py",
        '''from __future__ import annotations

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
    SuppressionCommand,
    SuppressionRevision,
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
        outcome: str,
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
            outcome=outcome,  # type: ignore[arg-type]
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
        target_kind: str,
        target_id: str,
        scopes: tuple[str, ...],
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
            target_kind=target_kind,  # type: ignore[arg-type]
            target_id=target_id,
            scopes=scopes,  # type: ignore[arg-type]
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
''',
    )

    write(
        "packages/review_application/src/review_application/__init__.py",
        '''from review_application.cursors import decode_cursor, encode_cursor
from review_application.errors import (
    ReviewApplicationError,
    ReviewConflict,
    ReviewForbidden,
    ReviewInputInvalid,
    ReviewNotFound,
)
from review_application.models import (
    ReviewCaseDetail,
    ReviewCaseSummary,
    ReviewerPrincipal,
    ReviewQueueCursor,
    ReviewQueuePage,
)
from review_application.ports import ReviewRepository
from review_application.service import ReviewService

__all__ = [
    "ReviewApplicationError",
    "ReviewCaseDetail",
    "ReviewCaseSummary",
    "ReviewConflict",
    "ReviewForbidden",
    "ReviewInputInvalid",
    "ReviewNotFound",
    "ReviewQueueCursor",
    "ReviewQueuePage",
    "ReviewRepository",
    "ReviewService",
    "ReviewerPrincipal",
    "decode_cursor",
    "encode_cursor",
]
''',
    )

    write(
        "packages/review_application/tests/test_service.py",
        '''from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from review_application import (
    ReviewForbidden,
    ReviewInputInvalid,
    ReviewService,
    ReviewerPrincipal,
)
from review_contracts import ManualObservation, ReviewCase, ReviewDecision, SuppressionRevision

NOW = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


class Repository:
    def __init__(self) -> None:
        self.decision_command = None
        self.observation_command = None
        self.suppression_command = None

    def list_cases(self, *, state, limit, cursor):
        return (state, limit, cursor)

    def get_case(self, case_id):
        return case_id

    def submit_decision(self, command, *, now_utc):
        self.decision_command = command
        return (object(), object())

    def add_manual_observation(self, command, *, now_utc):
        self.observation_command = command
        return object()

    def get_suppression(self, suppression_id):
        return SuppressionRevision(
            suppression_id=suppression_id,
            revision=0,
            state="active",
            target_kind="candidate",
            target_id="candidate-1",
            scopes=("discovery", "export"),
            reason_code="LEGAL_REVIEW",
            actor_id="reviewer",
            evidence_reference=DIGEST,
            starts_at_utc=NOW,
            expires_at_utc=None,
            resolved_at_utc=None,
            command_digest=DIGEST,
            correlation_id="test",
        )

    def activate_suppression(self, command, *, now_utc):
        self.suppression_command = command
        return object()

    def resolve_suppression(self, command, *, now_utc):
        self.suppression_command = command
        return object()


def principal(*permissions: str) -> ReviewerPrincipal:
    return ReviewerPrincipal(
        actor_id="reviewer-1",
        permissions=frozenset(permissions),  # type: ignore[arg-type]
    )


def test_actor_is_derived_from_authenticated_principal() -> None:
    repository = Repository()
    service = ReviewService(repository, clock=lambda: NOW)
    service.submit_decision(
        principal("review:decide"),
        case_id=uuid4(),
        expected_revision=0,
        outcome="accept_candidate",
        rationale="Verified.",
        evidence_references=(DIGEST,),
        supersedes_decision_id=None,
        correlation_id="decision-test",
    )
    assert repository.decision_command.actor_id == "reviewer-1"
    assert repository.decision_command.command_digest.startswith("sha256:")


def test_permission_is_fail_closed() -> None:
    service = ReviewService(Repository(), clock=lambda: NOW)
    with pytest.raises(ReviewForbidden):
        service.submit_decision(
            principal("review:read"),
            case_id=uuid4(),
            expected_revision=0,
            outcome="accept_candidate",
            rationale="Verified.",
            evidence_references=(DIGEST,),
            supersedes_decision_id=None,
            correlation_id="decision-test",
        )


def test_queue_input_is_validated() -> None:
    service = ReviewService(Repository(), clock=lambda: NOW)
    with pytest.raises(ReviewInputInvalid):
        service.list_cases(
            principal("review:read"),
            state="invalid",
            limit=20,
            cursor=None,
        )


def test_manual_observation_command_has_principal_actor() -> None:
    repository = Repository()
    service = ReviewService(repository, clock=lambda: NOW)
    service.add_manual_observation(
        principal("review:observe"),
        candidate_id=uuid4(),
        candidate_revision=1,
        field_key="website",
        value_text="https://example.test",
        reason_code="MANUAL_VERIFICATION",
        supersedes_observation_id=None,
        correlation_id="observation-test",
    )
    assert repository.observation_command.actor_id == "reviewer-1"


def test_resolve_suppression_preserves_identity() -> None:
    repository = Repository()
    service = ReviewService(repository, clock=lambda: NOW)
    suppression_id = uuid4()
    service.resolve_suppression(
        principal("review:suppress"),
        suppression_id=suppression_id,
        expected_revision=0,
        evidence_reference=DIGEST,
        correlation_id="suppression-test",
    )
    assert repository.suppression_command.target_id == "candidate-1"
    assert repository.suppression_command.expected_revision == 0
''',
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
