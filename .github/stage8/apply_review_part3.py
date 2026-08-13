from __future__ import annotations

from pathlib import Path
from textwrap import dedent

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(content).lstrip(), encoding="utf-8")


infra_project = ROOT / "packages/review_infrastructure/pyproject.toml"
infra_text = infra_project.read_text(encoding="utf-8")
if '"psycopg[binary]==3.3.4"' not in infra_text:
    infra_text = infra_text.replace(
        '  "boto3==1.43.54",\n',
        '  "boto3==1.43.54",\n  "psycopg[binary]==3.3.4",\n',
        1,
    )
infra_project.write_text(infra_text, encoding="utf-8")

write(
    "apps/control_api/src/control_api/settings.py",
    r'''
    from __future__ import annotations

    import os
    from dataclasses import dataclass


    @dataclass(frozen=True, slots=True)
    class ControlApiSettings:
        database_url: str
        s3_endpoint_url: str
        s3_bucket: str
        s3_access_key_id: str
        s3_secret_access_key: str
        s3_region: str
        internal_review_key: str
        maximum_evidence_preview_bytes: int = 256 * 1024
        allowed_origin: str | None = None
        host: str = "0.0.0.0"
        port: int = 8080

        @classmethod
        def from_environment(cls) -> ControlApiSettings:
            maximum = int(
                os.getenv(
                    "COLLECTOR_REVIEW_MAXIMUM_EVIDENCE_PREVIEW_BYTES",
                    str(256 * 1024),
                )
            )
            if not 1 <= maximum <= 256 * 1024:
                raise ValueError("configured evidence preview limit is invalid")
            port = int(os.getenv("COLLECTOR_CONTROL_API_PORT", "8080"))
            if not 1 <= port <= 65535:
                raise ValueError("configured Control API port is invalid")
            return cls(
                database_url=_required("COLLECTOR_DATABASE_URL"),
                s3_endpoint_url=_required("COLLECTOR_S3_ENDPOINT_URL"),
                s3_bucket=_required("COLLECTOR_S3_BUCKET"),
                s3_access_key_id=_required("COLLECTOR_S3_ACCESS_KEY_ID"),
                s3_secret_access_key=_required("COLLECTOR_S3_SECRET_ACCESS_KEY"),
                s3_region=_required("COLLECTOR_S3_REGION"),
                internal_review_key=_required("COLLECTOR_REVIEW_INTERNAL_KEY"),
                maximum_evidence_preview_bytes=maximum,
                allowed_origin=_optional("COLLECTOR_REVIEW_ALLOWED_ORIGIN"),
                host=os.getenv("COLLECTOR_CONTROL_API_HOST", "0.0.0.0"),
                port=port,
            )


    def _required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise RuntimeError(f"required Control API setting {name} is missing")
        return value


    def _optional(name: str) -> str | None:
        value = os.getenv(name, "").strip()
        return value or None
    ''',
)

write(
    "apps/control_api/src/control_api/app.py",
    r'''
    from __future__ import annotations

    import hmac
    from collections.abc import Callable
    from typing import Annotated, Protocol
    from uuid import UUID

    from fastapi import FastAPI, Header, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from review_application import (
        EvidenceNotFound,
        ReviewApplicationError,
        ReviewConflict,
        ReviewDependencyUnavailable,
        ReviewItemNotFound,
        StaleReviewRevision,
    )
    from review_contracts import (
        DecisionCommand,
        EvidencePreview,
        ManualObservationCommand,
        ProblemDetail,
        ReviewAdmissionRequest,
        ReviewItemDetail,
        ReviewItemKind,
        ReviewItemState,
        ReviewMutationResult,
        ReviewQueuePage,
        SuppressionCommand,
    )


    class ReviewOperations(Protocol):
        def admit(
            self,
            request: ReviewAdmissionRequest,
            *,
            actor_reference: str,
            correlation_id: str,
        ) -> ReviewItemDetail: ...

        def list_queue(
            self,
            *,
            state: ReviewItemState | None = "pending",
            item_kind: ReviewItemKind | None = None,
            campaign_key: str | None = None,
            cursor: str | None = None,
            limit: int = 50,
        ) -> ReviewQueuePage: ...

        def get_detail(self, review_item_id: UUID) -> ReviewItemDetail: ...

        def decide(
            self,
            review_item_id: UUID,
            command: DecisionCommand,
            *,
            actor_reference: str,
            correlation_id: str,
        ) -> ReviewMutationResult: ...

        def add_manual_observation(
            self,
            review_item_id: UUID,
            command: ManualObservationCommand,
            *,
            actor_reference: str,
            correlation_id: str,
        ) -> ReviewMutationResult: ...

        def change_suppression(
            self,
            review_item_id: UUID,
            command: SuppressionCommand,
            *,
            actor_reference: str,
            correlation_id: str,
        ) -> ReviewMutationResult: ...

        def preview_evidence(
            self,
            artifact_id: UUID,
            *,
            maximum_bytes: int,
        ) -> EvidencePreview: ...


    ActorHeader = Annotated[str | None, Header(alias="X-Review-Actor")]
    CorrelationHeader = Annotated[str | None, Header(alias="X-Correlation-Id")]
    InternalKeyHeader = Annotated[str | None, Header(alias="X-Review-Internal-Key")]


    def create_app(
        service: ReviewOperations,
        *,
        internal_review_key: str,
        allowed_origin: str | None = None,
    ) -> FastAPI:
        if not internal_review_key:
            raise ValueError("internal review key is required")
        app = FastAPI(title="Data Collection Platform Control API", version="1")
        if allowed_origin is not None:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=[allowed_origin],
                allow_credentials=False,
                allow_methods=["GET", "POST"],
                allow_headers=[
                    "Content-Type",
                    "X-Review-Actor",
                    "X-Correlation-Id",
                    "X-Review-Internal-Key",
                ],
            )

        @app.exception_handler(ReviewItemNotFound)
        async def _item_not_found(
            request: Request,
            error: ReviewItemNotFound,
        ) -> JSONResponse:
            del request
            return _problem(
                status=404,
                code=error.code,
                title="Review item not found",
                detail=str(error),
            )

        @app.exception_handler(EvidenceNotFound)
        async def _evidence_not_found(
            request: Request,
            error: EvidenceNotFound,
        ) -> JSONResponse:
            del request
            return _problem(
                status=404,
                code=error.code,
                title="Evidence not found",
                detail=str(error),
            )

        @app.exception_handler(StaleReviewRevision)
        async def _stale_revision(
            request: Request,
            error: StaleReviewRevision,
        ) -> JSONResponse:
            return _problem(
                status=409,
                code=error.code,
                title="Review revision is stale",
                detail="Reload the review item before submitting another command.",
                current_revision=error.current_revision,
                correlation_id=request.headers.get("X-Correlation-Id"),
            )

        @app.exception_handler(ReviewConflict)
        async def _review_conflict(
            request: Request,
            error: ReviewConflict,
        ) -> JSONResponse:
            return _problem(
                status=409,
                code=error.code,
                title="Review command conflict",
                detail=str(error),
                correlation_id=request.headers.get("X-Correlation-Id"),
            )

        @app.exception_handler(ReviewDependencyUnavailable)
        async def _dependency_unavailable(
            request: Request,
            error: ReviewDependencyUnavailable,
        ) -> JSONResponse:
            return _problem(
                status=503,
                code=error.code,
                title="Review dependency unavailable",
                detail=str(error),
                correlation_id=request.headers.get("X-Correlation-Id"),
            )

        @app.exception_handler(ValueError)
        async def _invalid_request(request: Request, error: ValueError) -> JSONResponse:
            return _problem(
                status=400,
                code="REVIEW_REQUEST_INVALID",
                title="Review request is invalid",
                detail=str(error),
                correlation_id=request.headers.get("X-Correlation-Id"),
            )

        @app.exception_handler(ReviewApplicationError)
        async def _application_error(
            request: Request,
            error: ReviewApplicationError,
        ) -> JSONResponse:
            return _problem(
                status=500,
                code=error.code,
                title="Review operation failed",
                detail="The review operation could not be completed.",
                correlation_id=request.headers.get("X-Correlation-Id"),
            )

        @app.get("/health")
        def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.post(
            "/v1/internal/review/admissions",
            response_model=ReviewItemDetail,
            status_code=201,
        )
        def admit(
            request: ReviewAdmissionRequest,
            actor: ActorHeader = None,
            correlation_id: CorrelationHeader = None,
            internal_key: InternalKeyHeader = None,
        ) -> ReviewItemDetail | JSONResponse:
            authorization = _mutation_headers(actor, correlation_id)
            if isinstance(authorization, JSONResponse):
                return authorization
            if internal_key is None or not hmac.compare_digest(
                internal_key,
                internal_review_key,
            ):
                return _problem(
                    status=403,
                    code="REVIEW_INTERNAL_KEY_INVALID",
                    title="Internal review authorization failed",
                    detail="The internal admission credential is invalid.",
                    correlation_id=authorization[1],
                )
            return service.admit(
                request,
                actor_reference=authorization[0],
                correlation_id=authorization[1],
            )

        @app.get("/v1/review/queue", response_model=ReviewQueuePage)
        def list_queue(
            state: Annotated[ReviewItemState | None, Query()] = "pending",
            item_kind: Annotated[ReviewItemKind | None, Query(alias="itemKind")] = None,
            campaign_key: Annotated[str | None, Query(alias="campaignKey")] = None,
            cursor: Annotated[str | None, Query()] = None,
            limit: Annotated[int, Query(ge=1, le=200)] = 50,
        ) -> ReviewQueuePage:
            return service.list_queue(
                state=state,
                item_kind=item_kind,
                campaign_key=campaign_key,
                cursor=cursor,
                limit=limit,
            )

        @app.get(
            "/v1/review/items/{review_item_id}",
            response_model=ReviewItemDetail,
        )
        def get_detail(review_item_id: UUID) -> ReviewItemDetail:
            return service.get_detail(review_item_id)

        @app.post(
            "/v1/review/items/{review_item_id}/decisions",
            response_model=ReviewMutationResult,
        )
        def decide(
            review_item_id: UUID,
            command: DecisionCommand,
            actor: ActorHeader = None,
            correlation_id: CorrelationHeader = None,
        ) -> ReviewMutationResult | JSONResponse:
            authorization = _mutation_headers(actor, correlation_id)
            if isinstance(authorization, JSONResponse):
                return authorization
            return service.decide(
                review_item_id,
                command,
                actor_reference=authorization[0],
                correlation_id=authorization[1],
            )

        @app.post(
            "/v1/review/items/{review_item_id}/manual-observations",
            response_model=ReviewMutationResult,
        )
        def add_manual_observation(
            review_item_id: UUID,
            command: ManualObservationCommand,
            actor: ActorHeader = None,
            correlation_id: CorrelationHeader = None,
        ) -> ReviewMutationResult | JSONResponse:
            authorization = _mutation_headers(actor, correlation_id)
            if isinstance(authorization, JSONResponse):
                return authorization
            return service.add_manual_observation(
                review_item_id,
                command,
                actor_reference=authorization[0],
                correlation_id=authorization[1],
            )

        @app.post(
            "/v1/review/items/{review_item_id}/suppressions",
            response_model=ReviewMutationResult,
        )
        def change_suppression(
            review_item_id: UUID,
            command: SuppressionCommand,
            actor: ActorHeader = None,
            correlation_id: CorrelationHeader = None,
        ) -> ReviewMutationResult | JSONResponse:
            authorization = _mutation_headers(actor, correlation_id)
            if isinstance(authorization, JSONResponse):
                return authorization
            return service.change_suppression(
                review_item_id,
                command,
                actor_reference=authorization[0],
                correlation_id=authorization[1],
            )

        @app.get(
            "/v1/review/evidence/{artifact_id}/preview",
            response_model=EvidencePreview,
        )
        def preview_evidence(
            artifact_id: UUID,
            maximum_bytes: Annotated[
                int,
                Query(alias="maximumBytes", ge=1, le=256 * 1024),
            ] = 64 * 1024,
        ) -> EvidencePreview:
            return service.preview_evidence(
                artifact_id,
                maximum_bytes=maximum_bytes,
            )

        return app


    def _mutation_headers(
        actor: str | None,
        correlation_id: str | None,
    ) -> tuple[str, str] | JSONResponse:
        if actor is None or not actor.strip():
            return _problem(
                status=401,
                code="REVIEW_ACTOR_REQUIRED",
                title="Review actor is required",
                detail="Set X-Review-Actor before submitting a mutation.",
                correlation_id=correlation_id,
            )
        if correlation_id is None or not correlation_id.strip():
            return _problem(
                status=400,
                code="REVIEW_CORRELATION_REQUIRED",
                title="Correlation ID is required",
                detail="Set X-Correlation-Id before submitting a mutation.",
            )
        return actor.strip(), correlation_id.strip()


    def _problem(
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        current_revision: int | None = None,
        correlation_id: str | None = None,
    ) -> JSONResponse:
        problem = ProblemDetail(
            type=f"urn:data-collection-platform:{code.lower()}",
            title=title,
            status=status,
            code=code,
            detail=detail,
            currentRevision=current_revision,
            correlationId=correlation_id,
        )
        return JSONResponse(
            status_code=status,
            content=problem.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
            media_type="application/problem+json",
        )
    ''',
)

write(
    "apps/control_api/src/control_api/composition.py",
    r'''
    from __future__ import annotations

    import sqlalchemy as sa
    from fastapi import FastAPI
    from review_application import ReviewService
    from review_infrastructure import PostgresReviewRepository, S3EvidenceReader

    from control_api.app import create_app
    from control_api.settings import ControlApiSettings


    def create_default_app(settings: ControlApiSettings | None = None) -> FastAPI:
        resolved = settings or ControlApiSettings.from_environment()
        engine = sa.create_engine(
            resolved.database_url,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        repository = PostgresReviewRepository(engine)
        evidence_reader = S3EvidenceReader(
            bucket=resolved.s3_bucket,
            endpoint_url=resolved.s3_endpoint_url,
            access_key_id=resolved.s3_access_key_id,
            secret_access_key=resolved.s3_secret_access_key,
            region=resolved.s3_region,
        )
        service = ReviewService(
            repository,
            evidence_reader,
            maximum_evidence_preview_bytes=resolved.maximum_evidence_preview_bytes,
        )
        return create_app(
            service,
            internal_review_key=resolved.internal_review_key,
            allowed_origin=resolved.allowed_origin,
        )
    ''',
)

write(
    "apps/control_api/src/control_api/main.py",
    r'''
    from __future__ import annotations

    import uvicorn

    from control_api.composition import create_default_app
    from control_api.settings import ControlApiSettings


    def main() -> None:
        settings = ControlApiSettings.from_environment()
        uvicorn.run(
            create_default_app(settings),
            host=settings.host,
            port=settings.port,
            log_config=None,
        )
    ''',
)

write(
    "apps/control_api/src/control_api/__init__.py",
    r'''
    from control_api.app import ReviewOperations, create_app
    from control_api.composition import create_default_app
    from control_api.settings import ControlApiSettings

    __all__ = [
        "ControlApiSettings",
        "ReviewOperations",
        "create_app",
        "create_default_app",
    ]
    ''',
)

write(
    "apps/control_api/tests/test_app.py",
    r'''
    from __future__ import annotations

    from datetime import UTC, datetime
    from typing import cast
    from uuid import UUID

    from control_api import ReviewOperations, create_app
    from fastapi.testclient import TestClient
    from review_application import StaleReviewRevision
    from review_contracts import (
        AuditEvent,
        DecisionCommand,
        EvidencePreview,
        ManualObservationCommand,
        ReviewAdmissionRequest,
        ReviewItemDetail,
        ReviewItemSummary,
        ReviewMutationResult,
        ReviewQueuePage,
        SuppressionCommand,
    )

    ITEM_ID = UUID("00000000-0000-5000-8000-000000000801")
    ARTIFACT_ID = UUID("00000000-0000-5000-8000-000000000802")
    NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


    def _summary(revision: int = 0) -> ReviewItemSummary:
        return ReviewItemSummary(
            reviewItemId=ITEM_ID,
            campaignKey="berlin_recording_services",
            itemKind="resolution_pair",
            subjectId="pair-1",
            sourceSnapshotContract="entity-resolution-snapshot-v1",
            sourceSnapshotDigest="sha256:" + "a" * 64,
            payloadDigest="sha256:" + "b" * 64,
            state="pending" if revision == 0 else "decided",
            revision=revision,
            createdAtUtc=NOW,
            updatedAtUtc=NOW,
        )


    def _detail() -> ReviewItemDetail:
        return ReviewItemDetail(
            item=_summary(),
            payload={"unsafeHtml": "<script>alert('x')</script>"},
            evidenceBindings=(),
            decisions=(),
            manualObservations=(),
            suppressions=(),
            auditEvents=(
                AuditEvent(
                    auditEventId=UUID("00000000-0000-5000-8000-000000000803"),
                    reviewItemId=ITEM_ID,
                    commandKind="admit",
                    commandId=ITEM_ID,
                    resultingRevision=0,
                    actorReference="system",
                    payloadDigest="sha256:" + "c" * 64,
                    occurredAtUtc=NOW,
                    correlationId="correlation-admit",
                ),
            ),
        )


    class _Service:
        def __init__(self, *, stale: bool = False) -> None:
            self.stale = stale

        def admit(
            self,
            request: ReviewAdmissionRequest,
            *,
            actor_reference: str,
            correlation_id: str,
        ) -> ReviewItemDetail:
            del request, actor_reference, correlation_id
            return _detail()

        def list_queue(self, **_: object) -> ReviewQueuePage:
            return ReviewQueuePage(items=(_summary(),), nextCursor=None)

        def get_detail(self, review_item_id: UUID) -> ReviewItemDetail:
            assert review_item_id == ITEM_ID
            return _detail()

        def decide(
            self,
            review_item_id: UUID,
            command: DecisionCommand,
            *,
            actor_reference: str,
            correlation_id: str,
        ) -> ReviewMutationResult:
            del command, actor_reference, correlation_id
            if self.stale:
                raise StaleReviewRevision(review_item_id, 4)
            return ReviewMutationResult(
                commandId=UUID("00000000-0000-5000-8000-000000000804"),
                resultingRevision=1,
                item=_summary(1),
            )

        def add_manual_observation(
            self,
            review_item_id: UUID,
            command: ManualObservationCommand,
            *,
            actor_reference: str,
            correlation_id: str,
        ) -> ReviewMutationResult:
            del review_item_id, command, actor_reference, correlation_id
            raise AssertionError("not used")

        def change_suppression(
            self,
            review_item_id: UUID,
            command: SuppressionCommand,
            *,
            actor_reference: str,
            correlation_id: str,
        ) -> ReviewMutationResult:
            del review_item_id, command, actor_reference, correlation_id
            raise AssertionError("not used")

        def preview_evidence(
            self,
            artifact_id: UUID,
            *,
            maximum_bytes: int,
        ) -> EvidencePreview:
            assert artifact_id == ARTIFACT_ID
            return EvidencePreview(
                artifactId=artifact_id,
                contentDigest="sha256:" + "d" * 64,
                contentType="text/html",
                sizeBytes=64,
                requestedMaximumBytes=maximum_bytes,
                returnedBytes=29,
                truncated=True,
                text="<script>alert('x')</script>",
            )


    def _client(*, stale: bool = False) -> TestClient:
        app = create_app(
            cast(ReviewOperations, _Service(stale=stale)),
            internal_review_key="internal-secret",
        )
        return TestClient(app, raise_server_exceptions=False)


    def test_queue_and_detail_return_explicit_contracts() -> None:
        client = _client()

        queue = client.get("/v1/review/queue")
        detail = client.get(f"/v1/review/items/{ITEM_ID}")

        assert queue.status_code == 200
        assert queue.json()["items"][0]["state"] == "pending"
        assert detail.status_code == 200
        assert detail.json()["payload"]["unsafeHtml"] == "<script>alert('x')</script>"
        assert detail.headers["content-type"].startswith("application/json")


    def test_stale_decision_returns_409_with_current_revision() -> None:
        response = _client(stale=True).post(
            f"/v1/review/items/{ITEM_ID}/decisions",
            headers={
                "X-Review-Actor": "reviewer-1",
                "X-Correlation-Id": "correlation-decision",
            },
            json={
                "commandId": "00000000-0000-5000-8000-000000000805",
                "expectedRevision": 0,
                "action": "match",
                "reasonCode": "CONFIRMED_MATCH",
            },
        )

        assert response.status_code == 409
        assert response.json()["code"] == "REVIEW_REVISION_STALE"
        assert response.json()["currentRevision"] == 4


    def test_mutation_requires_actor_and_correlation_headers() -> None:
        response = _client().post(
            f"/v1/review/items/{ITEM_ID}/decisions",
            json={
                "commandId": "00000000-0000-5000-8000-000000000806",
                "expectedRevision": 0,
                "action": "match",
                "reasonCode": "CONFIRMED_MATCH",
            },
        )

        assert response.status_code == 401
        assert response.json()["code"] == "REVIEW_ACTOR_REQUIRED"


    def test_internal_admission_rejects_invalid_key() -> None:
        response = _client().post(
            "/v1/internal/review/admissions",
            headers={
                "X-Review-Actor": "resolution-admission",
                "X-Correlation-Id": "correlation-admission",
                "X-Review-Internal-Key": "wrong",
            },
            json={
                "contract": "review-admission",
                "contractRevision": "review-admission-v1",
                "sourceSnapshotContract": "entity-resolution-snapshot-v1",
                "sourceSnapshotDigest": "sha256:" + "a" * 64,
                "campaignKey": "berlin_recording_services",
                "itemKind": "resolution_pair",
                "subjectId": "pair-1",
                "payload": {},
                "evidenceBindings": [],
            },
        )

        assert response.status_code == 403
        assert response.json()["code"] == "REVIEW_INTERNAL_KEY_INVALID"


    def test_evidence_preview_is_json_plain_text_not_html_response() -> None:
        response = _client().get(
            f"/v1/review/evidence/{ARTIFACT_ID}/preview?maximumBytes=64"
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["text"] == "<script>alert('x')</script>"
        assert response.json()["truncated"] is True
    ''',
)

write(
    "apps/control_api/README.md",
    r'''
    # Control API

    `control_api` is the internal HTTP composition root for the review system.

    It maps typed application failures to bounded HTTP problem responses, requires explicit actor and correlation headers for every mutation, protects internal admission with a configured credential, and returns evidence only as bounded JSON text. It does not implement review state transitions, query PostgreSQL from route handlers, expose Object Store credentials, or return executable HTML.

    The owner contract is `docs/specifications/stage-8-review-console-v1.md`.
    ''',
)

write(
    "deploy/docker/control-api.Dockerfile",
    r'''
    FROM python:3.13.14-slim AS build

    ENV UV_COMPILE_BYTECODE=1 \
        UV_LINK_MODE=copy

    WORKDIR /workspace

    RUN pip install --no-cache-dir uv==0.10.0

    COPY .python-version pyproject.toml uv.lock ./
    COPY apps/control_api ./apps/control_api
    COPY packages/review_application ./packages/review_application
    COPY packages/review_contracts ./packages/review_contracts
    COPY packages/review_infrastructure ./packages/review_infrastructure

    RUN uv sync \
        --frozen \
        --no-dev \
        --no-editable \
        --package control-api

    FROM python:3.13.14-slim AS runtime

    ENV PATH="/workspace/.venv/bin:${PATH}" \
        PYTHONDONTWRITEBYTECODE=1 \
        PYTHONUNBUFFERED=1

    RUN groupadd --gid 10001 control-api \
        && useradd --uid 10001 --gid control-api --create-home control-api

    WORKDIR /workspace
    COPY --from=build --chown=control-api:control-api /workspace/.venv /workspace/.venv

    USER 10001:10001
    EXPOSE 8080

    ENTRYPOINT ["control-api"]
    ''',
)
