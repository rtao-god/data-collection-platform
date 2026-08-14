from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from collection_application import (
    CampaignRunBootstrapPlan,
    CampaignRunCreated,
    WorkEngineConflict,
)
from collection_infrastructure.postgres.work_engine import PostgresWorkEngine


class PostgresCampaignRunStore:
    """Creates the run, source capacities, stages, and initial work atomically."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._work_engine = PostgresWorkEngine(engine, clock=clock or (lambda: datetime.now(UTC)))

    def create(self, plan: CampaignRunBootstrapPlan) -> CampaignRunCreated:
        try:
            with self._engine.begin() as connection:
                for source in plan.sources:
                    self._work_engine.configure_source_in_transaction(connection, source)
                self._work_engine.create_run_in_transaction(connection, plan.run)
                for stage in plan.stages:
                    self._work_engine.create_stage_in_transaction(connection, stage)
                for work in plan.initial_work:
                    self._work_engine.enqueue_work_in_transaction(connection, work)
        except WorkEngineConflict:
            raise
        except SQLAlchemyError as exc:
            raise WorkEngineConflict(
                code="CAMPAIGN_RUN_STORAGE_FAILED",
                message="The campaign run bootstrap transaction did not complete.",
                context={"causeType": type(exc).__name__, "runId": str(plan.run.run_id)},
                required_action=(
                    "Inspect the Work Engine owner state and retry the exact run identity."
                ),
            ) from exc
        return CampaignRunCreated(
            run_id=plan.run.run_id,
            campaign_key=plan.run.campaign_key,
            config_bundle_digest=plan.run.config_bundle_digest,
            initial_work_ids=tuple(item.work_id for item in plan.initial_work),
        )
