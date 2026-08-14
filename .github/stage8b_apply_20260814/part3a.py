from __future__ import annotations

import re
from pathlib import Path

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def clone_project() -> None:
    source = ROOT / "apps/worker_gateway/pyproject.toml"
    text = source.read_text(encoding="utf-8")
    text = text.replace("worker-gateway", "control-api")
    text = text.replace("worker_gateway", "control_api")
    dependencies = '''dependencies = [
  "collection-infrastructure",
  "fastapi==0.141.1",
  "pydantic==2.13.4",
  "review-application",
  "review-contracts",
  "sqlalchemy==2.0.51",
  "uvicorn==0.52.1",
]'''
    text, count = re.subn(r"(?ms)^dependencies = \[.*?^\]", dependencies, text, count=1)
    if count != 1:
        raise RuntimeError("worker gateway dependency block was not found")
    target = ROOT / "apps/control_api/pyproject.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def main() -> int:
    clone_project()

    write(
        "apps/control_api/src/control_api/auth.py",
        '''from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass

from review_application import ReviewerPrincipal


class ReviewAuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _Credential:
    token: str
    principal: ReviewerPrincipal


class TokenAuthenticator:
    def __init__(self, credentials: tuple[_Credential, ...]) -> None:
        if not credentials:
            raise ValueError("at least one reviewer credential is required")
        tokens = tuple(credential.token for credential in credentials)
        if len(tokens) != len(set(tokens)):
            raise ValueError("reviewer tokens must be unique")
        self._credentials = credentials

    @classmethod
    def from_json(cls, value: str) -> TokenAuthenticator:
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("CONTROL_API_REVIEWER_TOKENS_JSON is malformed") from exc
        if not isinstance(raw, dict) or not raw:
            raise ValueError("reviewer token configuration must be a non-empty object")
        credentials: list[_Credential] = []
        for token, payload in raw.items():
            if not isinstance(token, str) or len(token) < 32:
                raise ValueError("reviewer tokens must contain at least 32 characters")
            if not isinstance(payload, dict):
                raise ValueError("reviewer credential payload must be an object")
            if set(payload) != {"actorId", "permissions"}:
                raise ValueError("reviewer credential payload has an unexpected shape")
            actor_id = payload["actorId"]
            permissions = payload["permissions"]
            if not isinstance(actor_id, str) or not actor_id or len(actor_id) > 200:
                raise ValueError("reviewer actorId is invalid")
            if not isinstance(permissions, list) or not permissions:
                raise ValueError("reviewer permissions must be a non-empty array")
            allowed = {
                "review:read",
                "review:decide",
                "review:observe",
                "review:suppress",
            }
            if any(not isinstance(item, str) or item not in allowed for item in permissions):
                raise ValueError("reviewer credential contains an unsupported permission")
            if len(permissions) != len(set(permissions)):
                raise ValueError("reviewer permissions must be unique")
            credentials.append(
                _Credential(
                    token=token,
                    principal=ReviewerPrincipal(
                        actor_id=actor_id,
                        permissions=frozenset(permissions),  # type: ignore[arg-type]
                    ),
                )
            )
        return cls(tuple(credentials))

    def authenticate(self, token: str | None) -> ReviewerPrincipal:
        if token is None:
            raise ReviewAuthenticationError("reviewer bearer token is required")
        matched: ReviewerPrincipal | None = None
        for credential in self._credentials:
            if hmac.compare_digest(token, credential.token):
                matched = credential.principal
        if matched is None:
            raise ReviewAuthenticationError("reviewer bearer token is invalid")
        return matched
''',
    )

    write(
        "apps/control_api/src/control_api/schemas.py",
        '''from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from review_application import ReviewCaseDetail, ReviewQueuePage, encode_cursor
from review_contracts import (
    CandidateRevision,
    ManualObservation,
    QualityRecord,
    ReviewCase,
    ReviewDecision,
    SuppressionRevision,
)

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Code = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")]
Key = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")]


class ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ErrorResponse(ApiModel):
    code: Code
    owner: str
    message: str
    required_action: str = Field(alias="requiredAction")
    correlation_id: str = Field(alias="correlationId")


class ReviewQueueItemResponse(ApiModel):
    case_id: UUID = Field(alias="caseId")
    candidate_id: UUID = Field(alias="candidateId")
    candidate_revision: int = Field(alias="candidateRevision")
    revision: int
    state: str
    reason_codes: tuple[str, ...] = Field(alias="reasonCodes")
    current_decision_id: UUID | None = Field(alias="currentDecisionId")
    recorded_at_utc: datetime = Field(alias="recordedAtUtc")


class ReviewQueueResponse(ApiModel):
    items: tuple[ReviewQueueItemResponse, ...]
    next_cursor: str | None = Field(alias="nextCursor")

    @classmethod
    def from_page(cls, page: ReviewQueuePage) -> ReviewQueueResponse:
        return cls(
            items=tuple(
                ReviewQueueItemResponse(
                    case_id=item.case_id,
                    candidate_id=item.candidate_id,
                    candidate_revision=item.candidate_revision,
                    revision=item.revision,
                    state=item.state,
                    reason_codes=item.reason_codes,
                    current_decision_id=item.current_decision_id,
                    recorded_at_utc=item.recorded_at_utc,
                )
                for item in page.items
            ),
            next_cursor=encode_cursor(page.next_cursor),
        )


class ReviewCaseDetailResponse(ApiModel):
    case: ReviewCase
    candidate: CandidateRevision
    quality: QualityRecord | None
    decisions: tuple[ReviewDecision, ...]
    manual_observations: tuple[ManualObservation, ...] = Field(
        alias="manualObservations"
    )
    active_suppressions: tuple[SuppressionRevision, ...] = Field(
        alias="activeSuppressions"
    )

    @classmethod
    def from_detail(cls, detail: ReviewCaseDetail) -> ReviewCaseDetailResponse:
        return cls(
            case=detail.case,
            candidate=detail.candidate,
            quality=detail.quality,
            decisions=detail.decisions,
            manual_observations=detail.manual_observations,
            active_suppressions=detail.active_suppressions,
        )


class SubmitDecisionRequest(ApiModel):
    expected_revision: Annotated[int, Field(ge=0)] = Field(alias="expectedRevision")
    outcome: Literal[
        "accept_candidate",
        "reject_candidate",
        "approve_merge",
        "reject_merge",
        "request_recollection",
        "block_export",
    ]
    rationale: Annotated[str, Field(min_length=1, max_length=4000)]
    evidence_references: tuple[Digest, ...] = Field(alias="evidenceReferences")
    supersedes_decision_id: UUID | None = Field(
        default=None,
        alias="supersedesDecisionId",
    )


class DecisionResponse(ApiModel):
    case: ReviewCase
    decision: ReviewDecision


class ManualObservationRequest(ApiModel):
    candidate_revision: Annotated[int, Field(ge=0)] = Field(alias="candidateRevision")
    field_key: Key = Field(alias="fieldKey")
    value_text: Annotated[str, Field(min_length=1, max_length=4000)] = Field(
        alias="valueText"
    )
    reason_code: Code = Field(alias="reasonCode")
    supersedes_observation_id: UUID | None = Field(
        default=None,
        alias="supersedesObservationId",
    )


class ActivateSuppressionRequest(ApiModel):
    target_kind: Literal["candidate", "source_observation", "artifact", "source"] = Field(
        alias="targetKind"
    )
    target_id: Annotated[str, Field(min_length=1, max_length=500)] = Field(alias="targetId")
    scopes: tuple[Literal["discovery", "normalization", "export"], ...]
    reason_code: Code = Field(alias="reasonCode")
    evidence_reference: Digest = Field(alias="evidenceReference")
    expires_at_utc: datetime | None = Field(default=None, alias="expiresAtUtc")


class ResolveSuppressionRequest(ApiModel):
    expected_revision: Annotated[int, Field(ge=0)] = Field(alias="expectedRevision")
    evidence_reference: Digest = Field(alias="evidenceReference")
''',
    )

    write(
        "apps/control_api/src/control_api/app.py",
        '''from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from review_application import (
    ReviewApplicationError,
    ReviewService,
    ReviewerPrincipal,
    decode_cursor,
)

from control_api.auth import ReviewAuthenticationError, TokenAuthenticator
from control_api.schemas import (
    ActivateSuppressionRequest,
    DecisionResponse,
    ErrorResponse,
    ManualObservationRequest,
    ResolveSuppressionRequest,
    ReviewCaseDetailResponse,
    ReviewQueueResponse,
    SubmitDecisionRequest,
)

ReadinessProbe = Callable[[], bool]


def create_app(
    *,
    service: ReviewService,
    authenticator: TokenAuthenticator,
    readiness_probe: ReadinessProbe,
) -> FastAPI:
    app = FastAPI(
        title="Collection Control API",
        version="review-control-api-v1",
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    bearer = HTTPBearer(auto_error=False)

    def principal_dependency(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer),
        ],
    ) -> ReviewerPrincipal:
        token = None if credentials is None else credentials.credentials
        try:
            return authenticator.authenticate(token)
        except ReviewAuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def correlation_dependency(
        x_correlation_id: Annotated[str | None, Header(alias="X-Correlation-ID")] = None,
    ) -> str:
        return x_correlation_id.strip() if x_correlation_id and x_correlation_id.strip() else str(uuid4())

    @app.exception_handler(ReviewApplicationError)
    async def review_error_handler(
        request: Request,
        exc: ReviewApplicationError,
    ) -> JSONResponse:
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid4())
        body = ErrorResponse(
            code=exc.code,
            owner=exc.owner,
            message=exc.message,
            required_action=exc.required_action,
            correlation_id=correlation_id,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(mode="json", by_alias=True),
            headers={"X-Correlation-ID": correlation_id},
        )

    @app.get("/health/live", operation_id="control_api_liveness")
    def liveness() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", operation_id="control_api_readiness")
    def readiness() -> dict[str, str]:
        if not readiness_probe():
            raise HTTPException(status_code=503, detail="control API dependency is unavailable")
        return {"status": "ready"}

    @app.get(
        "/review/cases",
        response_model=ReviewQueueResponse,
        responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
        operation_id="list_review_cases",
    )
    def list_cases(
        response: Response,
        principal: Annotated[ReviewerPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
        state: Annotated[str, Query(pattern="^(open|decided)$")] = "open",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> ReviewQueueResponse:
        response.headers["X-Correlation-ID"] = correlation_id
        page = service.list_cases(
            principal,
            state=state,
            limit=limit,
            cursor=decode_cursor(cursor),
        )
        return ReviewQueueResponse.from_page(page)

    @app.get(
        "/review/cases/{case_id}",
        response_model=ReviewCaseDetailResponse,
        operation_id="get_review_case",
    )
    def get_case(
        case_id: UUID,
        response: Response,
        principal: Annotated[ReviewerPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
    ) -> ReviewCaseDetailResponse:
        response.headers["X-Correlation-ID"] = correlation_id
        return ReviewCaseDetailResponse.from_detail(service.get_case(principal, case_id))

    @app.post(
        "/review/cases/{case_id}/decisions",
        response_model=DecisionResponse,
        operation_id="submit_review_decision",
    )
    def submit_decision(
        case_id: UUID,
        body: SubmitDecisionRequest,
        response: Response,
        principal: Annotated[ReviewerPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
    ) -> DecisionResponse:
        response.headers["X-Correlation-ID"] = correlation_id
        case, decision = service.submit_decision(
            principal,
            case_id=case_id,
            expected_revision=body.expected_revision,
            outcome=body.outcome,
            rationale=body.rationale,
            evidence_references=body.evidence_references,
            supersedes_decision_id=body.supersedes_decision_id,
            correlation_id=correlation_id,
        )
        return DecisionResponse(case=case, decision=decision)

    @app.post(
        "/review/candidates/{candidate_id}/manual-observations",
        response_model=dict,
        operation_id="add_manual_observation",
    )
    def add_manual_observation(
        candidate_id: UUID,
        body: ManualObservationRequest,
        response: Response,
        principal: Annotated[ReviewerPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
    ) -> dict:
        response.headers["X-Correlation-ID"] = correlation_id
        observation = service.add_manual_observation(
            principal,
            candidate_id=candidate_id,
            candidate_revision=body.candidate_revision,
            field_key=body.field_key,
            value_text=body.value_text,
            reason_code=body.reason_code,
            supersedes_observation_id=body.supersedes_observation_id,
            correlation_id=correlation_id,
        )
        return observation.model_dump(mode="json")

    @app.post(
        "/review/suppressions",
        response_model=dict,
        operation_id="activate_review_suppression",
    )
    def activate_suppression(
        body: ActivateSuppressionRequest,
        response: Response,
        principal: Annotated[ReviewerPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
    ) -> dict:
        response.headers["X-Correlation-ID"] = correlation_id
        suppression = service.activate_suppression(
            principal,
            target_kind=body.target_kind,
            target_id=body.target_id,
            scopes=body.scopes,
            reason_code=body.reason_code,
            evidence_reference=body.evidence_reference,
            expires_at_utc=body.expires_at_utc,
            correlation_id=correlation_id,
        )
        return suppression.model_dump(mode="json")

    @app.post(
        "/review/suppressions/{suppression_id}/resolve",
        response_model=dict,
        operation_id="resolve_review_suppression",
    )
    def resolve_suppression_route(
        suppression_id: UUID,
        body: ResolveSuppressionRequest,
        response: Response,
        principal: Annotated[ReviewerPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
    ) -> dict:
        response.headers["X-Correlation-ID"] = correlation_id
        suppression = service.resolve_suppression(
            principal,
            suppression_id=suppression_id,
            expected_revision=body.expected_revision,
            evidence_reference=body.evidence_reference,
            correlation_id=correlation_id,
        )
        return suppression.model_dump(mode="json")

    return app
''',
    )

    write(
        "apps/control_api/src/control_api/main.py",
        '''from __future__ import annotations

import os
from datetime import UTC, datetime

import sqlalchemy as sa
from collection_infrastructure.postgres import PostgresReviewRepository
from review_application import ReviewService
from sqlalchemy.pool import NullPool

from control_api.app import create_app
from control_api.auth import TokenAuthenticator


def create_runtime_app():
    database_url = _required("COLLECTOR_DATABASE_URL")
    token_json = _required("CONTROL_API_REVIEWER_TOKENS_JSON")
    engine = sa.create_engine(database_url, pool_pre_ping=True, poolclass=NullPool)
    service = ReviewService(
        PostgresReviewRepository(engine),
        clock=lambda: datetime.now(UTC),
    )

    def readiness() -> bool:
        try:
            with engine.connect() as connection:
                connection.execute(sa.text("SELECT 1"))
            return True
        except sa.exc.SQLAlchemyError:
            return False

    return create_app(
        service=service,
        authenticator=TokenAuthenticator.from_json(token_json),
        readiness_probe=readiness,
    )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"required Control API setting {name} is missing")
    return value


app = create_runtime_app()
''',
    )
    write(
        "apps/control_api/src/control_api/__init__.py",
        '''from control_api.app import create_app
from control_api.auth import ReviewAuthenticationError, TokenAuthenticator

__all__ = ["ReviewAuthenticationError", "TokenAuthenticator", "create_app"]
''',
    )
    write(
        "apps/control_api/src/control_api/__main__.py",
        '''import uvicorn

uvicorn.run("control_api.main:app", host="0.0.0.0", port=8080, factory=False)
''',
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
