from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from review_application import (
    ReviewerPrincipal,
    ReviewForbidden,
    ReviewInputInvalid,
    ReviewService,
)
from review_contracts import SuppressionRevision, deterministic_suppression_id

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
    suppression_id = deterministic_suppression_id(
        "candidate",
        "candidate-1",
        ("discovery", "export"),
        "LEGAL_REVIEW",
    )
    service.resolve_suppression(
        principal("review:suppress"),
        suppression_id=suppression_id,
        expected_revision=0,
        evidence_reference=DIGEST,
        correlation_id="suppression-test",
    )
    assert repository.suppression_command.target_id == "candidate-1"
    assert repository.suppression_command.expected_revision == 0
