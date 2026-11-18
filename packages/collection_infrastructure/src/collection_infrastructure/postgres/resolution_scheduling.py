"""Schedule the run's single canonical entity-resolution unit.

Batch assembly is not a pipeline transition: the batch covers *every* persisted candidate
revision of the run, so it can only be built once the run has no candidate-producing work
left that could add another revision. Scheduling therefore happens at the decision point
where that fact becomes true, inside the transaction that applied the last intake, and the
resulting unit is a normal Work Engine unit advanced by the usual pipeline transition.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from collection_application import (
    ArtifactKind,
    OwnedArtifactPublishConflict,
    PublishOwnedArtifact,
    RetryPolicy,
    StageRunState,
    WorkCapability,
    WorkInputArtifact,
    WorkStage,
    WorkUnitSpec,
)
from collection_application.pipeline_advancement import (
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionRegistry,
    SucceededWorkOutput,
)
from collection_contracts import QualityProfileDocument
from collection_infrastructure.postgres.owned_artifact_publisher import (
    PostgresOwnedArtifactPublisher,
)
from collection_infrastructure.postgres.succeeded_work_catalog import (
    PostgresSucceededWorkCatalog,
)
from collection_infrastructure.postgres.work_engine import PostgresWorkEngine
from collection_infrastructure.postgres.work_metadata import stage_runs, work_units
from collection_infrastructure.resolution import (
    ResolutionAssemblyError,
    ResolutionCandidateSource,
    ResolutionMarketArea,
    assemble_resolution_batch,
)
from resolution_contracts import (
    PriorCluster,
    PriorClusterPayload,
    canonical_resolution_batch_json,
    seal_prior_cluster,
)

_ARTIFACT_NAMESPACE = UUID("c7f1b476-2c9a-5d38-8f05-43a6e17b9c28")
_BATCH_ROLE = "resolution_batch"
_BATCH_CONTENT_TYPE = "application/vnd.collection.entity-resolution-batch+json"
_OUTPUT_CONTRACT = "entity-resolution-snapshot@1"
_PRODUCER_IDENTITY = "collection.infra.resolution-batch-assembly"
_QUALITY_PROFILE_DOCUMENT = "quality_profile.yaml"
_GEOGRAPHY_DOCUMENT = "geography.yaml"
# Candidate revisions come from exactly these capabilities; nothing else can add one.
_CANDIDATE_PRODUCING_CAPABILITIES = frozenset(
    {
        WorkCapability.NORMALIZATION.value,
        WorkCapability.SOURCE_RECORD_MAPPING.value,
        WorkCapability.MANUAL_RECORD.value,
    }
)
_OPEN_WORK_STATES = frozenset({"pending", "leased", "retry_wait"})


class PostgresResolutionBatchScheduler:
    """Publishes the run's canonical resolution batch and enqueues its work unit."""

    def __init__(
        self,
        work_engine: PostgresWorkEngine,
        artifact_publisher: PostgresOwnedArtifactPublisher,
        catalog: PostgresSucceededWorkCatalog,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._work_engine = work_engine
        self._artifact_publisher = artifact_publisher
        self._catalog = catalog
        self._clock = clock or (lambda: datetime.now(UTC))

    def schedule_if_drained(
        self,
        connection: Connection,
        *,
        source: SucceededWorkOutput,
        correlation_id: str,
    ) -> UUID | None:
        """Schedule the batch once no further candidate revision can appear."""
        if not self._candidate_work_drained(connection, source.run_id):
            return None
        if source.campaign_bundle_content is None:
            raise _conflict(
                "PIPELINE_RESOLUTION_SNAPSHOT_MISSING",
                "Resolution dispatch requires the exact sealed campaign snapshot.",
                source_work_unit_id=source.source_work_unit_id,
                required_action="Restore the run-bound campaign snapshot before resolution.",
            )
        documents = _bundle_documents(source.campaign_bundle_content, source)
        profile = _quality_profile(documents, source)
        market_area = _market_area(documents, source)
        planned = self._candidate_intake_actions(connection, source, correlation_id)
        if not self._all_revisions_persisted(connection, planned):
            # A later intake still owes this run a revision, so the batch would be incomplete.
            return None
        revisions = self._persisted_candidates(connection, source, planned)
        covered = self._resolved_revisions(connection, source.run_id)
        if covered == set(planned):
            # The current resolution already covers exactly these revisions, so a replay has
            # nothing new to resolve and must not spawn a further generation of the batch.
            return self._current_work_id(connection, source.run_id, covered)
        prior_clusters = self._prior_clusters(connection, source.run_id)
        try:
            batch = assemble_resolution_batch(
                run_id=source.run_id,
                market_area=market_area,
                quality_profile=profile,
                revisions=revisions,
                prior_clusters=prior_clusters,
            )
        except ResolutionAssemblyError as exc:
            raise _conflict(
                exc.code,
                str(exc) or "The persisted candidate revisions cannot form a resolution batch.",
                source_work_unit_id=source.source_work_unit_id,
                context=exc.context,
                required_action=(
                    "Correct the persisted candidate revision or the campaign quality profile."
                ),
            ) from exc
        payload = canonical_resolution_batch_json(batch).encode("utf-8")
        existing = self._existing_unit(connection, source.run_id, batch.batch_digest)
        if existing is not None:
            # A replayed intake derives the same batch, so the unit it schedules already
            # exists: dispatch is idempotent instead of a second lease of the same work.
            return existing
        stage_run_id = _stage_run_id(connection, source.run_id, WorkStage.ENTITY_RESOLUTION)
        now_utc = self._now_utc()
        _ensure_stage_running(
            connection,
            stage_run_id,
            now_utc=now_utc,
            correlation_id=correlation_id,
        )
        artifact_id = self._publish_batch(
            connection,
            batch_digest=batch.batch_digest,
            payload=payload,
            correlation_id=correlation_id,
        )
        spec = WorkUnitSpec(
            work_id=uuid5(_ARTIFACT_NAMESPACE, f"{source.run_id}:resolution:{batch.batch_digest}"),
            run_id=source.run_id,
            stage_run_id=stage_run_id,
            stage=WorkStage.ENTITY_RESOLUTION,
            capability=WorkCapability.ENTITY_RESOLUTION,
            source_key=None,
            source_binding_key=None,
            # The Work Engine binds a semantic key to an exact digest, so the batch identity
            # is expressed the same way every other work unit expresses it.
            semantic_key=batch.batch_digest,
            input_digest=batch.batch_digest,
            expected_output_contract=_OUTPUT_CONTRACT,
            priority=0,
            retry_policy=RetryPolicy(
                max_attempts=3,
                initial_delay_seconds=30,
                multiplier=2,
                max_delay_seconds=900,
            ),
            available_at_utc=now_utc,
            correlation_id=correlation_id,
            input_artifacts=(WorkInputArtifact(artifact_id=artifact_id, role=_BATCH_ROLE),),
        )
        self._work_engine.enqueue_work_in_transaction(connection, spec)
        return spec.work_id

    @staticmethod
    def _resolved_revisions(connection: Connection, run_id: UUID) -> set[tuple[UUID, int]]:
        rows = connection.execute(
            sa.text(
                "SELECT m.candidate_id, r.revision"
                " FROM candidates.resolution_runs rr"
                " JOIN candidates.resolution_cluster_members m"
                " ON m.resolution_id = rr.resolution_id"
                " JOIN candidates.candidate_revisions r ON r.candidate_id = m.candidate_id"
                " WHERE rr.run_id = :run_id AND NOT EXISTS ("
                "   SELECT 1 FROM candidates.resolution_runs s"
                "   WHERE s.supersedes_resolution_id = rr.resolution_id)"
            ),
            {"run_id": run_id},
        ).all()
        return {(UUID(str(row[0])), int(row[1])) for row in rows}

    @staticmethod
    def _current_work_id(
        connection: Connection, run_id: UUID, covered: set[tuple[UUID, int]]
    ) -> UUID | None:
        if not covered:
            return None
        row = (
            connection.execute(
                sa.text(
                    "SELECT rr.work_id FROM candidates.resolution_runs rr"
                    " WHERE rr.run_id = :run_id AND NOT EXISTS ("
                    "   SELECT 1 FROM candidates.resolution_runs s"
                    "   WHERE s.supersedes_resolution_id = rr.resolution_id)"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .first()
        )
        return UUID(str(row["work_id"])) if row is not None else None

    @staticmethod
    def _existing_unit(connection: Connection, run_id: UUID, batch_digest: str) -> UUID | None:
        work_id = uuid5(_ARTIFACT_NAMESPACE, f"{run_id}:resolution:{batch_digest}")
        row = (
            connection.execute(
                sa.select(
                    work_units.c.work_id, work_units.c.semantic_key, work_units.c.input_digest
                ).where(work_units.c.work_id == work_id)
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        if row["semantic_key"] != batch_digest or row["input_digest"] != batch_digest:
            raise _conflict(
                "PIPELINE_RESOLUTION_UNIT_CONFLICT",
                "The resolution unit identity is bound to a different batch.",
                context={"workId": str(work_id)},
                required_action="Repair the resolution unit at its producer.",
            )
        return work_id

    @staticmethod
    def _all_revisions_persisted(
        connection: Connection,
        candidate_revisions: tuple[tuple[UUID, int], ...],
    ) -> bool:
        persisted = {
            (UUID(str(row[0])), int(row[1]))
            for row in connection.execute(
                sa.text(
                    "SELECT candidate_id, revision FROM candidates.candidate_revisions "
                    "WHERE (candidate_id, revision) IN ("
                    "  SELECT * FROM unnest(CAST(:ids AS uuid[]), CAST(:revisions AS integer[]))"
                    ")"
                ),
                {
                    "ids": [item[0] for item in candidate_revisions],
                    "revisions": [item[1] for item in candidate_revisions],
                },
            ).all()
        }
        return persisted == set(candidate_revisions)

    def _persisted_candidates(
        self,
        connection: Connection,
        source: SucceededWorkOutput,
        candidate_revisions: tuple[tuple[UUID, int], ...],
    ) -> tuple[ResolutionCandidateSource, ...]:
        """Read exactly the candidate revisions this run's own transitions created."""
        revisions: list[ResolutionCandidateSource] = []
        for candidate_id, revision in candidate_revisions:
            row = (
                connection.execute(
                    sa.text(
                        "SELECT r.revision, r.normalized_payload, c.entity_kind "
                        "FROM candidates.candidate_revisions AS r "
                        "JOIN candidates.candidates AS c USING (candidate_id) "
                        "WHERE r.candidate_id = :candidate_id AND r.revision = :revision"
                    ),
                    {"candidate_id": candidate_id, "revision": revision},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise _conflict(
                    "PIPELINE_RESOLUTION_CANDIDATE_MISSING",
                    "A planned candidate revision is not persisted.",
                    source_work_unit_id=source.source_work_unit_id,
                    context={"candidateId": str(candidate_id), "revision": revision},
                    required_action="Apply the candidate intake transition before resolution.",
                )
            revisions.append(
                ResolutionCandidateSource(
                    candidate_id=candidate_id,
                    revision=int(row["revision"]),
                    entity_kind=str(row["entity_kind"]),
                    normalized_payload=row["normalized_payload"],
                )
            )
        return tuple(revisions)

    def _candidate_intake_actions(
        self,
        connection: Connection,
        source: SucceededWorkOutput,
        correlation_id: str,
    ) -> tuple[tuple[UUID, int], ...]:
        return run_candidate_intake_actions(
            connection,
            self._catalog,
            source.run_id,
            source_work_unit_id=source.source_work_unit_id,
            correlation_id=correlation_id,
        )

    def _prior_clusters(
        self,
        connection: Connection,
        run_id: UUID,
    ) -> tuple[PriorCluster, ...]:
        rows = (
            connection.execute(
                sa.text(
                    "SELECT c.cluster_id, m.candidate_id "
                    "FROM candidates.resolution_clusters c "
                    "JOIN candidates.resolution_runs r ON r.resolution_id = c.resolution_id "
                    "JOIN candidates.resolution_cluster_members m ON m.cluster_id = c.cluster_id "
                    "WHERE r.run_id = :run_id AND NOT EXISTS ("
                    "  SELECT 1 FROM candidates.resolution_runs s "
                    "  WHERE s.supersedes_resolution_id = r.resolution_id) "
                    "ORDER BY c.cluster_id, m.candidate_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
        members: dict[UUID, list[UUID]] = {}
        for row in rows:
            members.setdefault(UUID(str(row["cluster_id"])), []).append(
                UUID(str(row["candidate_id"]))
            )
        clusters = tuple(
            seal_prior_cluster(
                PriorClusterPayload(
                    cluster_id=cluster_id,
                    member_candidate_ids=tuple(candidate_ids),
                )
            )
            for cluster_id, candidate_ids in members.items()
        )
        return tuple(sorted(clusters, key=lambda item: item.cluster_id.hex))

    @staticmethod
    def _candidate_work_drained(connection: Connection, run_id: UUID) -> bool:
        open_work = connection.execute(
            sa.select(sa.func.count())
            .select_from(work_units)
            .where(
                work_units.c.run_id == run_id,
                work_units.c.capability.in_(sorted(_CANDIDATE_PRODUCING_CAPABILITIES)),
                work_units.c.state.in_(sorted(_OPEN_WORK_STATES)),
            )
        ).scalar_one()
        return int(open_work) == 0

    def _publish_batch(
        self,
        connection: Connection,
        *,
        batch_digest: str,
        payload: bytes,
        correlation_id: str,
    ) -> UUID:
        operation_id = uuid5(_ARTIFACT_NAMESPACE, f"resolution_batch:{batch_digest}")
        artifact_id = uuid5(_ARTIFACT_NAMESPACE, f"artifact:{operation_id}")
        try:
            published = self._artifact_publisher.publish_in_transaction(
                connection,
                PublishOwnedArtifact(
                    artifact_id=artifact_id,
                    operation_id=operation_id,
                    producer_identity=_PRODUCER_IDENTITY,
                    artifact_kind=ArtifactKind.DERIVED_ARTIFACT,
                    content=payload,
                    content_type=_BATCH_CONTENT_TYPE,
                    # The batch is derived from candidates of possibly several source
                    # policies, so naming a single policy would be a false attribution.
                    source_policy_digest=None,
                    correlation_id=correlation_id,
                ),
            )
        except OwnedArtifactPublishConflict as exc:
            raise PipelineAdvancementConflict(
                code=exc.code,
                message=exc.message,
                context={"artifactRole": _BATCH_ROLE, **exc.context},
                required_action=exc.required_action,
            ) from exc
        return published.artifact_id

    def _now_utc(self) -> datetime:
        return self._clock().astimezone(UTC)


def resolution_batch_scheduler(
    work_engine: PostgresWorkEngine,
    artifact_publisher: PostgresOwnedArtifactPublisher,
    catalog: PostgresSucceededWorkCatalog,
    *,
    clock: Callable[[], datetime] | None = None,
) -> PostgresResolutionBatchScheduler:
    return PostgresResolutionBatchScheduler(
        work_engine,
        artifact_publisher,
        catalog,
        clock=clock,
    )


def _bundle_documents(
    content: bytes,
    source: SucceededWorkOutput,
) -> Mapping[str, object]:
    try:
        bundle = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _conflict(
            "PIPELINE_RESOLUTION_SNAPSHOT_INVALID",
            "The sealed campaign snapshot is not canonical JSON.",
            source_work_unit_id=source.source_work_unit_id,
            required_action="Restore the exact sealed campaign snapshot.",
        ) from exc
    if not isinstance(bundle, dict) or set(bundle) != {"campaignKey", "components"}:
        raise _conflict(
            "PIPELINE_RESOLUTION_SNAPSHOT_INVALID",
            "The sealed campaign snapshot has an unsupported owner shape.",
            source_work_unit_id=source.source_work_unit_id,
            required_action="Restore the exact sealed campaign snapshot.",
        )
    documents: dict[str, object] = {}
    for component in bundle["components"]:
        if not isinstance(component, dict) or set(component) != {"path", "digest", "content"}:
            raise _conflict(
                "PIPELINE_RESOLUTION_SNAPSHOT_INVALID",
                "A sealed campaign component has an invalid shape.",
                source_work_unit_id=source.source_work_unit_id,
                required_action="Restore the exact sealed campaign snapshot.",
            )
        documents[str(component["path"])] = component["content"]
    return documents


def _quality_profile(
    documents: Mapping[str, object],
    source: SucceededWorkOutput,
) -> QualityProfileDocument:
    document = documents.get(_QUALITY_PROFILE_DOCUMENT)
    if document is None:
        raise _conflict(
            "PIPELINE_RESOLUTION_QUALITY_PROFILE_MISSING",
            "The sealed campaign snapshot declares no quality profile.",
            source_work_unit_id=source.source_work_unit_id,
            required_action=(
                "Author the campaign quality profile and re-seal the run snapshot; entity "
                "resolution never invents a quality policy."
            ),
        )
    try:
        return QualityProfileDocument.model_validate(document)
    except ValueError as exc:
        raise _conflict(
            "PIPELINE_RESOLUTION_QUALITY_PROFILE_INVALID",
            "The campaign quality profile does not satisfy its contract.",
            source_work_unit_id=source.source_work_unit_id,
            context={"causeType": type(exc).__name__},
            required_action="Correct the authored quality profile and re-seal the run.",
        ) from exc


def _market_area(
    documents: Mapping[str, object],
    source: SucceededWorkOutput,
) -> ResolutionMarketArea:
    geography = documents.get(_GEOGRAPHY_DOCUMENT)
    if not isinstance(geography, Mapping):
        raise _conflict(
            "PIPELINE_RESOLUTION_MARKET_AREA_MISSING",
            "The sealed campaign snapshot declares no market area.",
            source_work_unit_id=source.source_work_unit_id,
            required_action="Declare the campaign geography and re-seal the run snapshot.",
        )
    revision = geography.get("geography_revision")
    digest = geography.get("boundary_digest")
    if not isinstance(revision, str) or not isinstance(digest, str):
        raise _conflict(
            "PIPELINE_RESOLUTION_MARKET_AREA_INVALID",
            "The sealed market area has no revision or boundary digest.",
            source_work_unit_id=source.source_work_unit_id,
            required_action="Correct the campaign geography document and re-seal the run.",
        )
    return ResolutionMarketArea(revision=revision, digest=digest)


def _stage_run_id(connection: Connection, run_id: UUID, stage: WorkStage) -> UUID:
    values = tuple(
        connection.execute(
            sa.select(stage_runs.c.stage_run_id).where(
                stage_runs.c.run_id == run_id,
                stage_runs.c.stage == stage.value,
            )
        )
        .scalars()
        .all()
    )
    if len(values) != 1 or not isinstance(values[0], UUID):
        raise _conflict(
            "PIPELINE_RESOLUTION_STAGE_OWNER_INVALID",
            "The run must have exactly one canonical entity-resolution stage owner.",
            context={"runId": str(run_id), "stageRunCount": len(values)},
            required_action="Repair the run's stage ownership before resolution dispatch.",
        )
    return values[0]


def _ensure_stage_running(
    connection: Connection,
    stage_run_id: UUID,
    *,
    now_utc: datetime,
    correlation_id: str,
) -> None:
    state = connection.execute(
        sa.select(stage_runs.c.state).where(stage_runs.c.stage_run_id == stage_run_id)
    ).scalar_one_or_none()
    if state is None:
        raise _conflict(
            "PIPELINE_RESOLUTION_STAGE_OWNER_INVALID",
            "The scheduled stage no longer exists.",
            context={"stageRunId": str(stage_run_id)},
            required_action="Repair the run's stage ownership before resolution dispatch.",
        )
    if state == StageRunState.RUNNING.value:
        return
    if state != StageRunState.PENDING.value:
        raise _conflict(
            "PIPELINE_RESOLUTION_STAGE_STATE_INVALID",
            "The entity-resolution stage is not pending, so new work cannot start it.",
            context={"stageRunId": str(stage_run_id), "stageState": str(state)},
            required_action="Resolve the stage's own state before scheduling resolution.",
        )
    connection.execute(
        sa.update(stage_runs)
        .where(stage_runs.c.stage_run_id == stage_run_id)
        .values(
            state=StageRunState.RUNNING.value,
            revision=stage_runs.c.revision + 1,
            updated_at_utc=now_utc,
        )
    )


def _conflict(
    code: str,
    message: str,
    *,
    source_work_unit_id: UUID | None = None,
    context: Mapping[str, object] | None = None,
    required_action: str,
) -> PipelineAdvancementConflict:
    payload: dict[str, object] = dict(context or {})
    if source_work_unit_id is not None:
        payload["sourceWorkUnitId"] = str(source_work_unit_id)
    return PipelineAdvancementConflict(
        code=code,
        message=message,
        context=payload,
        required_action=required_action,
    )


def run_candidate_intake_actions(
    connection: Connection,
    catalog: PostgresSucceededWorkCatalog,
    run_id: UUID,
    *,
    source_work_unit_id: UUID | None = None,
    correlation_id: str,
) -> tuple[tuple[UUID, int], ...]:
    """Every (candidate, revision) this run's own intake transitions created.

    Candidate revisions carry no run column, so the run-scoped set is the deterministic plan
    of the run's own candidate-producing work units. Replanning is pure, so the scheduler and
    the snapshot applier always agree on the exact set.
    """
    registry = PipelineTransitionRegistry()
    rows = (
        connection.execute(
            sa.select(work_units.c.work_id)
            .where(
                work_units.c.run_id == run_id,
                work_units.c.state == "succeeded",
                work_units.c.capability.in_(sorted(_CANDIDATE_PRODUCING_CAPABILITIES)),
            )
            .order_by(work_units.c.work_id)
        )
        .scalars()
        .all()
    )
    planned: list[tuple[UUID, int]] = []
    for work_id in rows:
        succeeded = catalog.read(connection, work_id)
        plan = registry.plan(succeeded)
        if plan.disposition is not PipelineTransitionDisposition.APPLY:
            continue
        for action in plan.candidate_intake_actions:
            planned.append((action.candidate_id, action.revision))
    if not planned:
        raise _conflict(
            "PIPELINE_RESOLUTION_CANDIDATES_UNRESOLVED",
            "No applied candidate-intake transition belongs to this run.",
            source_work_unit_id=source_work_unit_id,
            context={"runId": str(run_id), "correlationId": correlation_id},
            required_action="Apply candidate intake for this run before resolving.",
        )
    return tuple(sorted(set(planned), key=lambda item: (item[0].hex, item[1])))
