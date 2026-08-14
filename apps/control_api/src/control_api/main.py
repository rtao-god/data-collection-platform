from __future__ import annotations

import os
from datetime import UTC, datetime

import sqlalchemy as sa
from collection_infrastructure.postgres import PostgresReviewRepository
from fastapi import FastAPI
from sqlalchemy.pool import NullPool

from control_api.app import create_app
from control_api.auth import TokenAuthenticator
from review_application import ReviewService


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
        # Container ingress is constrained by the deployment network.
        host="0.0.0.0",  # noqa: S104
        port=8080,
    )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"required Control API setting {name} is missing")
    return value


app = create_runtime_app()
