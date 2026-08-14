from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from control_api.app import create_app
from control_api.auth import TokenAuthenticator
from fastapi.testclient import TestClient

from review_application import ReviewQueuePage
from review_contracts import (
    ReviewCase,
    ReviewDecision,
    deterministic_case_id,
    deterministic_decision_id,
    review_decision_command_digest,
)

TOKEN = "a" * 40
NOW = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


class Service:
    def __init__(self) -> None:
        self.principal = None
        self.decision_call = None
        self.candidate_id = uuid4()
        self.case_id = deterministic_case_id(
            self.candidate_id,
            0,
            ("MATCH_REVIEW",),
        )

    def list_cases(self, principal, *, state, limit, cursor):
        self.principal = principal
        return ReviewQueuePage(items=(), next_cursor=None)

    def get_case(self, principal, case_id):
        raise AssertionError("not used")

    def submit_decision(self, principal, **values):
        self.principal = principal
        self.decision_call = values
        case_id = values["case_id"]
        assert case_id == self.case_id
        digest = review_decision_command_digest(
            case_id=case_id,
            expected_case_revision=values["expected_revision"],
            outcome=values["outcome"],
            actor_id=principal.actor_id,
            rationale=values["rationale"],
            evidence_references=values["evidence_references"],
            supersedes_decision_id=values["supersedes_decision_id"],
        )
        decision_id = deterministic_decision_id(case_id, 1, digest)
        case = ReviewCase(
            case_id=case_id,
            candidate_id=self.candidate_id,
            candidate_revision=0,
            revision=1,
            state="decided",
            reason_codes=("MATCH_REVIEW",),
            current_decision_id=decision_id,
            opened_at_utc=NOW,
            recorded_at_utc=NOW,
            correlation_id=values["correlation_id"],
        )
        decision = ReviewDecision(
            decision_id=decision_id,
            case_id=case_id,
            case_revision=1,
            outcome=values["outcome"],
            actor_id=principal.actor_id,
            rationale=values["rationale"],
            evidence_references=values["evidence_references"],
            supersedes_decision_id=None,
            command_digest=digest,
            decided_at_utc=NOW,
            correlation_id=values["correlation_id"],
        )
        return case, decision

    def add_manual_observation(self, principal, **values):
        raise AssertionError("not used")

    def activate_suppression(self, principal, **values):
        raise AssertionError("not used")

    def resolve_suppression(self, principal, **values):
        raise AssertionError("not used")


def client(service: Service) -> TestClient:
    auth = TokenAuthenticator.from_json(
        json.dumps(
            {
                TOKEN: {
                    "actorId": "reviewer-1",
                    "permissions": [
                        "review:read",
                        "review:decide",
                        "review:observe",
                        "review:suppress",
                    ],
                }
            }
        )
    )
    return TestClient(
        create_app(
            service=service,
            authenticator=auth,
            readiness_probe=lambda: True,
        )
    )


def test_missing_token_returns_typed_401() -> None:
    response = client(Service()).get("/review/cases")
    assert response.status_code == 401
    assert response.json()["code"] == "CONTROL_API_UNAUTHORIZED"
    assert TOKEN not in str(response.json())


def test_queue_uses_authenticated_principal() -> None:
    service = Service()
    response = client(service).get(
        "/review/cases",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "nextCursor": None}
    assert service.principal.actor_id == "reviewer-1"


def test_decision_actor_cannot_be_supplied_by_request() -> None:
    service = Service()
    case_id = service.case_id
    body = {
        "expectedRevision": 0,
        "outcome": "accept_candidate",
        "rationale": "Verified.",
        "evidenceReferences": [DIGEST],
        "actorId": "attacker",
    }
    rejected = client(service).post(
        f"/review/cases/{case_id}/decisions",
        json=body,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert rejected.status_code == 422

    body.pop("actorId")
    accepted = client(service).post(
        f"/review/cases/{case_id}/decisions",
        json=body,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Correlation-ID": "decision-api-test",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["decision"]["actor_id"] == "reviewer-1"
    assert service.decision_call["correlation_id"] == "decision-api-test"


def test_decision_rejects_markup_and_noncanonical_evidence() -> None:
    service = Service()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    markup = client(service).post(
        f"/review/cases/{service.case_id}/decisions",
        json={
            "expectedRevision": 0,
            "outcome": "accept_candidate",
            "rationale": "<b>unsafe</b>",
            "evidenceReferences": [DIGEST],
        },
        headers=headers,
    )
    assert markup.status_code == 422

    noncanonical = client(service).post(
        f"/review/cases/{service.case_id}/decisions",
        json={
            "expectedRevision": 0,
            "outcome": "accept_candidate",
            "rationale": "Verified.",
            "evidenceReferences": ["sha256:" + "b" * 64, DIGEST],
        },
        headers=headers,
    )
    assert noncanonical.status_code == 422


def test_request_validation_returns_typed_error() -> None:
    service = Service()
    response = client(service).post(
        f"/review/cases/{service.case_id}/decisions",
        json={
            "expectedRevision": 0,
            "outcome": "accept_candidate",
            "rationale": "Verified.",
            "evidenceReferences": [DIGEST],
            "actorId": "attacker",
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "CONTROL_API_REQUEST_INVALID"
    assert response.json()["owner"] == "ControlApi.Transport"


def test_invalid_correlation_id_is_rejected_without_echo() -> None:
    response = client(Service()).get(
        "/review/cases",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Correlation-ID": "<invalid>",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "CONTROL_API_REQUEST_INVALID"
    assert response.headers["X-Correlation-ID"] != "<invalid>"


def test_runtime_openapi_route_is_not_exposed() -> None:
    response = client(Service()).get("/openapi.json")
    assert response.status_code == 404
