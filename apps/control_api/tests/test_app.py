from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from control_api.app import create_app
from control_api.auth import TokenAuthenticator
from fastapi.testclient import TestClient

from collection_application import (
    CampaignRunCreated,
    CollectionRunStatus,
    RunCoverageReport,
    StageCoverage,
    StageRunStatus,
    WorkStateCount,
)
from collection_domain import (
    CollectionRunState,
    StageRunState,
    WorkStage,
    WorkUnitState,
)
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


class RunCreator:
    def __init__(self, control: RunControl) -> None:
        self.control = control
        self.command = None

    def create(self, command):
        self.command = command
        return CampaignRunCreated(
            run_id=command.run_id,
            campaign_key=command.campaign_key,
            config_bundle_digest=DIGEST,
            initial_work_ids=(),
        )


class RunControl:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.state = CollectionRunState.RUNNING
        self.revision = 0
        self.transition = None

    def get(self, run_id, *, correlation_id):
        self.run_id = run_id
        return self._status()

    def coverage(self, run_id, *, correlation_id):
        return RunCoverageReport(
            run_id=run_id,
            state=self.state,
            revision=self.revision,
            stages=(
                StageCoverage(
                    stage=WorkStage.DISCOVERY,
                    total=1,
                    pending=1,
                    leased=0,
                    retry_wait=0,
                    succeeded=0,
                    dead_letter=0,
                    blocked_by_policy=0,
                    cancelled=0,
                    superseded=0,
                ),
            ),
            blockers=(),
        )

    def pause(self, run_id, **values):
        return self._change(run_id, CollectionRunState.PAUSED, values)

    def resume(self, run_id, **values):
        return self._change(run_id, CollectionRunState.RUNNING, values)

    def cancel(self, run_id, **values):
        return self._change(run_id, CollectionRunState.CANCELLED, values)

    def _change(self, run_id, state, values):
        self.run_id = run_id
        self.state = state
        self.revision += 1
        self.transition = values
        return self._status()

    def _status(self):
        return CollectionRunStatus(
            run_id=self.run_id,
            campaign_key="berlin_recording_services",
            config_bundle_digest=DIGEST,
            state=self.state,
            revision=self.revision,
            created_at_utc=NOW,
            updated_at_utc=NOW,
            stages=(
                StageRunStatus(
                    stage_run_id=uuid4(),
                    stage=WorkStage.DISCOVERY,
                    state=StageRunState.RUNNING,
                    revision=0,
                    work_counts=(WorkStateCount(state=WorkUnitState.PENDING, count=1),),
                ),
            ),
        )


def client(service: Service, run_control: RunControl | None = None) -> TestClient:
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
                        "runs:create",
                        "runs:read",
                        "runs:control",
                    ],
                }
            }
        )
    )
    control = run_control or RunControl()
    return TestClient(
        create_app(
            service=service,
            run_creator=RunCreator(control),
            run_control=control,
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


def test_run_create_read_coverage_and_pause_are_operator_owned() -> None:
    service = Service()
    control = RunControl()
    api = client(service, control)
    run_id = uuid4()
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Correlation-ID": "run-api-test",
    }

    created = api.post(
        "/runs",
        json={"runId": str(run_id), "campaignKey": "berlin_recording_services"},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["runId"] == str(run_id)
    assert created.json()["state"] == "running"

    read = api.get(f"/runs/{run_id}", headers=headers)
    assert read.status_code == 200
    assert read.json()["revision"] == 0

    coverage = api.get(f"/runs/{run_id}/coverage", headers=headers)
    assert coverage.status_code == 200
    assert coverage.json()["total"] == 1
    assert coverage.json()["terminal"] == 0
    assert coverage.json()["blockers"] == []

    paused = api.post(
        f"/runs/{run_id}/pause",
        json={"expectedRevision": 0, "reason": "Operator pause."},
        headers=headers,
    )
    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"
    assert control.transition["actor_id"] == "reviewer-1"
    assert control.transition["correlation_id"] == "run-api-test"
