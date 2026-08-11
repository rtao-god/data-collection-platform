from __future__ import annotations

from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]


def write(relative: str, content: str) -> None:
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip(), encoding="utf-8")


write(
    "packages/collection_application/src/collection_application/run_admission.py",
    r'''
    from __future__ import annotations

    import json
    from collections.abc import Mapping
    from dataclasses import dataclass
    from enum import StrEnum
    from hashlib import sha256
    from typing import Protocol, cast
    from uuid import UUID

    from collection_application.work_engine import CollectionRunSpec, StageRunSpec, WorkUnitSpec
    from collection_contracts import owner_error
    from collection_domain import (
        CollectionRunState,
        RetryPolicy,
        StageRunState,
        WorkCapability,
        WorkStage,
        WorkUnitState,
    )

    INITIAL_STAGE_STATE = StageRunState.PENDING
    INITIAL_WORK_STATE = WorkUnitState.PENDING


    class RunAdmissionStatus(StrEnum):
        CREATED = "created"
        EXISTING = "existing"


    @dataclass(frozen=True, slots=True)
    class RunAdmissionResult:
        run_id: UUID
        admission_digest: str
        status: RunAdmissionStatus
        stage_count: int
        work_unit_count: int


    class RunAdmissionConflict(Exception):
        def __init__(
            self,
            *,
            code: str,
            message: str,
            context: Mapping[str, object],
            required_action: str,
        ) -> None:
            self.code = code
            self.message = message
            self.context = dict(context)
            self.required_action = required_action
            super().__init__(message)


    @dataclass(frozen=True, slots=True)
    class RunAdmissionPlan:
        run: CollectionRunSpec
        stages: tuple[StageRunSpec, ...]
        work_units: tuple[WorkUnitSpec, ...]

        def __post_init__(self) -> None:
            if self.run.initial_state is not CollectionRunState.RUNNING:
                raise ValueError("admitted collection run must start in running state")
            if not self.stages:
                raise ValueError("run admission requires at least one stage")
            if not self.work_units:
                raise ValueError("run admission requires at least one work unit")

            stage_by_id: dict[UUID, StageRunSpec] = {}
            stage_values: set[WorkStage] = set()
            for stage in self.stages:
                if stage.run_id != self.run.run_id:
                    raise ValueError("stage run belongs to another collection run")
                if stage.correlation_id != self.run.correlation_id:
                    raise ValueError("stage run correlation does not match the collection run")
                if stage.stage_run_id in stage_by_id:
                    raise ValueError("run admission contains a duplicate stage run id")
                if stage.stage in stage_values:
                    raise ValueError("run admission contains more than one owner for a stage")
                stage_by_id[stage.stage_run_id] = stage
                stage_values.add(stage.stage)

            work_ids: set[UUID] = set()
            semantic_keys: set[str] = set()
            used_stage_ids: set[UUID] = set()
            for work in self.work_units:
                if work.run_id != self.run.run_id:
                    raise ValueError("work unit belongs to another collection run")
                if work.correlation_id != self.run.correlation_id:
                    raise ValueError("work unit correlation does not match the collection run")
                stage = stage_by_id.get(work.stage_run_id)
                if stage is None or stage.stage is not work.stage:
                    raise ValueError("work unit does not match its admitted stage owner")
                if work.work_id in work_ids:
                    raise ValueError("run admission contains a duplicate work id")
                if work.semantic_key in semantic_keys:
                    raise ValueError("run admission contains a duplicate semantic work key")
                work_ids.add(work.work_id)
                semantic_keys.add(work.semantic_key)
                used_stage_ids.add(work.stage_run_id)

            empty_stages = set(stage_by_id) - used_stage_ids
            if empty_stages:
                raise ValueError("run admission must not create an empty stage")

        @property
        def admission_digest(self) -> str:
            payload: dict[str, object] = {
                "run": {
                    "runId": str(self.run.run_id),
                    "campaignKey": self.run.campaign_key,
                    "configBundleDigest": self.run.config_bundle_digest,
                    "initialState": self.run.initial_state.value,
                },
                "stages": [
                    {
                        "stageRunId": str(stage.stage_run_id),
                        "stage": stage.stage.value,
                    }
                    for stage in sorted(
                        self.stages,
                        key=lambda value: (value.stage.value, str(value.stage_run_id)),
                    )
                ],
                "workUnits": [
                    {
                        "workId": str(work.work_id),
                        "stageRunId": str(work.stage_run_id),
                        "stage": work.stage.value,
                        "capability": work.capability.value,
                        "sourceKey": work.source_key,
                        "semanticKey": work.semantic_key,
                        "inputDigest": work.input_digest,
                        "expectedOutputContract": work.expected_output_contract,
                        "priority": work.priority,
                        "retry": {
                            "maxAttempts": work.retry_policy.max_attempts,
                            "initialDelaySeconds": work.retry_policy.initial_delay_seconds,
                            "multiplier": work.retry_policy.multiplier,
                            "maxDelaySeconds": work.retry_policy.max_delay_seconds,
                        },
                    }
                    for work in sorted(self.work_units, key=lambda value: str(value.work_id))
                ],
            }
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return f"sha256:{sha256(canonical).hexdigest()}"


    class RunAdmissionPort(Protocol):
        def admit(self, plan: RunAdmissionPlan) -> RunAdmissionResult: ...


    class RunAdmissionService:
        def __init__(self, port: RunAdmissionPort) -> None:
            self._port = port

        def admit(self, plan: RunAdmissionPlan) -> RunAdmissionResult:
            try:
                return self._port.admit(plan)
            except RunAdmissionConflict as exc:
                raise owner_error(
                    error_type=f"collection/{exc.code.lower().replace('_', '-')}",
                    owner="RunAdmission",
                    code=exc.code,
                    message=exc.message,
                    context=exc.context,
                    required_action=exc.required_action,
                    correlation_id=plan.run.correlation_id,
                ) from exc


    def run_admission_plan_from_payload(payload: object) -> RunAdmissionPlan:
        root = _object(payload, "plan")
        _exact_keys(
            root,
            {
                "runId",
                "campaignKey",
                "configBundleDigest",
                "correlationId",
                "stages",
                "workUnits",
            },
            "plan",
        )
        run_id = _uuid(root["runId"], "plan.runId")
        correlation_id = _string(root["correlationId"], "plan.correlationId")
        run = CollectionRunSpec(
            run_id=run_id,
            campaign_key=_string(root["campaignKey"], "plan.campaignKey"),
            config_bundle_digest=_string(
                root["configBundleDigest"],
                "plan.configBundleDigest",
            ),
            initial_state=CollectionRunState.RUNNING,
            correlation_id=correlation_id,
        )

        stages: list[StageRunSpec] = []
        for index, item in enumerate(_array(root["stages"], "plan.stages")):
            path = f"plan.stages[{index}]"
            stage = _object(item, path)
            _exact_keys(stage, {"stageRunId", "stage"}, path)
            stages.append(
                StageRunSpec(
                    stage_run_id=_uuid(stage["stageRunId"], f"{path}.stageRunId"),
                    run_id=run_id,
                    stage=_stage(stage["stage"], f"{path}.stage"),
                    correlation_id=correlation_id,
                )
            )

        work_units: list[WorkUnitSpec] = []
        for index, item in enumerate(_array(root["workUnits"], "plan.workUnits")):
            path = f"plan.workUnits[{index}]"
            work = _object(item, path)
            _exact_keys(
                work,
                {
                    "workId",
                    "stageRunId",
                    "stage",
                    "capability",
                    "sourceKey",
                    "semanticKey",
                    "inputDigest",
                    "expectedOutputContract",
                    "priority",
                    "retry",
                },
                path,
            )
            retry_path = f"{path}.retry"
            retry = _object(work["retry"], retry_path)
            _exact_keys(
                retry,
                {
                    "maxAttempts",
                    "initialDelaySeconds",
                    "multiplier",
                    "maxDelaySeconds",
                },
                retry_path,
            )
            work_units.append(
                WorkUnitSpec(
                    work_id=_uuid(work["workId"], f"{path}.workId"),
                    run_id=run_id,
                    stage_run_id=_uuid(work["stageRunId"], f"{path}.stageRunId"),
                    stage=_stage(work["stage"], f"{path}.stage"),
                    capability=_capability(work["capability"], f"{path}.capability"),
                    source_key=_optional_string(work["sourceKey"], f"{path}.sourceKey"),
                    semantic_key=_string(work["semanticKey"], f"{path}.semanticKey"),
                    input_digest=_string(work["inputDigest"], f"{path}.inputDigest"),
                    expected_output_contract=_string(
                        work["expectedOutputContract"],
                        f"{path}.expectedOutputContract",
                    ),
                    priority=_integer(work["priority"], f"{path}.priority"),
                    retry_policy=RetryPolicy(
                        max_attempts=_integer(
                            retry["maxAttempts"],
                            f"{retry_path}.maxAttempts",
                        ),
                        initial_delay_seconds=_integer(
                            retry["initialDelaySeconds"],
                            f"{retry_path}.initialDelaySeconds",
                        ),
                        multiplier=_integer(
                            retry["multiplier"],
                            f"{retry_path}.multiplier",
                        ),
                        max_delay_seconds=_integer(
                            retry["maxDelaySeconds"],
                            f"{retry_path}.maxDelaySeconds",
                        ),
                    ),
                    correlation_id=correlation_id,
                )
            )

        return RunAdmissionPlan(
            run=run,
            stages=tuple(stages),
            work_units=tuple(work_units),
        )


    def _object(value: object, path: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{path} must be an object")
        if not all(isinstance(key, str) for key in value):
            raise ValueError(f"{path} contains a non-string key")
        return cast(Mapping[str, object], value)


    def _array(value: object, path: str) -> list[object]:
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        return cast(list[object], value)


    def _exact_keys(value: Mapping[str, object], expected: set[str], path: str) -> None:
        actual = set(value)
        if actual != expected:
            raise ValueError(
                f"{path} keys differ: missing={sorted(expected - actual)}, "
                f"unexpected={sorted(actual - expected)}"
            )


    def _string(value: object, path: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        return value


    def _optional_string(value: object, path: str) -> str | None:
        if value is None:
            return None
        return _string(value, path)


    def _integer(value: object, path: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path} must be an integer")
        return value


    def _uuid(value: object, path: str) -> UUID:
        raw = _string(value, path)
        try:
            return UUID(raw)
        except ValueError as exc:
            raise ValueError(f"{path} must be a UUID") from exc


    def _stage(value: object, path: str) -> WorkStage:
        raw = _string(value, path)
        try:
            return WorkStage(raw)
        except ValueError as exc:
            raise ValueError(f"{path} is not a supported work stage") from exc


    def _capability(value: object, path: str) -> WorkCapability:
        raw = _string(value, path)
        try:
            return WorkCapability(raw)
        except ValueError as exc:
            raise ValueError(f"{path} is not a supported work capability") from exc
    ''',
)

write(
    "packages/collection_infrastructure/src/collection_infrastructure/postgres/run_admission_metadata.py",
    r'''
    from __future__ import annotations

    import sqlalchemy as sa

    from collection_infrastructure.postgres.metadata import collector_metadata

    RUNS_SCHEMA = "runs"

    run_admissions = sa.Table(
        "run_admissions",
        collector_metadata,
        sa.Column(
            "run_id",
            sa.Uuid,
            sa.ForeignKey("runs.collection_runs.run_id"),
            primary_key=True,
        ),
        sa.Column("admission_digest", sa.Text, nullable=False),
        sa.Column("admitted_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text, nullable=False),
        sa.CheckConstraint(
            "admission_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_run_admissions_digest_format",
        ),
        sa.CheckConstraint(
            "length(btrim(correlation_id)) > 0",
            name="ck_run_admissions_correlation_id_non_empty",
        ),
        sa.UniqueConstraint("admission_digest", name="uq_run_admissions_digest"),
        schema=RUNS_SCHEMA,
        comment="Immutable identity of the exact topology admitted for a collection run.",
    )

    RUN_ADMISSION_TABLES = (run_admissions,)
    ''',
)

write(
    "packages/collection_infrastructure/src/collection_infrastructure/postgres/run_admission.py",
    r'''
    from __future__ import annotations

    from collections.abc import Callable
    from datetime import UTC, datetime, timedelta
    from typing import Self

    import sqlalchemy as sa
    from sqlalchemy.engine import Connection, Engine

    from collection_application.run_admission import (
        INITIAL_STAGE_STATE,
        INITIAL_WORK_STATE,
        RunAdmissionConflict,
        RunAdmissionPlan,
        RunAdmissionResult,
        RunAdmissionStatus,
    )
    from collection_infrastructure.postgres.metadata import (
        config_bundle_blockers,
        config_bundles,
    )
    from collection_infrastructure.postgres.run_admission_metadata import run_admissions
    from collection_infrastructure.postgres.work_metadata import (
        collection_runs,
        source_capacity_states,
        stage_runs,
        work_units,
    )


    class PostgresRunAdmissionStore:
        def __init__(
            self,
            engine: Engine,
            *,
            clock: Callable[[], datetime] | None = None,
        ) -> None:
            self._engine = engine
            self._clock = clock or _utc_now

        @classmethod
        def from_url(
            cls,
            database_url: str,
            *,
            clock: Callable[[], datetime] | None = None,
        ) -> Self:
            return cls(
                sa.create_engine(database_url, pool_pre_ping=True),
                clock=clock,
            )

        def admit(self, plan: RunAdmissionPlan) -> RunAdmissionResult:
            now_utc = self._clock()
            _require_utc(now_utc)
            try:
                with self._engine.begin() as connection:
                    return self._admit(connection, plan, now_utc)
            except sa.exc.IntegrityError as exc:
                raise RunAdmissionConflict(
                    code="RUN_ADMISSION_STORAGE_CONFLICT",
                    message="The database rejected the atomic run admission plan.",
                    context={
                        "runId": str(plan.run.run_id),
                        "constraint": _constraint_name(exc),
                    },
                    required_action=(
                        "Inspect the conflicting immutable owner record and submit a new run id "
                        "only after correcting the admission plan."
                    ),
                ) from exc

        @staticmethod
        def _admit(
            connection: Connection,
            plan: RunAdmissionPlan,
            now_utc: datetime,
        ) -> RunAdmissionResult:
            connection.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": str(plan.run.run_id)},
            )
            bundle = connection.execute(
                sa.select(
                    config_bundles.c.campaign_key,
                    config_bundles.c.readiness,
                ).where(
                    config_bundles.c.bundle_digest == plan.run.config_bundle_digest
                )
            ).mappings().one_or_none()
            if bundle is None:
                raise RunAdmissionConflict(
                    code="RUN_CONFIG_SNAPSHOT_NOT_PUBLISHED",
                    message="The requested campaign snapshot is not published in PostgreSQL.",
                    context={
                        "runId": str(plan.run.run_id),
                        "configBundleDigest": plan.run.config_bundle_digest,
                    },
                    required_action=(
                        "Publish and verify the exact campaign snapshot before admitting the run."
                    ),
                )
            if bundle["campaign_key"] != plan.run.campaign_key:
                raise RunAdmissionConflict(
                    code="RUN_CONFIG_CAMPAIGN_MISMATCH",
                    message="The published snapshot belongs to another campaign.",
                    context={
                        "runId": str(plan.run.run_id),
                        "expectedCampaignKey": plan.run.campaign_key,
                        "actualCampaignKey": bundle["campaign_key"],
                    },
                    required_action="Use a snapshot published for the requested campaign.",
                )
            if bundle["readiness"] != "ready":
                blockers = tuple(
                    connection.scalars(
                        sa.select(config_bundle_blockers.c.code)
                        .where(
                            config_bundle_blockers.c.bundle_digest
                            == plan.run.config_bundle_digest
                        )
                        .order_by(config_bundle_blockers.c.position)
                    )
                )
                raise RunAdmissionConflict(
                    code="RUN_CONFIG_SNAPSHOT_BLOCKED",
                    message="The published campaign snapshot is explicitly blocked.",
                    context={
                        "runId": str(plan.run.run_id),
                        "configBundleDigest": plan.run.config_bundle_digest,
                        "blockerCodes": list(blockers),
                    },
                    required_action=(
                        "Resolve every published blocker and publish a new ready snapshot digest."
                    ),
                )

            existing = connection.execute(
                sa.select(
                    collection_runs.c.campaign_key,
                    collection_runs.c.config_bundle_digest,
                )
                .where(collection_runs.c.run_id == plan.run.run_id)
                .with_for_update()
            ).mappings().one_or_none()
            if existing is not None:
                existing_digest = connection.scalar(
                    sa.select(run_admissions.c.admission_digest).where(
                        run_admissions.c.run_id == plan.run.run_id
                    )
                )
                if (
                    existing["campaign_key"] == plan.run.campaign_key
                    and existing["config_bundle_digest"] == plan.run.config_bundle_digest
                    and existing_digest == plan.admission_digest
                ):
                    return RunAdmissionResult(
                        run_id=plan.run.run_id,
                        admission_digest=plan.admission_digest,
                        status=RunAdmissionStatus.EXISTING,
                        stage_count=len(plan.stages),
                        work_unit_count=len(plan.work_units),
                    )
                raise RunAdmissionConflict(
                    code="RUN_ADMISSION_IDENTITY_CONFLICT",
                    message="The run id is already owned by a different admission plan.",
                    context={
                        "runId": str(plan.run.run_id),
                        "requestedAdmissionDigest": plan.admission_digest,
                        "storedAdmissionDigest": existing_digest,
                    },
                    required_action="Create a new run id for the changed immutable plan.",
                )

            source_keys = sorted(
                {work.source_key for work in plan.work_units if work.source_key is not None}
            )
            if source_keys:
                configured = set(
                    connection.scalars(
                        sa.select(source_capacity_states.c.source_key).where(
                            source_capacity_states.c.source_key.in_(source_keys)
                        )
                    )
                )
                missing = sorted(set(source_keys) - configured)
                if missing:
                    raise RunAdmissionConflict(
                        code="RUN_SOURCE_CAPACITY_NOT_CONFIGURED",
                        message="One or more source-bound work units have no capacity owner.",
                        context={"runId": str(plan.run.run_id), "sourceKeys": missing},
                        required_action=(
                            "Configure each source capacity and policy digest before admitting work."
                        ),
                    )

            connection.execute(
                collection_runs.insert().values(
                    run_id=plan.run.run_id,
                    campaign_key=plan.run.campaign_key,
                    config_bundle_digest=plan.run.config_bundle_digest,
                    state=plan.run.initial_state.value,
                    revision=0,
                    created_at_utc=now_utc,
                    updated_at_utc=now_utc,
                    correlation_id=plan.run.correlation_id,
                )
            )
            connection.execute(
                run_admissions.insert().values(
                    run_id=plan.run.run_id,
                    admission_digest=plan.admission_digest,
                    admitted_at_utc=now_utc,
                    correlation_id=plan.run.correlation_id,
                )
            )
            connection.execute(
                stage_runs.insert(),
                [
                    {
                        "stage_run_id": stage.stage_run_id,
                        "run_id": stage.run_id,
                        "stage": stage.stage.value,
                        "state": INITIAL_STAGE_STATE.value,
                        "revision": 0,
                        "created_at_utc": now_utc,
                        "updated_at_utc": now_utc,
                        "correlation_id": stage.correlation_id,
                    }
                    for stage in plan.stages
                ],
            )
            connection.execute(
                work_units.insert(),
                [
                    {
                        "work_id": work.work_id,
                        "run_id": work.run_id,
                        "stage_run_id": work.stage_run_id,
                        "stage": work.stage.value,
                        "capability": work.capability.value,
                        "source_key": work.source_key,
                        "semantic_key": work.semantic_key,
                        "input_digest": work.input_digest,
                        "expected_output_contract": work.expected_output_contract,
                        "priority": work.priority,
                        "state": INITIAL_WORK_STATE.value,
                        "attempt_count": 0,
                        "max_attempts": work.retry_policy.max_attempts,
                        "retry_initial_delay_seconds": (
                            work.retry_policy.initial_delay_seconds
                        ),
                        "retry_multiplier": work.retry_policy.multiplier,
                        "retry_max_delay_seconds": work.retry_policy.max_delay_seconds,
                        "available_at_utc": now_utc,
                        "active_lease_id": None,
                        "active_lease_token": None,
                        "active_worker_id": None,
                        "lease_issued_at_utc": None,
                        "lease_expires_at_utc": None,
                        "heartbeat_deadline_utc": None,
                        "source_policy_digest": None,
                        "source_permit_not_before_utc": None,
                        "output_contract": None,
                        "output_digest": None,
                        "completed_at_utc": None,
                        "revision": 0,
                        "created_at_utc": now_utc,
                        "updated_at_utc": now_utc,
                        "correlation_id": work.correlation_id,
                    }
                    for work in plan.work_units
                ],
            )
            return RunAdmissionResult(
                run_id=plan.run.run_id,
                admission_digest=plan.admission_digest,
                status=RunAdmissionStatus.CREATED,
                stage_count=len(plan.stages),
                work_unit_count=len(plan.work_units),
            )


    def _utc_now() -> datetime:
        return datetime.now(UTC)


    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("run admission clock must return timezone-aware UTC")


    def _constraint_name(exc: sa.exc.IntegrityError) -> str:
        diagnostic = getattr(exc.orig, "diag", None)
        value = getattr(diagnostic, "constraint_name", None)
        return value if isinstance(value, str) and value else "unknown"
    ''',
)

write(
    "apps/collector_cli/src/collector_cli/run_admission.py",
    r'''
    from __future__ import annotations

    import argparse
    import json
    import os
    import sys
    from pathlib import Path

    from collection_application.run_admission import (
        RunAdmissionService,
        run_admission_plan_from_payload,
    )
    from collection_contracts import OwnerContextError, owner_error
    from collection_infrastructure.postgres.run_admission import PostgresRunAdmissionStore


    def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result


    def _reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant is not allowed: {value}")


    def _load_payload(path: Path) -> object:
        return json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )


    def _create_store() -> PostgresRunAdmissionStore:
        database_url = os.environ.get("COLLECTOR_DATABASE_URL")
        if not database_url:
            raise owner_error(
                error_type="collection/run-admission-database-url-missing",
                owner="RunAdmission",
                code="RUN_ADMISSION_DATABASE_URL_MISSING",
                message="The run admission process has no database connection setting.",
                context={},
                required_action="Set COLLECTOR_DATABASE_URL for the control-plane database.",
                correlation_id="run-admission-cli",
            )
        return PostgresRunAdmissionStore.from_url(database_url)


    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description="Atomically admit one immutable collection-run topology."
        )
        parser.add_argument("plan", type=Path)
        return parser


    def main(argv: list[str] | None = None) -> int:
        args = _parser().parse_args(argv)
        try:
            plan = run_admission_plan_from_payload(_load_payload(args.plan))
            result = RunAdmissionService(_create_store()).admit(plan)
        except OwnerContextError as exc:
            print(exc.envelope.model_dump_json(by_alias=True), file=sys.stderr)
            return 1
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            error = owner_error(
                error_type="collection/run-admission-plan-invalid",
                owner="RunAdmission",
                code="RUN_ADMISSION_PLAN_INVALID",
                message="The run admission plan could not be parsed or validated.",
                context={"reason": type(exc).__name__},
                required_action=(
                    "Provide one strict UTF-8 JSON plan using the documented admission contract."
                ),
                correlation_id="run-admission-cli",
            )
            print(error.envelope.model_dump_json(by_alias=True), file=sys.stderr)
            return 1

        print(
            json.dumps(
                {
                    "admissionDigest": result.admission_digest,
                    "runId": str(result.run_id),
                    "stageCount": result.stage_count,
                    "status": result.status.value,
                    "workUnitCount": result.work_unit_count,
                },
                sort_keys=True,
            )
        )
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    ''',
)

write(
    "database/migrations/versions/20260811_0003_run_admission.py",
    r'''
    """Create immutable run-admission identities.

    Revision ID: 20260811_0003
    Revises: 20260811_0002
    Create Date: 2026-08-11
    """

    from __future__ import annotations

    from collections.abc import Sequence

    import sqlalchemy as sa
    from alembic import op

    revision: str = "20260811_0003"
    down_revision: str | None = "20260811_0002"
    branch_labels: str | Sequence[str] | None = None
    depends_on: str | Sequence[str] | None = None


    def upgrade() -> None:
        op.create_table(
            "run_admissions",
            sa.Column("run_id", sa.Uuid(), nullable=False),
            sa.Column("admission_digest", sa.Text(), nullable=False),
            sa.Column("admitted_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("correlation_id", sa.Text(), nullable=False),
            sa.CheckConstraint(
                "admission_digest ~ '^sha256:[0-9a-f]{64}$'",
                name="ck_run_admissions_digest_format",
            ),
            sa.CheckConstraint(
                "length(btrim(correlation_id)) > 0",
                name="ck_run_admissions_correlation_id_non_empty",
            ),
            sa.ForeignKeyConstraint(
                ("run_id",),
                ("runs.collection_runs.run_id",),
                name="fk_run_admissions_run_id_collection_runs",
            ),
            sa.PrimaryKeyConstraint("run_id", name="pk_run_admissions"),
            sa.UniqueConstraint(
                "admission_digest",
                name="uq_run_admissions_digest",
            ),
            schema="runs",
            comment="Immutable identity of the exact topology admitted for a collection run.",
        )
        op.execute(
            """
            CREATE FUNCTION runs.reject_run_admission_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'run admission identity is immutable';
                RETURN NULL;
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_run_admissions_immutable
            BEFORE UPDATE OR DELETE ON runs.run_admissions
            FOR EACH ROW
            EXECUTE FUNCTION runs.reject_run_admission_mutation()
            """
        )


    def downgrade() -> None:
        op.drop_table("run_admissions", schema="runs")
        op.execute("DROP FUNCTION runs.reject_run_admission_mutation()")
    ''',
)

write(
    "packages/collection_application/tests/test_run_admission.py",
    r'''
    from __future__ import annotations

    from copy import deepcopy
    from uuid import UUID

    import pytest

    from collection_application.run_admission import run_admission_plan_from_payload

    _RUN_ID = "019c0000-0000-7000-8000-000000000101"
    _STAGE_ID = "019c0000-0000-7000-8000-000000000102"
    _WORK_ID = "019c0000-0000-7000-8000-000000000103"
    _DIGEST = "sha256:" + ("a" * 64)


    def _payload() -> dict[str, object]:
        return {
            "runId": _RUN_ID,
            "campaignKey": "berlin_recording_services",
            "configBundleDigest": _DIGEST,
            "correlationId": "correlation-1",
            "stages": [{"stageRunId": _STAGE_ID, "stage": "discovery"}],
            "workUnits": [
                {
                    "workId": _WORK_ID,
                    "stageRunId": _STAGE_ID,
                    "stage": "discovery",
                    "capability": "manual_import",
                    "sourceKey": None,
                    "semanticKey": "sha256:" + ("b" * 64),
                    "inputDigest": "sha256:" + ("c" * 64),
                    "expectedOutputContract": "manual-discovery-observations",
                    "priority": 0,
                    "retry": {
                        "maxAttempts": 3,
                        "initialDelaySeconds": 10,
                        "multiplier": 2,
                        "maxDelaySeconds": 60,
                    },
                }
            ],
        }


    def test_plan_has_deterministic_digest_and_exact_owner_links() -> None:
        plan = run_admission_plan_from_payload(_payload())

        assert plan.run.run_id == UUID(_RUN_ID)
        assert plan.stages[0].run_id == plan.run.run_id
        assert plan.work_units[0].stage_run_id == plan.stages[0].stage_run_id
        assert plan.admission_digest.startswith("sha256:")
        assert plan.admission_digest == run_admission_plan_from_payload(_payload()).admission_digest


    def test_plan_digest_is_independent_of_array_order() -> None:
        payload = _payload()
        second_stage = {
            "stageRunId": "019c0000-0000-7000-8000-000000000104",
            "stage": "acquisition",
        }
        second_work = {
            "workId": "019c0000-0000-7000-8000-000000000105",
            "stageRunId": second_stage["stageRunId"],
            "stage": "acquisition",
            "capability": "http_fetch",
            "sourceKey": "official_website",
            "semanticKey": "sha256:" + ("d" * 64),
            "inputDigest": "sha256:" + ("e" * 64),
            "expectedOutputContract": "fetch-observation",
            "priority": 5,
            "retry": {
                "maxAttempts": 4,
                "initialDelaySeconds": 20,
                "multiplier": 2,
                "maxDelaySeconds": 120,
            },
        }
        payload["stages"] = [*payload["stages"], second_stage]  # type: ignore[index]
        payload["workUnits"] = [*payload["workUnits"], second_work]  # type: ignore[index]
        reversed_payload = deepcopy(payload)
        reversed_payload["stages"] = list(reversed(reversed_payload["stages"]))  # type: ignore[arg-type]
        reversed_payload["workUnits"] = list(
            reversed(reversed_payload["workUnits"])  # type: ignore[arg-type]
        )

        assert (
            run_admission_plan_from_payload(payload).admission_digest
            == run_admission_plan_from_payload(reversed_payload).admission_digest
        )


    def test_unknown_plan_field_is_rejected() -> None:
        payload = _payload()
        payload["unexpected"] = True

        with pytest.raises(ValueError, match="keys differ"):
            run_admission_plan_from_payload(payload)


    def test_work_cannot_claim_another_stage_owner() -> None:
        payload = _payload()
        payload["workUnits"][0]["stageRunId"] = (  # type: ignore[index]
            "019c0000-0000-7000-8000-000000000199"
        )

        with pytest.raises(ValueError, match="admitted stage owner"):
            run_admission_plan_from_payload(payload)
    ''',
)

write(
    "apps/collector_cli/tests/test_run_admission_cli.py",
    r'''
    from __future__ import annotations

    import json
    from pathlib import Path

    from collection_application.run_admission import (
        RunAdmissionPlan,
        RunAdmissionResult,
        RunAdmissionStatus,
    )
    from collector_cli import run_admission

    _DIGEST = "sha256:" + ("a" * 64)


    class FakeStore:
        def admit(self, plan: RunAdmissionPlan) -> RunAdmissionResult:
            return RunAdmissionResult(
                run_id=plan.run.run_id,
                admission_digest=plan.admission_digest,
                status=RunAdmissionStatus.CREATED,
                stage_count=len(plan.stages),
                work_unit_count=len(plan.work_units),
            )


    def _plan_file(tmp_path: Path) -> Path:
        path = tmp_path / "plan.json"
        path.write_text(
            json.dumps(
                {
                    "runId": "019c0000-0000-7000-8000-000000000201",
                    "campaignKey": "berlin_recording_services",
                    "configBundleDigest": _DIGEST,
                    "correlationId": "correlation-1",
                    "stages": [
                        {
                            "stageRunId": "019c0000-0000-7000-8000-000000000202",
                            "stage": "discovery",
                        }
                    ],
                    "workUnits": [
                        {
                            "workId": "019c0000-0000-7000-8000-000000000203",
                            "stageRunId": "019c0000-0000-7000-8000-000000000202",
                            "stage": "discovery",
                            "capability": "manual_import",
                            "sourceKey": None,
                            "semanticKey": "sha256:" + ("b" * 64),
                            "inputDigest": "sha256:" + ("c" * 64),
                            "expectedOutputContract": "manual-discovery-observations",
                            "priority": 0,
                            "retry": {
                                "maxAttempts": 3,
                                "initialDelaySeconds": 10,
                                "multiplier": 2,
                                "maxDelaySeconds": 60,
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path


    def test_cli_invokes_real_application_boundary(
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        monkeypatch.setattr(run_admission, "_create_store", lambda: FakeStore())

        exit_code = run_admission.main([str(_plan_file(tmp_path))])

        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "created"
        assert payload["stageCount"] == 1
        assert payload["workUnitCount"] == 1


    def test_cli_rejects_duplicate_json_keys(tmp_path: Path, capsys) -> None:
        path = tmp_path / "duplicate.json"
        path.write_text('{"runId":"a","runId":"b"}', encoding="utf-8")

        exit_code = run_admission.main([str(path)])

        assert exit_code == 1
        payload = json.loads(capsys.readouterr().err)
        assert payload["code"] == "RUN_ADMISSION_PLAN_INVALID"
    ''',
)

write(
    "packages/collection_infrastructure/tests/test_postgres_run_admission_metadata.py",
    r'''
    from __future__ import annotations

    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from collection_infrastructure.postgres.run_admission_metadata import run_admissions


    def test_run_admission_metadata_is_immutable_identity_shape() -> None:
        sql = str(CreateTable(run_admissions).compile(dialect=postgresql.dialect()))

        assert run_admissions.fullname == "runs.run_admissions"
        assert run_admissions.primary_key.columns.keys() == ["run_id"]
        assert all(column.server_default is None for column in run_admissions.columns)
        assert all(foreign_key.ondelete is None for foreign_key in run_admissions.foreign_keys)
        assert "CONSTRAINT fk_run_admissions_run_id_collection_runs" in sql
        assert "CONSTRAINT ck_run_admissions_digest_format" in sql
        assert "CONSTRAINT uq_run_admissions_digest" in sql
    ''',
)

write(
    "database/tests/test_run_admission_integration.py",
    r'''
    from __future__ import annotations

    import os
    from copy import deepcopy
    from datetime import UTC, datetime
    from hashlib import sha256
    from uuid import uuid4

    import pytest
    import sqlalchemy as sa
    from sqlalchemy.engine import Engine

    from collection_application.run_admission import (
        RunAdmissionConflict,
        RunAdmissionStatus,
        run_admission_plan_from_payload,
    )
    from collection_infrastructure.postgres.metadata import (
        config_bundle_blockers,
        config_bundle_components,
        config_bundles,
    )
    from collection_infrastructure.postgres.run_admission import PostgresRunAdmissionStore
    from collection_infrastructure.postgres.run_admission_metadata import run_admissions
    from collection_infrastructure.postgres.work_metadata import (
        collection_runs,
        stage_runs,
        work_units,
    )

    pytestmark = pytest.mark.integration


    def _engine() -> Engine:
        database_url = os.environ.get("COLLECTOR_DATABASE_URL")
        if not database_url:
            pytest.skip("COLLECTOR_DATABASE_URL is required")
        return sa.create_engine(database_url)


    def _digest(seed: str) -> str:
        return f"sha256:{sha256(seed.encode('utf-8')).hexdigest()}"


    def _seed_bundle(
        engine: Engine,
        *,
        campaign_key: str,
        bundle_digest: str,
        readiness: str,
    ) -> None:
        with engine.begin() as connection:
            connection.execute(
                config_bundle_components.insert().values(
                    bundle_digest=bundle_digest,
                    position=0,
                    path="campaign.yaml",
                    component_digest=_digest(f"component-{bundle_digest}"),
                )
            )
            if readiness == "blocked":
                connection.execute(
                    config_bundle_blockers.insert().values(
                        bundle_digest=bundle_digest,
                        position=0,
                        code="TEST_BLOCKER",
                        owner="RunAdmissionTest",
                        message="The fixture is intentionally blocked.",
                        required_action="Use a ready fixture.",
                    )
                )
            connection.execute(
                config_bundles.insert().values(
                    bundle_digest=bundle_digest,
                    campaign_key=campaign_key,
                    contract="collector-campaign-snapshot",
                    contract_revision="campaign-snapshot-v1",
                    readiness=readiness,
                    recorded_at_utc=datetime.now(UTC),
                )
            )


    def _payload(campaign_key: str, bundle_digest: str) -> dict[str, object]:
        run_id = str(uuid4())
        stage_run_id = str(uuid4())
        return {
            "runId": run_id,
            "campaignKey": campaign_key,
            "configBundleDigest": bundle_digest,
            "correlationId": f"correlation-{run_id}",
            "stages": [{"stageRunId": stage_run_id, "stage": "discovery"}],
            "workUnits": [
                {
                    "workId": str(uuid4()),
                    "stageRunId": stage_run_id,
                    "stage": "discovery",
                    "capability": "manual_import",
                    "sourceKey": None,
                    "semanticKey": _digest(f"semantic-{run_id}"),
                    "inputDigest": _digest(f"input-{run_id}"),
                    "expectedOutputContract": "manual-discovery-observations",
                    "priority": 0,
                    "retry": {
                        "maxAttempts": 3,
                        "initialDelaySeconds": 10,
                        "multiplier": 2,
                        "maxDelaySeconds": 60,
                    },
                }
            ],
        }


    def test_ready_snapshot_is_admitted_atomically_and_idempotently() -> None:
        engine = _engine()
        campaign_key = f"campaign_{uuid4().hex[:12]}"
        bundle_digest = _digest(f"bundle-{uuid4()}")
        _seed_bundle(
            engine,
            campaign_key=campaign_key,
            bundle_digest=bundle_digest,
            readiness="ready",
        )
        plan = run_admission_plan_from_payload(_payload(campaign_key, bundle_digest))
        store = PostgresRunAdmissionStore(engine)

        created = store.admit(plan)
        existing = store.admit(plan)

        assert created.status is RunAdmissionStatus.CREATED
        assert existing.status is RunAdmissionStatus.EXISTING
        with engine.connect() as connection:
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(collection_runs).where(
                    collection_runs.c.run_id == plan.run.run_id
                )
            ) == 1
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(run_admissions).where(
                    run_admissions.c.run_id == plan.run.run_id
                )
            ) == 1
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(stage_runs).where(
                    stage_runs.c.run_id == plan.run.run_id
                )
            ) == 1
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(work_units).where(
                    work_units.c.run_id == plan.run.run_id
                )
            ) == 1


    def test_blocked_snapshot_rejects_run_without_partial_rows() -> None:
        engine = _engine()
        campaign_key = f"campaign_{uuid4().hex[:12]}"
        bundle_digest = _digest(f"bundle-{uuid4()}")
        _seed_bundle(
            engine,
            campaign_key=campaign_key,
            bundle_digest=bundle_digest,
            readiness="blocked",
        )
        plan = run_admission_plan_from_payload(_payload(campaign_key, bundle_digest))

        with pytest.raises(RunAdmissionConflict) as raised:
            PostgresRunAdmissionStore(engine).admit(plan)

        assert raised.value.code == "RUN_CONFIG_SNAPSHOT_BLOCKED"
        with engine.connect() as connection:
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(collection_runs).where(
                    collection_runs.c.run_id == plan.run.run_id
                )
            ) == 0


    def test_same_run_id_rejects_changed_immutable_plan() -> None:
        engine = _engine()
        campaign_key = f"campaign_{uuid4().hex[:12]}"
        bundle_digest = _digest(f"bundle-{uuid4()}")
        _seed_bundle(
            engine,
            campaign_key=campaign_key,
            bundle_digest=bundle_digest,
            readiness="ready",
        )
        payload = _payload(campaign_key, bundle_digest)
        original = run_admission_plan_from_payload(payload)
        PostgresRunAdmissionStore(engine).admit(original)
        changed_payload = deepcopy(payload)
        changed_payload["workUnits"][0]["priority"] = 1  # type: ignore[index]
        changed = run_admission_plan_from_payload(changed_payload)

        with pytest.raises(RunAdmissionConflict) as raised:
            PostgresRunAdmissionStore(engine).admit(changed)

        assert raised.value.code == "RUN_ADMISSION_IDENTITY_CONFLICT"


    def test_source_bound_work_requires_configured_capacity_owner() -> None:
        engine = _engine()
        campaign_key = f"campaign_{uuid4().hex[:12]}"
        bundle_digest = _digest(f"bundle-{uuid4()}")
        _seed_bundle(
            engine,
            campaign_key=campaign_key,
            bundle_digest=bundle_digest,
            readiness="ready",
        )
        payload = _payload(campaign_key, bundle_digest)
        payload["workUnits"][0].update(  # type: ignore[index]
            {
                "capability": "osm_query",
                "sourceKey": f"source_{uuid4().hex[:12]}",
            }
        )
        plan = run_admission_plan_from_payload(payload)

        with pytest.raises(RunAdmissionConflict) as raised:
            PostgresRunAdmissionStore(engine).admit(plan)

        assert raised.value.code == "RUN_SOURCE_CAPACITY_NOT_CONFIGURED"


    def test_database_rejects_mutation_of_admission_identity() -> None:
        engine = _engine()
        campaign_key = f"campaign_{uuid4().hex[:12]}"
        bundle_digest = _digest(f"bundle-{uuid4()}")
        _seed_bundle(
            engine,
            campaign_key=campaign_key,
            bundle_digest=bundle_digest,
            readiness="ready",
        )
        plan = run_admission_plan_from_payload(_payload(campaign_key, bundle_digest))
        PostgresRunAdmissionStore(engine).admit(plan)

        with pytest.raises(sa.exc.DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    run_admissions.update()
                    .where(run_admissions.c.run_id == plan.run.run_id)
                    .values(correlation_id="changed")
                )
        with pytest.raises(sa.exc.DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    run_admissions.delete().where(
                        run_admissions.c.run_id == plan.run.run_id
                    )
                )
    ''',
)

write(
    "docs/operations/run-admission.md",
    r'''
    # Run Admission

    Run admission is the only implemented path that creates a collection run, its owned stages,
    and its initial work units as one PostgreSQL transaction.

    ## Preconditions

    - `configBundleDigest` must already exist in `config.config_bundles`;
    - the stored campaign key must match the plan;
    - snapshot readiness must be `ready`; a persisted `blocked` snapshot is evidence, not runnable
      authorization;
    - every source-bound work unit must reference a configured
      `sources.source_capacity_states` owner;
    - every stage must own at least one work unit and every work unit must match its stage owner.

    ## Identity and replay

    The application canonicalizes the immutable topology and computes one SHA-256 admission digest.
    `runs.run_admissions` stores that digest under the run ID and rejects update or delete in
    PostgreSQL. Replaying the same plan returns `existing`. Reusing the run ID with a changed plan
    fails with `RUN_ADMISSION_IDENTITY_CONFLICT`; no partial stages or work units are written.

    ## Operator command

    ```text
    COLLECTOR_DATABASE_URL=... \
      uv run python -m collector_cli.run_admission path/to/run-plan.json
    ```

    The JSON document is strict: unknown or missing keys, duplicate keys, non-finite numbers,
    invalid stage/capability values, broken ownership links, or invalid retry policies fail before
    the transaction. Database credentials are read only from `COLLECTOR_DATABASE_URL` and are never
    accepted in the plan.

    This command admits durable work; it does not grant workers SQL credentials. Lease acquisition,
    heartbeat, completion, retry, and object transfer remain Worker Gateway responsibilities.
    ''',
)

module_path = ROOT / ".codex/modules/work-engine.md"
if module_path.exists():
    module_text = module_path.read_text(encoding="utf-8")
    marker = "## Run admission ownership"
    if marker not in module_text:
        module_text = module_text.rstrip() + dedent(
            r'''

            ## Run admission ownership

            `collection_application.run_admission` owns strict topology validation, canonical
            admission identity, replay semantics, and owner-context failures. PostgreSQL projection
            belongs to `collection_infrastructure.postgres.run_admission`; immutable admission
            identity belongs to `runs.run_admissions` and migration `20260811_0003`.

            A run is admitted only from an already published `ready` campaign snapshot. The root,
            admission identity, stages, and initial work units are inserted atomically. Blocked or
            missing snapshots, campaign mismatch, missing source-capacity owners, and reuse of a run
            ID for a changed plan fail without partial state.

            The operator composition root is `python -m collector_cli.run_admission`. It has no
            worker data-plane authority. Worker Gateway, lease transactions, source permit mutation,
            pre-signed object transfer, and stale-result rejection remain downstream owners.
            '''
        )
        module_path.write_text(module_text.lstrip(), encoding="utf-8")

status_path = ROOT / "docs/implementation-status.md"
if status_path.exists():
    status_text = status_path.read_text(encoding="utf-8")
    if "Atomic run admission" not in status_text:
        anchor = "| Architecture proof |"
        line = (
            "| Atomic run admission | Ready-snapshot gate, immutable plan digest, and one "
            "transaction for run/stages/work |\n"
        )
        if anchor in status_text:
            position = status_text.index(anchor)
            status_text = status_text[:position] + line + status_text[position:]
        else:
            status_text += "\n" + line
        status_text = status_text.replace(
            "- no collection-run, stage-run, work-unit, lease, attempt, or source-capacity persistence;",
            "- run/stage/work/source metadata and atomic run admission exist; operational lease, "
            "heartbeat, completion, retry/dead-letter, and expiry transactions are not yet wired "
            "through a Worker Gateway;",
        )
        status_path.write_text(status_text, encoding="utf-8")
