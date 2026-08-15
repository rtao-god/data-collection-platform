import re
from collections.abc import Callable
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from collection_application import (
    CampaignRunService,
    CreateCampaignRun,
    RunControlService,
)
from collection_contracts import OwnerContextError
from control_api.auth import (
    ControlAuthenticationError,
    OperatorPermission,
    OperatorPrincipal,
    TokenAuthenticator,
)
from control_api.schemas import (
    ActivateSuppressionRequest,
    CreateRunRequest,
    DecisionResponse,
    ErrorResponse,
    ManualObservationRequest,
    ResolveSuppressionRequest,
    ReviewCaseDetailResponse,
    ReviewQueueResponse,
    RunCoverageResponse,
    RunResponse,
    RunTransitionRequest,
    SubmitDecisionRequest,
)
from review_application import (
    ReviewApplicationError,
    ReviewService,
    decode_cursor,
)
from review_contracts import ManualObservation, SuppressionRevision

ReadinessProbe = Callable[[], bool]
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _correlation_id(value: str | None, *, reject_invalid: bool) -> str:
    if value is None or not value.strip():
        return str(uuid4())
    normalized = value.strip()
    if _CORRELATION_ID.fullmatch(normalized) is None:
        if reject_invalid:
            raise HTTPException(
                status_code=400,
                detail="X-Correlation-ID is invalid",
            )
        return str(uuid4())
    return normalized


def _owner_status(code: str) -> int:
    if code.endswith("_NOT_FOUND"):
        return 404
    if code.endswith("_REVISION_CONFLICT") or code.endswith("_TRANSITION_INVALID"):
        return 409
    if code.endswith("_STORAGE_FAILED"):
        return 503
    if code.endswith("_BLOCKED") or code.endswith("_INVALID"):
        return 422
    return 500


def create_app(
    *,
    service: ReviewService,
    run_creator: CampaignRunService,
    run_control: RunControlService,
    authenticator: TokenAuthenticator,
    readiness_probe: ReadinessProbe,
) -> FastAPI:
    app = FastAPI(
        title="Collection Control API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    bearer = HTTPBearer(auto_error=False)

    def principal_dependency(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer),
        ],
    ) -> OperatorPrincipal:
        token = None if credentials is None else credentials.credentials
        try:
            return authenticator.authenticate(token)
        except ControlAuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def correlation_dependency(
        x_correlation_id: Annotated[
            str | None,
            Header(alias="X-Correlation-ID"),
        ] = None,
    ) -> str:
        return _correlation_id(x_correlation_id, reject_invalid=True)

    def require_permission(
        principal: OperatorPrincipal,
        permission: OperatorPermission,
    ) -> None:
        try:
            principal.require(permission)
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail=f"operator permission {permission} is required",
            ) from exc

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        correlation_id = _correlation_id(
            request.headers.get("X-Correlation-ID"),
            reject_invalid=False,
        )
        body = ErrorResponse(
            code="CONTROL_API_REQUEST_INVALID",
            owner="ControlApi.Transport",
            message="The request violates the Control API contract.",
            required_action=("Correct the request using the generated Control API contract."),
            correlation_id=correlation_id,
        )
        return JSONResponse(
            status_code=422,
            content=body.model_dump(mode="json", by_alias=True),
            headers={"X-Correlation-ID": correlation_id},
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(
        request: Request,
        exc: HTTPException,
    ) -> JSONResponse:
        correlation_id = _correlation_id(
            request.headers.get("X-Correlation-ID"),
            reject_invalid=False,
        )
        if exc.status_code == 400:
            code = "CONTROL_API_REQUEST_INVALID"
            owner = "ControlApi.Transport"
            action = "Provide a valid request and correlation identifier."
        elif exc.status_code == 401:
            code = "CONTROL_API_UNAUTHORIZED"
            owner = "ControlApi.Auth"
            action = "Provide a valid reviewer bearer credential."
        elif exc.status_code == 503:
            code = "CONTROL_API_UNAVAILABLE"
            owner = "ControlApi.Readiness"
            action = "Restore the Control API dependency and retry."
        else:
            code = "CONTROL_API_HTTP_ERROR"
            owner = "ControlApi.Transport"
            action = "Correct the request or inspect the owning route."
        body = ErrorResponse(
            code=code,
            owner=owner,
            message=str(exc.detail),
            required_action=action,
            correlation_id=correlation_id,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(mode="json", by_alias=True),
            headers={"X-Correlation-ID": correlation_id},
        )

    @app.exception_handler(OwnerContextError)
    async def owner_context_error_handler(
        request: Request,
        exc: OwnerContextError,
    ) -> JSONResponse:
        envelope = exc.envelope
        status_code = _owner_status(envelope.code)
        body = ErrorResponse(
            code=envelope.code,
            owner=envelope.owner,
            message=envelope.message,
            required_action=envelope.required_action,
            correlation_id=envelope.correlation_id,
            context=envelope.context,
        )
        return JSONResponse(
            status_code=status_code,
            content=body.model_dump(mode="json", by_alias=True),
            headers={"X-Correlation-ID": envelope.correlation_id},
        )

    @app.exception_handler(ReviewApplicationError)
    async def review_error_handler(
        request: Request,
        exc: ReviewApplicationError,
    ) -> JSONResponse:
        correlation_id = _correlation_id(
            request.headers.get("X-Correlation-ID"),
            reject_invalid=False,
        )
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
            raise HTTPException(
                status_code=503,
                detail="control API dependency is unavailable",
            )
        return {"status": "ready"}

    @app.post(
        "/runs",
        response_model=RunResponse,
        status_code=201,
        operation_id="create_collection_run",
    )
    def create_run(
        body: CreateRunRequest,
        response: Response,
        principal: Annotated[OperatorPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
    ) -> RunResponse:
        require_permission(principal, "runs:create")
        response.headers["X-Correlation-ID"] = correlation_id
        created = run_creator.create(
            CreateCampaignRun(
                run_id=body.run_id,
                campaign_key=body.campaign_key,
                correlation_id=correlation_id,
            )
        )
        return RunResponse.from_status(
            run_control.get(created.run_id, correlation_id=correlation_id)
        )

    @app.get(
        "/runs/{run_id}",
        response_model=RunResponse,
        operation_id="get_collection_run",
    )
    def get_run(
        run_id: UUID,
        response: Response,
        principal: Annotated[OperatorPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
    ) -> RunResponse:
        require_permission(principal, "runs:read")
        response.headers["X-Correlation-ID"] = correlation_id
        return RunResponse.from_status(run_control.get(run_id, correlation_id=correlation_id))

    @app.get(
        "/runs/{run_id}/coverage",
        response_model=RunCoverageResponse,
        operation_id="get_collection_run_coverage",
    )
    def get_run_coverage(
        run_id: UUID,
        response: Response,
        principal: Annotated[OperatorPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
    ) -> RunCoverageResponse:
        require_permission(principal, "runs:read")
        response.headers["X-Correlation-ID"] = correlation_id
        return RunCoverageResponse.from_report(
            run_control.coverage(run_id, correlation_id=correlation_id)
        )

    @app.post(
        "/runs/{run_id}/pause",
        response_model=RunResponse,
        operation_id="pause_collection_run",
    )
    def pause_run(
        run_id: UUID,
        body: RunTransitionRequest,
        response: Response,
        principal: Annotated[OperatorPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
    ) -> RunResponse:
        require_permission(principal, "runs:control")
        response.headers["X-Correlation-ID"] = correlation_id
        return RunResponse.from_status(
            run_control.pause(
                run_id,
                expected_revision=body.expected_revision,
                actor_id=principal.actor_id,
                reason=body.reason,
                correlation_id=correlation_id,
            )
        )

    @app.post(
        "/runs/{run_id}/resume",
        response_model=RunResponse,
        operation_id="resume_collection_run",
    )
    def resume_run(
        run_id: UUID,
        body: RunTransitionRequest,
        response: Response,
        principal: Annotated[OperatorPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
    ) -> RunResponse:
        require_permission(principal, "runs:control")
        response.headers["X-Correlation-ID"] = correlation_id
        return RunResponse.from_status(
            run_control.resume(
                run_id,
                expected_revision=body.expected_revision,
                actor_id=principal.actor_id,
                reason=body.reason,
                correlation_id=correlation_id,
            )
        )

    @app.post(
        "/runs/{run_id}/cancel",
        response_model=RunResponse,
        operation_id="cancel_collection_run",
    )
    def cancel_run(
        run_id: UUID,
        body: RunTransitionRequest,
        response: Response,
        principal: Annotated[OperatorPrincipal, Depends(principal_dependency)],
        correlation_id: Annotated[str, Depends(correlation_dependency)],
    ) -> RunResponse:
        require_permission(principal, "runs:control")
        response.headers["X-Correlation-ID"] = correlation_id
        return RunResponse.from_status(
            run_control.cancel(
                run_id,
                expected_revision=body.expected_revision,
                actor_id=principal.actor_id,
                reason=body.reason,
                correlation_id=correlation_id,
            )
        )

    @app.get(
        "/review/cases",
        response_model=ReviewQueueResponse,
        responses={
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
        },
        operation_id="list_review_cases",
    )
    def list_cases(
        response: Response,
        principal: Annotated[
            OperatorPrincipal,
            Depends(principal_dependency),
        ],
        correlation_id: Annotated[
            str,
            Depends(correlation_dependency),
        ],
        state: Annotated[
            str,
            Query(pattern="^(open|decided)$"),
        ] = "open",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> ReviewQueueResponse:
        response.headers["X-Correlation-ID"] = correlation_id
        page = service.list_cases(
            principal.as_reviewer(),
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
        principal: Annotated[
            OperatorPrincipal,
            Depends(principal_dependency),
        ],
        correlation_id: Annotated[
            str,
            Depends(correlation_dependency),
        ],
    ) -> ReviewCaseDetailResponse:
        response.headers["X-Correlation-ID"] = correlation_id
        return ReviewCaseDetailResponse.from_detail(
            service.get_case(principal.as_reviewer(), case_id)
        )

    @app.post(
        "/review/cases/{case_id}/decisions",
        response_model=DecisionResponse,
        operation_id="submit_review_decision",
    )
    def submit_decision(
        case_id: UUID,
        body: SubmitDecisionRequest,
        response: Response,
        principal: Annotated[
            OperatorPrincipal,
            Depends(principal_dependency),
        ],
        correlation_id: Annotated[
            str,
            Depends(correlation_dependency),
        ],
    ) -> DecisionResponse:
        response.headers["X-Correlation-ID"] = correlation_id
        case, decision = service.submit_decision(
            principal.as_reviewer(),
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
        response_model=ManualObservation,
        operation_id="add_manual_observation",
    )
    def add_manual_observation(
        candidate_id: UUID,
        body: ManualObservationRequest,
        response: Response,
        principal: Annotated[
            OperatorPrincipal,
            Depends(principal_dependency),
        ],
        correlation_id: Annotated[
            str,
            Depends(correlation_dependency),
        ],
    ) -> ManualObservation:
        response.headers["X-Correlation-ID"] = correlation_id
        return service.add_manual_observation(
            principal.as_reviewer(),
            candidate_id=candidate_id,
            candidate_revision=body.candidate_revision,
            field_key=body.field_key,
            value_text=body.value_text,
            reason_code=body.reason_code,
            supersedes_observation_id=body.supersedes_observation_id,
            correlation_id=correlation_id,
        )

    @app.post(
        "/review/suppressions",
        response_model=SuppressionRevision,
        operation_id="activate_review_suppression",
    )
    def activate_suppression(
        body: ActivateSuppressionRequest,
        response: Response,
        principal: Annotated[
            OperatorPrincipal,
            Depends(principal_dependency),
        ],
        correlation_id: Annotated[
            str,
            Depends(correlation_dependency),
        ],
    ) -> SuppressionRevision:
        response.headers["X-Correlation-ID"] = correlation_id
        return service.activate_suppression(
            principal.as_reviewer(),
            target_kind=body.target_kind,
            target_id=body.target_id,
            scopes=body.scopes,
            reason_code=body.reason_code,
            evidence_reference=body.evidence_reference,
            expires_at_utc=body.expires_at_utc,
            correlation_id=correlation_id,
        )

    @app.post(
        "/review/suppressions/{suppression_id}/resolve",
        response_model=SuppressionRevision,
        operation_id="resolve_review_suppression",
    )
    def resolve_suppression_route(
        suppression_id: UUID,
        body: ResolveSuppressionRequest,
        response: Response,
        principal: Annotated[
            OperatorPrincipal,
            Depends(principal_dependency),
        ],
        correlation_id: Annotated[
            str,
            Depends(correlation_dependency),
        ],
    ) -> SuppressionRevision:
        response.headers["X-Correlation-ID"] = correlation_id
        return service.resolve_suppression(
            principal.as_reviewer(),
            suppression_id=suppression_id,
            expected_revision=body.expected_revision,
            evidence_reference=body.evidence_reference,
            correlation_id=correlation_id,
        )

    return app
