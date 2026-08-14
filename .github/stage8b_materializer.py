from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path.cwd()


def rewrite_project_sources(
    path: Path,
    *,
    description: str,
    sources: tuple[str, ...],
    remove_dependencies: tuple[str, ...] = (),
) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("description = "):
            lines[index] = f'description = "{description}"'
            break
    else:
        raise RuntimeError(f"{path}: project description is missing")
    text = "\n".join(lines) + "\n"
    for dependency in remove_dependencies:
        text = text.replace(f'  "{dependency}",\n', "")
    source_start = text.index("[tool.uv.sources]")
    rendered_sources = "[tool.uv.sources]\n" + "".join(
        f"{source} = {{ workspace = true }}\n" for source in sources
    )
    path.write_text(text[:source_start] + rendered_sources, encoding="utf-8")


def write_canonical_auth() -> None:
    path = ROOT / "apps/control_api/src/control_api/auth.py"
    path.write_text(
        dedent(
            '''\
            from __future__ import annotations

            import hmac
            import json
            from dataclasses import dataclass
            from typing import cast

            from review_application import Permission, ReviewerPrincipal


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
                        raise ValueError(
                            "CONTROL_API_REVIEWER_TOKENS_JSON is malformed"
                        ) from exc
                    if not isinstance(raw, dict) or not raw:
                        raise ValueError(
                            "reviewer token configuration must be a non-empty object"
                        )
                    credentials: list[_Credential] = []
                    for token, payload in raw.items():
                        if not isinstance(token, str) or len(token) < 32:
                            raise ValueError(
                                "reviewer tokens must contain at least 32 characters"
                            )
                        if not isinstance(payload, dict):
                            raise ValueError(
                                "reviewer credential payload must be an object"
                            )
                        if set(payload) != {"actorId", "permissions"}:
                            raise ValueError(
                                "reviewer credential payload has an unexpected shape"
                            )
                        actor_id = payload["actorId"]
                        permissions = payload["permissions"]
                        if (
                            not isinstance(actor_id, str)
                            or not actor_id
                            or len(actor_id) > 200
                        ):
                            raise ValueError("reviewer actorId is invalid")
                        if not isinstance(permissions, list) or not permissions:
                            raise ValueError(
                                "reviewer permissions must be a non-empty array"
                            )
                        allowed = {
                            "review:read",
                            "review:decide",
                            "review:observe",
                            "review:suppress",
                        }
                        parsed_permissions: list[Permission] = []
                        for permission in permissions:
                            if (
                                not isinstance(permission, str)
                                or permission not in allowed
                            ):
                                raise ValueError(
                                    "reviewer credential contains an unsupported permission"
                                )
                            parsed_permissions.append(cast(Permission, permission))
                        if len(parsed_permissions) != len(set(parsed_permissions)):
                            raise ValueError("reviewer permissions must be unique")
                        credentials.append(
                            _Credential(
                                token=token,
                                principal=ReviewerPrincipal(
                                    actor_id=actor_id,
                                    permissions=frozenset(parsed_permissions),
                                ),
                            )
                        )
                    return cls(tuple(credentials))

                def authenticate(self, token: str | None) -> ReviewerPrincipal:
                    if token is None:
                        raise ReviewAuthenticationError(
                            "reviewer bearer token is required"
                        )
                    matched: ReviewerPrincipal | None = None
                    for credential in self._credentials:
                        if hmac.compare_digest(token, credential.token):
                            matched = credential.principal
                    if matched is None:
                        raise ReviewAuthenticationError(
                            "reviewer bearer token is invalid"
                        )
                    return matched
            '''
        ),
        encoding="utf-8",
    )


def write_canonical_app() -> None:
    path = ROOT / "apps/control_api/src/control_api/app.py"
    path.write_text(
        dedent(
            '''\
            from __future__ import annotations

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
            from review_application import (
                ReviewApplicationError,
                ReviewService,
                ReviewerPrincipal,
                decode_cursor,
            )
            from review_contracts import ManualObservation, SuppressionRevision

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


            def create_app(
                *,
                service: ReviewService,
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
                ) -> ReviewerPrincipal:
                    token = None if credentials is None else credentials.credentials
                    try:
                        return authenticator.authenticate(token)
                    except ReviewAuthenticationError as exc:
                        raise HTTPException(status_code=401, detail=str(exc)) from exc

                def correlation_dependency(
                    x_correlation_id: Annotated[
                        str | None,
                        Header(alias="X-Correlation-ID"),
                    ] = None,
                ) -> str:
                    return _correlation_id(x_correlation_id, reject_invalid=True)

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
                        required_action=(
                            "Correct the request using the generated Control API contract."
                        ),
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
                        ReviewerPrincipal,
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
                    principal: Annotated[
                        ReviewerPrincipal,
                        Depends(principal_dependency),
                    ],
                    correlation_id: Annotated[
                        str,
                        Depends(correlation_dependency),
                    ],
                ) -> ReviewCaseDetailResponse:
                    response.headers["X-Correlation-ID"] = correlation_id
                    return ReviewCaseDetailResponse.from_detail(
                        service.get_case(principal, case_id)
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
                        ReviewerPrincipal,
                        Depends(principal_dependency),
                    ],
                    correlation_id: Annotated[
                        str,
                        Depends(correlation_dependency),
                    ],
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
                    response_model=ManualObservation,
                    operation_id="add_manual_observation",
                )
                def add_manual_observation(
                    candidate_id: UUID,
                    body: ManualObservationRequest,
                    response: Response,
                    principal: Annotated[
                        ReviewerPrincipal,
                        Depends(principal_dependency),
                    ],
                    correlation_id: Annotated[
                        str,
                        Depends(correlation_dependency),
                    ],
                ) -> ManualObservation:
                    response.headers["X-Correlation-ID"] = correlation_id
                    return service.add_manual_observation(
                        principal,
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
                        ReviewerPrincipal,
                        Depends(principal_dependency),
                    ],
                    correlation_id: Annotated[
                        str,
                        Depends(correlation_dependency),
                    ],
                ) -> SuppressionRevision:
                    response.headers["X-Correlation-ID"] = correlation_id
                    return service.activate_suppression(
                        principal,
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
                        ReviewerPrincipal,
                        Depends(principal_dependency),
                    ],
                    correlation_id: Annotated[
                        str,
                        Depends(correlation_dependency),
                    ],
                ) -> SuppressionRevision:
                    response.headers["X-Correlation-ID"] = correlation_id
                    return service.resolve_suppression(
                        principal,
                        suppression_id=suppression_id,
                        expected_revision=body.expected_revision,
                        evidence_reference=body.evidence_reference,
                        correlation_id=correlation_id,
                    )

                return app
            '''
        ),
        encoding="utf-8",
    )


def write_canonical_main() -> None:
    path = ROOT / "apps/control_api/src/control_api/main.py"
    path.write_text(
        dedent(
            '''\
            from __future__ import annotations

            import os
            from datetime import UTC, datetime

            import sqlalchemy as sa
            from collection_infrastructure.postgres import PostgresReviewRepository
            from fastapi import FastAPI
            from review_application import ReviewService
            from sqlalchemy.pool import NullPool

            from control_api.app import create_app
            from control_api.auth import TokenAuthenticator


            def create_runtime_app() -> FastAPI:
                database_url = _required("COLLECTOR_DATABASE_URL")
                token_json = _required("CONTROL_API_REVIEWER_TOKENS_JSON")
                engine = sa.create_engine(
                    database_url,
                    pool_pre_ping=True,
                    poolclass=NullPool,
                )
                service = ReviewService(
                    PostgresReviewRepository(engine),
                    clock=lambda: datetime.now(UTC),
                )

                def readiness() -> bool:
                    try:
                        with engine.connect() as connection:
                            ready = connection.execute(
                                sa.text(
                                    "SELECT "
                                    "to_regclass('review.review_cases') IS NOT NULL "
                                    "AND to_regclass('review.review_case_revisions') "
                                    "IS NOT NULL "
                                    "AND to_regclass('review.review_decisions') "
                                    "IS NOT NULL "
                                    "AND to_regclass('review.manual_observations') "
                                    "IS NOT NULL "
                                    "AND to_regclass('review.suppression_revisions') "
                                    "IS NOT NULL"
                                )
                            ).scalar_one()
                        return bool(ready)
                    except sa.exc.SQLAlchemyError:
                        return False

                return create_app(
                    service=service,
                    authenticator=TokenAuthenticator.from_json(token_json),
                    readiness_probe=readiness,
                )


            def main() -> None:
                import uvicorn

                uvicorn.run(
                    "control_api.main:app",
                    host="0.0.0.0",
                    port=8080,
                )


            def _required(name: str) -> str:
                value = os.getenv(name, "").strip()
                if not value:
                    raise RuntimeError(
                        f"required Control API setting {name} is missing"
                    )
                return value


            app = create_runtime_app()
            '''
        ),
        encoding="utf-8",
    )
    (ROOT / "apps/control_api/src/control_api/__main__.py").write_text(
        dedent(
            '''\
            from control_api.main import main

            if __name__ == "__main__":
                main()
            '''
        ),
        encoding="utf-8",
    )


def patch_contract_exports() -> None:
    contracts_init = (
        ROOT / "packages/review_contracts/src/review_contracts/__init__.py"
    )
    text = contracts_init.read_text(encoding="utf-8")
    text = text.replace(
        "    ReviewDecisionCommand,\n",
        "    ReviewDecisionCommand,\n    ReviewOutcome,\n",
        1,
    )
    text = text.replace(
        "    SuppressionCommand,\n",
        "    SuppressionCommand,\n"
        "    SuppressionScope,\n"
        "    SuppressionTargetKind,\n",
        1,
    )
    text = text.replace(
        '    "ReviewDecisionCommand",\n',
        '    "ReviewDecisionCommand",\n    "ReviewOutcome",\n',
        1,
    )
    text = text.replace(
        '    "SuppressionCommand",\n',
        '    "SuppressionCommand",\n'
        '    "SuppressionScope",\n'
        '    "SuppressionTargetKind",\n',
        1,
    )
    contracts_init.write_text(text, encoding="utf-8")

    application_init = (
        ROOT / "packages/review_application/src/review_application/__init__.py"
    )
    text = application_init.read_text(encoding="utf-8")
    text = text.replace(
        "    ReviewCaseDetail,\n",
        "    Permission,\n    ReviewCaseDetail,\n",
        1,
    )
    text = text.replace(
        '    "ReviewApplicationError",\n',
        '    "Permission",\n    "ReviewApplicationError",\n',
        1,
    )
    application_init.write_text(text, encoding="utf-8")


def patch_application_types() -> None:
    service = ROOT / "packages/review_application/src/review_application/service.py"
    text = service.read_text(encoding="utf-8")
    text = text.replace(
        "    ReviewDecisionCommand,\n",
        "    ReviewDecisionCommand,\n    ReviewOutcome,\n",
        1,
    )
    text = text.replace(
        "    SuppressionCommand,\n",
        "    SuppressionCommand,\n"
        "    SuppressionScope,\n"
        "    SuppressionTargetKind,\n",
        1,
    )
    text = text.replace(
        "        outcome: str,\n",
        "        outcome: ReviewOutcome,\n",
        1,
    )
    text = text.replace(
        "            outcome=outcome,  # type: ignore[arg-type]\n",
        "            outcome=outcome,\n",
        1,
    )
    text = text.replace(
        "        target_kind: str,\n",
        "        target_kind: SuppressionTargetKind,\n",
        1,
    )
    text = text.replace(
        "        scopes: tuple[str, ...],\n",
        "        scopes: tuple[SuppressionScope, ...],\n",
        1,
    )
    text = text.replace(
        "            target_kind=target_kind,  # type: ignore[arg-type]\n",
        "            target_kind=target_kind,\n",
        1,
    )
    text = text.replace(
        "            scopes=scopes,  # type: ignore[arg-type]\n",
        "            scopes=scopes,\n",
        1,
    )
    service.write_text(text, encoding="utf-8")


def patch_repository_types() -> None:
    path = ROOT / (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/review_repository.py"
    )
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "    SuppressionRevision,\n",
        "    SuppressionRevision,\n    SuppressionScope,\n",
        1,
    )
    start = text.index(
        "def _suppression(row: RowMapping) -> SuppressionRevision:\n"
    )
    end = text.index("\n\ndef _verify_decision_replay", start)
    replacement = dedent(
        '''\
        def _suppression(row: RowMapping) -> SuppressionRevision:
            scopes: list[SuppressionScope] = []
            if row["suppress_discovery"]:
                scopes.append("discovery")
            if row["suppress_export"]:
                scopes.append("export")
            if row["suppress_normalization"]:
                scopes.append("normalization")
            return SuppressionRevision(
                suppression_id=row["suppression_id"],
                revision=row["revision"],
                state=row["state"],
                target_kind=row["target_kind"],
                target_id=row["target_id"],
                scopes=tuple(scopes),
                reason_code=row["reason_code"],
                actor_id=row["actor_id"],
                evidence_reference=row["evidence_reference"],
                starts_at_utc=row["starts_at_utc"],
                expires_at_utc=row["expires_at_utc"],
                resolved_at_utc=row["resolved_at_utc"],
                command_digest=row["command_digest"],
                correlation_id=row["correlation_id"],
            )
        '''
    ).rstrip()
    path.write_text(text[:start] + replacement + text[end:], encoding="utf-8")


def patch_projects_and_architecture() -> None:
    rewrite_project_sources(
        ROOT / "packages/review_application/pyproject.toml",
        description=(
            "Review use cases and permission boundaries "
            "for the Data Collection Platform"
        ),
        sources=("review-contracts",),
        remove_dependencies=("review-core",),
    )
    control_project = ROOT / "apps/control_api/pyproject.toml"
    rewrite_project_sources(
        control_project,
        description=(
            "Authenticated operator Control API "
            "for the Data Collection Platform"
        ),
        sources=(
            "collection-infrastructure",
            "review-application",
            "review-contracts",
        ),
    )
    text = control_project.read_text(encoding="utf-8")
    text = text.replace(
        'control-api = "control_api.__main__:main"',
        'control-api = "control_api.main:main"',
        1,
    )
    control_project.write_text(text, encoding="utf-8")

    checker = ROOT / "tools/architecture_checks/check_dependencies.py"
    text = checker.read_text(encoding="utf-8")
    text = text.replace(
        'allowed_internal_imports=("review_contracts", "review_core"),',
        'allowed_internal_imports=("review_contracts",),',
        1,
    )
    checker.write_text(text, encoding="utf-8")

    policy = subprocess.check_output(
        [sys.executable, str(checker), "--print-policy"],
        text=True,
    ).strip()
    policy_path = ROOT / "docs/architecture/dependency-rules.md"
    text = policy_path.read_text(encoding="utf-8")
    start_marker = "<!-- dependency-policy:start -->"
    end_marker = "<!-- dependency-policy:end -->"
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    policy_path.write_text(
        text[:start] + policy + text[end:],
        encoding="utf-8",
    )


def patch_tests_and_docs() -> None:
    generator_init = ROOT / "tools/control_api_contract_generation/__init__.py"
    generator_init.write_text(
        "'Deterministic Control API contract generation tooling.'\n",
        encoding="utf-8",
    )

    app_test = ROOT / "apps/control_api/tests/test_app.py"
    text = app_test.read_text(encoding="utf-8")
    text += dedent(
        '''


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
        '''
    )
    app_test.write_text(text, encoding="utf-8")

    specification = ROOT / "docs/specifications/stage8b-control-api.md"
    text = specification.read_text(encoding="utf-8").rstrip()
    text += dedent(
        '''

        ## Authentication boundary

        The bearer credential is an internal reverse-proxy-to-Control-API
        capability. It is injected at runtime, never returned to or stored by
        the browser, and is not a replacement for the Stage 8C operator
        cookie/bootstrap boundary. Runtime OpenAPI is disabled; consumers use
        the checked-in generated contract.
        '''
    )
    specification.write_text(text + "\n", encoding="utf-8")


def main() -> int:
    patch_contract_exports()
    patch_application_types()
    patch_repository_types()
    patch_projects_and_architecture()
    write_canonical_auth()
    write_canonical_app()
    write_canonical_main()
    patch_tests_and_docs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
