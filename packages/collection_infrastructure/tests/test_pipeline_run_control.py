from __future__ import annotations

from contextlib import AbstractContextManager
from uuid import UUID

from collection_application.run_control import RunCoverageReport
from collection_infrastructure.postgres.pipeline_run_control import (
    PipelineAwareRunControlRepository,
)
from sqlalchemy.dialects import postgresql

from collection_domain import CollectionRunState, WorkStage

_RUN_ID = UUID("00000000-0000-0000-0000-000000000501")


class Delegate:
    def get(self, run_id):
        raise AssertionError(run_id)

    def transition(self, command):
        raise AssertionError(command)

    def coverage(self, run_id):
        assert run_id == _RUN_ID
        return RunCoverageReport(
            run_id=run_id,
            state=CollectionRunState.RUNNING,
            revision=4,
            stages=(),
            blockers=(),
        )


class Result:
    def mappings(self):
        return self

    def all(self):
        return [
            {
                "source_stage": WorkStage.DISCOVERY.value,
                "state": "pending",
                "blocker_owner": None,
                "blocker_code": None,
                "blocker_message": None,
                "blocker_required_action": None,
                "count": 2,
            },
            {
                "source_stage": WorkStage.DISCOVERY.value,
                "state": "blocked",
                "blocker_owner": "PipelineAdvancement",
                "blocker_code": "PIPELINE_TRANSITION_UNSUPPORTED",
                "blocker_message": "No transition owns the output.",
                "blocker_required_action": "Install the exact owner transition.",
                "count": 1,
            },
        ]


class Connection:
    def execute(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect())).upper()
        assert "WORK.PIPELINE_ADVANCEMENTS.STATE IN" in sql
        assert "APPLIED" not in statement.compile(dialect=postgresql.dialect()).params.values()
        return Result()


class Connect(AbstractContextManager[Connection]):
    def __enter__(self):
        return Connection()

    def __exit__(self, exc_type, exc_value, traceback):
        return None


class Engine:
    def connect(self):
        return Connect()


def test_pipeline_states_are_exposed_as_run_coverage_blockers() -> None:
    repository = PipelineAwareRunControlRepository(
        Delegate(),  # type: ignore[arg-type]
        Engine(),  # type: ignore[arg-type]
    )

    coverage = repository.coverage(_RUN_ID)

    assert coverage.revision == 4
    assert [(item.code, item.count) for item in coverage.blockers] == [
        ("PIPELINE_ADVANCEMENT_PENDING", 2),
        ("PIPELINE_TRANSITION_UNSUPPORTED", 1),
    ]
