from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.pool import NullPool

from collection_application import (
    CampaignRunService,
    CampaignSnapshotPublicationService,
    CampaignSnapshotService,
    OwnedArtifactPublisherService,
    RunControlService,
)
from collection_infrastructure import (
    ArtifactObjectStoreError,
    FilesystemCampaignBundleSource,
    PostgresCampaignRunStore,
    PostgresCampaignSnapshotStore,
    PostgresOwnedArtifactPublisher,
    PostgresRunControlRepository,
    S3ArtifactObjectStore,
)
from control_api.app import create_app
from control_api.auth import TokenAuthenticator
from review_application import ReviewService
from review_infrastructure import PostgresReviewRepository


def create_runtime_app() -> FastAPI:
    database_url = _required("COLLECTOR_DATABASE_URL")
    token_json = _required("CONTROL_API_OPERATOR_TOKENS_JSON")
    campaigns_root = Path(_required("COLLECTOR_CAMPAIGNS_ROOT"))
    engine = sa.create_engine(
        database_url,
        pool_pre_ping=True,
        poolclass=NullPool,
    )
    object_store = S3ArtifactObjectStore.create(
        endpoint_url=_required("COLLECTOR_OBJECT_STORE_ENDPOINT"),
        bucket=_required("COLLECTOR_OBJECT_STORE_BUCKET"),
        access_key_id=_read_secret("COLLECTOR_OBJECT_STORE_ACCESS_KEY_FILE"),
        secret_access_key=_read_secret("COLLECTOR_OBJECT_STORE_SECRET_KEY_FILE"),
        region_name=_required("COLLECTOR_OBJECT_STORE_REGION"),
    )
    artifact_publisher = OwnedArtifactPublisherService(
        PostgresOwnedArtifactPublisher(engine, object_store)
    )
    snapshot_publication = CampaignSnapshotPublicationService(
        CampaignSnapshotService(FilesystemCampaignBundleSource(campaigns_root)),
        artifact_publisher,
        PostgresCampaignSnapshotStore(engine),
    )
    run_creator = CampaignRunService(
        snapshot_publication,
        artifact_publisher,
        PostgresCampaignRunStore(engine),
        clock=lambda: datetime.now(UTC),
    )
    run_control = RunControlService(PostgresRunControlRepository(engine))
    review_service = ReviewService(
        PostgresReviewRepository(engine),
        clock=lambda: datetime.now(UTC),
    )

    def readiness() -> bool:
        try:
            with engine.connect() as connection:
                ready = connection.execute(
                    sa.text(
                        "SELECT "
                        "to_regclass('runs.collection_runs') IS NOT NULL "
                        "AND to_regclass('runs.collection_run_transitions') IS NOT NULL "
                        "AND to_regclass('review.review_cases') IS NOT NULL "
                        "AND to_regclass('review.review_case_revisions') IS NOT NULL "
                        "AND to_regclass('review.review_decisions') IS NOT NULL "
                        "AND to_regclass('review.manual_observations') IS NOT NULL "
                        "AND to_regclass('review.suppression_revisions') IS NOT NULL"
                    )
                ).scalar_one()
            object_store.assert_ready()
            return bool(ready)
        except (ArtifactObjectStoreError, sa.exc.SQLAlchemyError, RuntimeError):
            return False

    return create_app(
        service=review_service,
        run_creator=run_creator,
        run_control=run_control,
        authenticator=TokenAuthenticator.from_json(token_json),
        readiness_probe=readiness,
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        "control_api.main:app",
        # Container ingress is constrained by the deployment network.
        host="0.0.0.0",  # noqa: S104
        port=8080,
    )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"required Control API setting {name} is missing")
    return value


def _read_secret(name: str) -> str:
    path = Path(_required(name))
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Control API secret file {name} is empty")
    return value


app = create_runtime_app()
