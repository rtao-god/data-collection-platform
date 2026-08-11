from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from collection_application import (
    LeaseExpirySweep,
    LeaseHeartbeat,
    LeaseRequest,
    WorkCompletion,
    WorkEngineService,
    WorkerRegistration,
    WorkFailure,
    WorkRelease,
    WorkStage,
)
from collection_contracts import ErrorEnvelope, OwnerContextError
from worker_gateway.auth import (
    WorkerAuthenticationError,
    WorkerAuthenticator,
    WorkerPrincipal,
)
from worker_gateway.contracts import (
    HealthResponse,
    LeaseAcquiredResponse,
    LeaseAcquireRequest,
    LeaseAcquireResponse,
    LeaseHeartbeatRequest,
    NoEligibleWorkResponse,
    WorkCompletionRequest,
    WorkCompletionResponse,
    WorkerProtocolMetadataResponse,
    WorkerRegistrationRequest,
    WorkerRegistrationResponse,
    WorkFailureRequest,
    WorkLeaseResponse,
    WorkMutationResponse,
    WorkReleaseRequest,
)

_LOGGER = logging.getLogger("worker_gateway")
_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,99}$")
_WORKER_BEARER = HTTPBearer(auto_error=False, scheme_name="WorkerBearer")


@dataclass(frozen=True, slots=True)
class GatewayDependencies:
    work_engine: WorkEngineService
    authenticator: WorkerAuthenticator
    readiness_probe: Callable[[], None]
    expiry_interval_seconds: float = 5.0
    expiry_batch_size: int = 100

    def __post_init__(self) -> None:
        if self.expiry_interval_seconds < 0:
            raise ValueError("lease expiry interval cannot be negative")
        if not 1 <= self.expiry_batch_size <= 1_000:
            raise ValueError("lease expiry batch size must be between 1 and 1000")


@dataclass(slots=True)
class LeaseExpiryMonitor:
    last_success_at_utc: datetime | None = None
    last_error: ErrorEnvelope | None = None


class WorkerTransportError(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: dict[str, object],
        required_action: str,
    ) -> None:
        self.code = code
        self.message = message
        self.context = context
        self.required_action = required_action
        super().__init__(message)


class GatewayReadinessError(Exception):
    def __init__(self, *, code: str, message: str, context: dict[str, object]) -> None:
        self.code = code
        self.message = message
        self.context = context
        super().__init__(message)


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    dependencies = cast(GatewayDependencies | None, application.state.dependencies)
    stop = asyncio.Event()
    task: asyncio.Task[None] | None = None
    if dependencies is not None and dependencies.expiry_interval_seconds > 0:
        task = asyncio.create_task(
            _run_lease_expiry_loop(
                dependencies,
                cast(LeaseExpiryMonitor, application.state.expiry_monitor),
                stop,
            ),
            name="worker-gateway-lease-expiry",
        )
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            await task


async def _run_lease_expiry_loop(
    dependencies: GatewayDependencies,
    monitor: LeaseExpiryMonitor,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=dependencies.expiry_interval_seconds,
            )
        except TimeoutError:
            correlation_id = f"gateway-expiry-{uuid4()}"
            try:
                await asyncio.to_thread(
                    dependencies.work_engine.expire_leases,
                    LeaseExpirySweep(
                        limit=dependencies.expiry_batch_size,
                        correlation_id=correlation_id,
                    ),
                )
            except OwnerContextError as exc:
                monitor.last_error = exc.envelope
                _LOGGER.error(
                    "lease expiry sweep failed",
                    extra={
                        "owner": exc.envelope.owner,
                        "code": exc.envelope.code,
                        "correlation_id": exc.envelope.correlation_id,
                    },
                )
            except Exception as exc:
                monitor.last_error = ErrorEnvelope(
                    type="collection/worker-gateway-expiry-failed",
                    owner="WorkerGateway.Expiry",
                    code="WORKER_GATEWAY_EXPIRY_FAILED",
                    message="The lease expiry sweep failed unexpectedly.",
                    context={"causeType": type(exc).__name__},
                    required_action=(
                        "Inspect Worker Gateway logs and the Work Engine database before "
                        "resuming lease issuance."
                    ),
                    correlation_id=correlation_id,
                )
                _LOGGER.exception(
                    "lease expiry sweep raised an unexpected failure",
                    extra={"correlation_id": correlation_id},
                )
            else:
                monitor.last_success_at_utc = datetime.now(UTC)
                monitor.last_error = None


def create_app(dependencies: GatewayDependencies | None = None) -> FastAPI:
    application = FastAPI(
        title="Data Collection Platform Worker Gateway",
        version="1.0.0",
        openapi_version="3.1.0",
        lifespan=_lifespan,
    )
    application.state.dependencies = dependencies
    application.state.expiry_monitor = LeaseExpiryMonitor()
    _install_middleware(application)
    _install_exception_handlers(application)
    application.include_router(_worker_router())
    _install_health_routes(application)
    return application


def _install_middleware(application: FastAPI) -> None:
    @application.middleware("http")
    async def correlation_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        requested = request.headers.get("X-Correlation-Id")
        correlation_id = (
            requested
            if requested is not None and _CORRELATION_PATTERN.fullmatch(requested) is not None
            else str(uuid4())
        )
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = correlation_id
        return response


def _install_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(OwnerContextError)
    async def owner_context_handler(request: Request, exc: OwnerContextError) -> JSONResponse:
        del request
        return _error_response(_owner_status(exc.envelope.code), exc.envelope)

    @application.exception_handler(WorkerAuthenticationError)
    async def authentication_handler(
        request: Request,
        exc: WorkerAuthenticationError,
    ) -> JSONResponse:
        return _error_response(
            exc.status_code,
            ErrorEnvelope(
                type="collection/worker-authentication-failed",
                owner="WorkerGateway.Authentication",
                code=exc.code,
                message=exc.message,
                context=exc.context,
                required_action=exc.required_action,
                correlation_id=_correlation_id(request),
            ),
        )

    @application.exception_handler(WorkerTransportError)
    async def transport_handler(request: Request, exc: WorkerTransportError) -> JSONResponse:
        return _error_response(
            422,
            ErrorEnvelope(
                type="collection/worker-request-invalid",
                owner="WorkerGateway.Transport",
                code=exc.code,
                message=exc.message,
                context=exc.context,
                required_action=exc.required_action,
                correlation_id=_correlation_id(request),
            ),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            422,
            ErrorEnvelope(
                type="collection/worker-request-invalid",
                owner="WorkerGateway.Transport",
                code="WORKER_REQUEST_INVALID",
                message="The worker request does not satisfy the transport contract.",
                context={"errors": jsonable_encoder(exc.errors())},
                required_action="Correct the request using the published Worker Gateway contract.",
                correlation_id=_correlation_id(request),
            ),
        )

    @application.exception_handler(GatewayReadinessError)
    async def readiness_handler(request: Request, exc: GatewayReadinessError) -> JSONResponse:
        return _error_response(
            503,
            ErrorEnvelope(
                type="collection/worker-gateway-not-ready",
                owner="WorkerGateway.Readiness",
                code=exc.code,
                message=exc.message,
                context=exc.context,
                required_action=(
                    "Restore the failed owner dependency and verify readiness before issuing work."
                ),
                correlation_id=_correlation_id(request),
            ),
        )

    @application.exception_handler(Exception)
    async def unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = _correlation_id(request)
        _LOGGER.exception(
            "worker gateway request failed unexpectedly",
            extra={"correlation_id": correlation_id},
        )
        return _error_response(
            500,
            ErrorEnvelope(
                type="collection/worker-gateway-unexpected",
                owner="WorkerGateway",
                code="WORKER_GATEWAY_UNEXPECTED",
                message="The Worker Gateway failed outside a modeled owner result.",
                context={"causeType": type(exc).__name__},
                required_action=(
                    "Inspect the correlated server log and add or restore the missing owner "
                    "failure contract before retrying."
                ),
                correlation_id=correlation_id,
            ),
        )


def _worker_router() -> APIRouter:
    router = APIRouter(prefix="/worker", tags=["worker"])

    @router.post(
        "/registrations",
        response_model=WorkerRegistrationResponse,
        operation_id="registerWorker",
    )
    def register_worker(
        payload: WorkerRegistrationRequest,
        request: Request,
        principal: Annotated[WorkerPrincipal, Depends(_authenticate_worker)],
    ) -> WorkerRegistrationResponse:
        dependencies = _dependencies(request)
        dependencies.authenticator.require_registration_scope(
            principal,
            payload.capabilities,
        )
        command = _command(
            lambda: WorkerRegistration(
                worker_id=principal.worker_id,
                build_identity=payload.build_identity,
                capabilities=payload.capabilities,
                supported_output_contracts=payload.supported_output_contracts,
                max_concurrency=payload.max_concurrency,
                resource_profile=payload.resource_profile,
                correlation_id=_correlation_id(request),
            )
        )
        return WorkerRegistrationResponse.from_result(
            dependencies.work_engine.register_worker(command)
        )

    @router.post(
        "/leases/acquire",
        response_model=LeaseAcquireResponse,
        operation_id="acquireWorkerLease",
    )
    def acquire_lease(
        payload: LeaseAcquireRequest,
        request: Request,
        principal: Annotated[WorkerPrincipal, Depends(_authenticate_worker)],
    ) -> LeaseAcquireResponse:
        dependencies = _dependencies(request)
        dependencies.authenticator.require_capability(principal, payload.capability)
        command = _command(
            lambda: LeaseRequest(
                worker_id=principal.worker_id,
                capability=payload.capability,
                lease_duration_seconds=payload.lease_duration_seconds,
                heartbeat_interval_seconds=payload.heartbeat_interval_seconds,
                correlation_id=_correlation_id(request),
            )
        )
        lease = dependencies.work_engine.acquire_lease(command)
        if lease is None:
            return NoEligibleWorkResponse(capability=payload.capability)
        return LeaseAcquiredResponse(lease=WorkLeaseResponse.from_domain(lease))

    @router.post(
        "/leases/{lease_id}/heartbeat",
        response_model=WorkLeaseResponse,
        operation_id="heartbeatWorkerLease",
    )
    def heartbeat_lease(
        lease_id: UUID,
        payload: LeaseHeartbeatRequest,
        request: Request,
        principal: Annotated[WorkerPrincipal, Depends(_authenticate_worker)],
    ) -> WorkLeaseResponse:
        command = _command(
            lambda: LeaseHeartbeat(
                work_id=payload.work_id,
                lease_id=lease_id,
                lease_token=payload.lease_token,
                worker_id=principal.worker_id,
                input_digest=payload.input_digest,
                lease_duration_seconds=payload.lease_duration_seconds,
                heartbeat_interval_seconds=payload.heartbeat_interval_seconds,
                correlation_id=_correlation_id(request),
            )
        )
        return WorkLeaseResponse.from_domain(_dependencies(request).work_engine.heartbeat(command))

    @router.post(
        "/work/{work_id}/complete",
        response_model=WorkCompletionResponse,
        operation_id="completeWorkerWork",
    )
    def complete_work(
        work_id: UUID,
        payload: WorkCompletionRequest,
        request: Request,
        principal: Annotated[WorkerPrincipal, Depends(_authenticate_worker)],
    ) -> WorkCompletionResponse:
        command = _command(
            lambda: WorkCompletion(
                work_id=work_id,
                lease_id=payload.lease_id,
                lease_token=payload.lease_token,
                worker_id=principal.worker_id,
                input_digest=payload.input_digest,
                output_contract=payload.output_contract,
                output_digest=payload.output_digest,
                worker_build_identity=payload.worker_build_identity,
                correlation_id=_correlation_id(request),
            )
        )
        return WorkCompletionResponse.from_result(
            _dependencies(request).work_engine.complete(command)
        )

    @router.post(
        "/work/{work_id}/fail",
        response_model=WorkMutationResponse,
        operation_id="failWorkerWork",
    )
    def fail_work(
        work_id: UUID,
        payload: WorkFailureRequest,
        request: Request,
        principal: Annotated[WorkerPrincipal, Depends(_authenticate_worker)],
    ) -> WorkMutationResponse:
        command = _command(
            lambda: WorkFailure(
                work_id=work_id,
                lease_id=payload.lease_id,
                lease_token=payload.lease_token,
                worker_id=principal.worker_id,
                input_digest=payload.input_digest,
                failure_kind=payload.failure_kind,
                code=payload.code,
                owner=payload.owner,
                message=payload.message,
                required_action=payload.required_action,
                worker_build_identity=payload.worker_build_identity,
                correlation_id=_correlation_id(request),
            )
        )
        return WorkMutationResponse.from_result(_dependencies(request).work_engine.fail(command))

    @router.post(
        "/work/{work_id}/release",
        response_model=WorkMutationResponse,
        operation_id="releaseWorkerWork",
    )
    def release_work(
        work_id: UUID,
        payload: WorkReleaseRequest,
        request: Request,
        principal: Annotated[WorkerPrincipal, Depends(_authenticate_worker)],
    ) -> WorkMutationResponse:
        command = _command(
            lambda: WorkRelease(
                work_id=work_id,
                lease_id=payload.lease_id,
                lease_token=payload.lease_token,
                worker_id=principal.worker_id,
                input_digest=payload.input_digest,
                reason_code=payload.reason_code,
                worker_build_identity=payload.worker_build_identity,
                correlation_id=_correlation_id(request),
            )
        )
        return WorkMutationResponse.from_result(_dependencies(request).work_engine.release(command))

    @router.get(
        "/capabilities",
        response_model=WorkerProtocolMetadataResponse,
        operation_id="getWorkerProtocolMetadata",
    )
    def protocol_metadata(
        principal: Annotated[WorkerPrincipal, Depends(_authenticate_worker)],
    ) -> WorkerProtocolMetadataResponse:
        return WorkerProtocolMetadataResponse(
            worker_id=principal.worker_id,
            authorized_capabilities=tuple(
                sorted(principal.capabilities, key=lambda value: value.value)
            ),
            supported_stages=tuple(WorkStage),
        )

    return router


def _install_health_routes(application: FastAPI) -> None:
    @application.get(
        "/health/live",
        response_model=HealthResponse,
        operation_id="getWorkerGatewayLiveness",
    )
    def liveness() -> HealthResponse:
        return HealthResponse(status="live")

    @application.get(
        "/health/ready",
        response_model=HealthResponse,
        operation_id="getWorkerGatewayReadiness",
    )
    def readiness(request: Request) -> HealthResponse:
        dependencies = _dependencies(request)
        try:
            dependencies.readiness_probe()
        except Exception as exc:
            raise GatewayReadinessError(
                code="WORKER_GATEWAY_DEPENDENCY_UNAVAILABLE",
                message="A required Worker Gateway dependency is unavailable.",
                context={"causeType": type(exc).__name__},
            ) from exc
        monitor = cast(LeaseExpiryMonitor, request.app.state.expiry_monitor)
        if monitor.last_error is not None:
            raise GatewayReadinessError(
                code="WORKER_GATEWAY_EXPIRY_DEGRADED",
                message="Lease expiry processing has not recovered from its latest failure.",
                context={
                    "failureOwner": monitor.last_error.owner,
                    "failureCode": monitor.last_error.code,
                    "failureCorrelationId": monitor.last_error.correlation_id,
                },
            )
        return HealthResponse(status="ready")


def _authenticate_worker(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_WORKER_BEARER),
    ],
) -> WorkerPrincipal:
    authorization = (
        f"{credentials.scheme} {credentials.credentials}" if credentials is not None else None
    )
    return _dependencies(request).authenticator.authenticate(authorization)


def _dependencies(request: Request) -> GatewayDependencies:
    dependencies = cast(GatewayDependencies | None, request.app.state.dependencies)
    if dependencies is None:
        raise GatewayReadinessError(
            code="WORKER_GATEWAY_NOT_CONFIGURED",
            message="Worker Gateway runtime dependencies are not configured.",
            context={},
        )
    return dependencies


def _correlation_id(request: Request) -> str:
    value = getattr(request.state, "correlation_id", None)
    if not isinstance(value, str):
        raise RuntimeError("correlation middleware did not establish a correlation ID")
    return value


def _command[CommandT](factory: Callable[[], CommandT]) -> CommandT:
    try:
        return factory()
    except ValueError as exc:
        raise WorkerTransportError(
            code="WORKER_COMMAND_INVALID",
            message="The worker request cannot create a valid Work Engine command.",
            context={"detail": str(exc)},
            required_action="Correct the request using the published Worker Gateway contract.",
        ) from exc


def _owner_status(code: str) -> int:
    if code in {
        "RUN_CONFIG_NOT_FOUND",
        "STAGE_RUN_OWNER_NOT_FOUND",
        "WORK_NOT_FOUND",
        "WORK_SOURCE_NOT_CONFIGURED",
        "WORKER_NOT_REGISTERED",
    }:
        return 404
    if code in {
        "SOURCE_CAPACITY_CORRUPT",
        "WORKER_CAPACITY_CORRUPT",
        "WORKER_HEARTBEAT_MISSING",
        "WORK_ATTEMPT_MISSING",
        "WORK_ENGINE_STATE_INVALID",
        "WORK_ENGINE_STORAGE_FAILED",
    }:
        return 503
    return 409


def _error_response(status_code: int, envelope: ErrorEnvelope) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(envelope.model_dump(by_alias=True)),
    )
