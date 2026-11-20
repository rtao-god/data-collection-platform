from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
import uvicorn
from fastapi import FastAPI
from sqlalchemy.engine import Engine

from collection_application import ArtifactTransferService, WorkEngineService
from collection_application.pipeline_advancement import (
    TRANSITION_RESULT_DIGESTS,
    PipelineAdvancementService,
    PipelineTransitionRegistry,
)
from collection_application.pipeline_supervision import (
    PipelinePreviewBlocked,
    PipelineSupervisorService,
)
from collection_infrastructure import (
    PostgresArtifactTransfer,
    PostgresWorkEngine,
    S3ArtifactObjectStore,
)
from collection_infrastructure.postgres.candidate_intake_applier import (
    candidate_intake_transition_appliers,
)
from collection_infrastructure.postgres.manual_import_admission import (
    PostgresManualImportAdmissionStore,
)
from collection_infrastructure.postgres.manual_import_admission_applier import (
    manual_import_admission_transition_appliers,
)
from collection_infrastructure.postgres.manual_import_child_writer import (
    PostgresManualImportChildWorkWriter,
)
from collection_infrastructure.postgres.normalization_applier import (
    normalization_transition_appliers,
)
from collection_infrastructure.postgres.owned_artifact_publisher import (
    PostgresOwnedArtifactPublisher,
)
from collection_infrastructure.postgres.page_extraction_applier import (
    page_extraction_transition_appliers,
)
from collection_infrastructure.postgres.pipeline_advancement import (
    PipelineTransitionApplier,
    PostgresPipelineAdvancementRepository,
)
from collection_infrastructure.postgres.resolution_scheduling import (
    PostgresResolutionBatchScheduler,
)
from collection_infrastructure.postgres.resolution_snapshot_applier import (
    resolution_snapshot_transition_appliers,
)
from collection_infrastructure.postgres.source_record_mapping_applier import (
    source_record_mapping_transition_appliers,
)
from collection_infrastructure.postgres.succeeded_work_catalog import (
    PostgresSucceededWorkCatalog,
)
from collection_infrastructure.postgres.website_route_applier import (
    website_route_transition_appliers,
)
from worker_gateway.app import GatewayDependencies, create_app
from worker_gateway.auth import WorkerAuthenticator

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080
_DEFAULT_EXPIRY_INTERVAL_SECONDS = 5.0
_DEFAULT_EXPIRY_BATCH_SIZE = 100
_DEFAULT_BIND_MODE = "local"
_CONTAINER_BIND_MODE = "container"
_CONTAINER_HOST = "0.0.0.0"  # noqa: S104 - validated container-only bind.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def build_runtime() -> tuple[FastAPI, Engine]:
    database_url = _required_environment("COLLECTOR_DATABASE_URL")
    credential_file = Path(_required_environment("WORKER_GATEWAY_TOKEN_FILE"))
    authenticator = WorkerAuthenticator.from_secret_file(credential_file)

    object_store = S3ArtifactObjectStore.create(
        endpoint_url=_required_environment("COLLECTOR_OBJECT_STORE_ENDPOINT"),
        bucket=_required_environment("COLLECTOR_OBJECT_STORE_BUCKET"),
        access_key_id=_read_secret_file(
            Path(_required_environment("COLLECTOR_OBJECT_STORE_ACCESS_KEY_FILE"))
        ),
        secret_access_key=_read_secret_file(
            Path(_required_environment("COLLECTOR_OBJECT_STORE_SECRET_KEY_FILE"))
        ),
        region_name=_required_environment("COLLECTOR_OBJECT_STORE_REGION"),
    )
    expiry_interval = _float_environment(
        "WORKER_GATEWAY_EXPIRY_INTERVAL_SECONDS",
        _DEFAULT_EXPIRY_INTERVAL_SECONDS,
        minimum=0,
        maximum=3_600,
    )
    expiry_batch_size = _integer_environment(
        "WORKER_GATEWAY_EXPIRY_BATCH_SIZE",
        _DEFAULT_EXPIRY_BATCH_SIZE,
        minimum=1,
        maximum=1_000,
    )
    engine = sa.create_engine(database_url, pool_pre_ping=True)
    postgres_work_engine = PostgresWorkEngine(engine)
    work_engine = WorkEngineService(postgres_work_engine)
    artifact_transfer = ArtifactTransferService(PostgresArtifactTransfer(engine, object_store))
    succeeded_catalog = PostgresSucceededWorkCatalog(engine, object_store=object_store)
    transition_registry = PipelineTransitionRegistry()
    artifact_publisher = PostgresOwnedArtifactPublisher(engine, object_store)
    manual_import_admission_store = PostgresManualImportAdmissionStore(
        engine,
        PostgresManualImportChildWorkWriter(engine),
    )
    transition_appliers: dict[str, PipelineTransitionApplier] = {
        **manual_import_admission_transition_appliers(manual_import_admission_store),
        **website_route_transition_appliers(postgres_work_engine, artifact_publisher),
        **page_extraction_transition_appliers(postgres_work_engine, artifact_publisher),
        **normalization_transition_appliers(postgres_work_engine, artifact_publisher),
        **source_record_mapping_transition_appliers(postgres_work_engine, artifact_publisher),
        **candidate_intake_transition_appliers(
            resolution_scheduler=PostgresResolutionBatchScheduler(
                postgres_work_engine,
                artifact_publisher,
                succeeded_catalog,
            )
        ),
        **resolution_snapshot_transition_appliers(succeeded_catalog),
    }
    advancement_repository = PostgresPipelineAdvancementRepository(
        engine,
        source_reader=succeeded_catalog,
        transition_appliers=transition_appliers,
    )
    advancement = PipelineAdvancementService(advancement_repository)

    class TransitionPlanPreviewer:
        def preview_result_digest(
            self,
            source_work_unit_id: UUID,
            transition_plan_digest: str,
            *,
            correlation_id: str,
        ) -> str:
            del correlation_id
            with engine.connect() as connection:
                source = succeeded_catalog.read(connection, source_work_unit_id)
            plan = transition_registry.plan(source)
            if plan.plan_digest != transition_plan_digest:
                raise RuntimeError("pipeline transition plan changed after registration")
            if plan.blocker is not None:
                raise PipelinePreviewBlocked(plan.blocker)
            result_digest = TRANSITION_RESULT_DIGESTS.get(plan.transition_key)
            if result_digest is None:
                raise RuntimeError("pipeline preview received an unsupported transition")
            return result_digest(plan)

    # Every registered transition has a preview owner: the preview is the transition's own
    # canonical result identity, so the supervisor can state a completion the applier must
    # reproduce, for every route and not only for the ones a preview happened to cover.
    previewer = TransitionPlanPreviewer()
    pipeline_supervisor = PipelineSupervisorService(
        advancement,
        succeeded_catalog,
        dict.fromkeys(transition_appliers, previewer),
    )

    def readiness_probe() -> None:
        with engine.connect() as connection:
            if connection.execute(sa.text("SELECT 1")).scalar_one() != 1:
                raise RuntimeError("PostgreSQL readiness probe returned an unexpected result")
        object_store.assert_ready()

    application = create_app(
        GatewayDependencies(
            pipeline_supervisor=pipeline_supervisor,
            pipeline_service_token=_read_secret_file(
                Path(_required_environment("PIPELINE_ADVANCEMENT_TOKEN_FILE"))
            ),
            work_engine=work_engine,
            artifact_transfer=artifact_transfer,
            authenticator=authenticator,
            readiness_probe=readiness_probe,
            expiry_interval_seconds=expiry_interval,
            expiry_batch_size=expiry_batch_size,
        )
    )
    return application, engine


def main() -> None:
    host = _bind_host_from_environment()
    port = _integer_environment(
        "WORKER_GATEWAY_PORT",
        _DEFAULT_PORT,
        minimum=1,
        maximum=65_535,
    )
    application, engine = build_runtime()
    try:
        uvicorn.run(
            application,
            host=host,
            port=port,
            workers=1,
            proxy_headers=False,
            server_header=False,
            timeout_keep_alive=5,
        )
    finally:
        engine.dispose()


def _bind_host_from_environment() -> str:
    mode = os.environ.get("WORKER_GATEWAY_BIND_MODE", _DEFAULT_BIND_MODE).strip().casefold()
    if mode == _DEFAULT_BIND_MODE:
        host = os.environ.get("WORKER_GATEWAY_HOST", _DEFAULT_HOST).strip()
        if host not in _LOCAL_HOSTS:
            raise RuntimeError(
                "Worker Gateway refuses a non-local bind unless WORKER_GATEWAY_BIND_MODE=container"
            )
        return host
    if mode == _CONTAINER_BIND_MODE:
        host = os.environ.get("WORKER_GATEWAY_HOST", _CONTAINER_HOST).strip()
        if host != _CONTAINER_HOST:
            raise RuntimeError(
                "Worker Gateway container bind mode requires WORKER_GATEWAY_HOST=0.0.0.0"
            )
        return host
    raise RuntimeError("WORKER_GATEWAY_BIND_MODE must be local or container")


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _read_secret_file(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read required secret file: {path}") from exc
    if not value:
        raise RuntimeError(f"required secret file is empty: {path}")
    if len(value) > 4_096:
        raise RuntimeError(f"required secret file is too large: {path}")
    return value


def _integer_environment(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float_environment(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


if __name__ == "__main__":
    main()
