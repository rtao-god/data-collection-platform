"""Persist one entity-resolution snapshot as the run's current resolution and quality verdict.

The applier is the only writer of `candidates.resolution_*` and of resolution-borne
`quality.quality_evaluations`. It never trusts the snapshot on its own: the batch it claims to
have resolved is re-derived from the run's persisted candidate revisions, so a snapshot can
only ever describe candidate revisions this run actually stored.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from collection_application import WorkCapability, WorkStage
from collection_application.pipeline_advancement import (
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    SucceededWorkOutput,
    resolution_snapshot_result_digest,
)
from collection_contracts import QualityProfileDocument, canonical_digest, quality_profile_digest
from collection_infrastructure.postgres.resolution_scheduling import (
    read_prior_clusters,
    run_candidate_intake_actions,
)
from collection_infrastructure.postgres.succeeded_work_catalog import (
    PostgresSucceededWorkCatalog,
)
from collection_infrastructure.resolution import (
    ResolutionAssemblyError,
    ResolutionCandidateSource,
    ResolutionMarketArea,
    assemble_resolution_batch,
)
from collection_infrastructure.resolution.decisions import (
    decision_basis_digest,
    manual_decisions_for_revisions,
)
from resolution_contracts import (
    ManualResolutionDecision,
    ResolutionBatch,
    ResolutionSnapshot,
    decode_resolution_batch,
    decode_resolution_snapshot,
)

_TRANSITION_KEY = "entity-resolution-snapshot-quality-v1"
_OUTPUT_CONTRACT = "entity-resolution-snapshot@1"
_RESOLUTION_NAMESPACE = UUID("9a34d1c8-6f52-5b71-8e0c-2d47a91b6f35")
_QUALITY_PROFILE_DOCUMENT = "quality_profile.yaml"
_GEOGRAPHY_DOCUMENT = "geography.yaml"


class PostgresResolutionSnapshotApplier:
    """Atomically records the resolved clusters, membership and per-candidate verdicts."""

    def __init__(
        self,
        catalog: PostgresSucceededWorkCatalog,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._catalog = catalog
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(
        self,
        connection: Connection,
        source: SucceededWorkOutput,
        plan: PipelineTransitionPlan,
        *,
        correlation_id: str,
    ) -> str:
        if not self._valid_plan(source, plan):
            raise PipelineAdvancementConflict(
                code="PIPELINE_RESOLUTION_PLAN_INVALID",
                message="The resolution applier received unrelated or incomplete evidence.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "transitionKey": plan.transition_key,
                    "actionCount": len(plan.resolution_snapshot_actions),
                },
                required_action="Recompute the resolution transition from the exact snapshot.",
            )
        action = plan.resolution_snapshot_actions[0]
        if source.output_content is None or source.resolution_batch_content is None:
            raise _conflict(
                "PIPELINE_RESOLUTION_CONTENT_MISSING",
                "Resolution persistence requires the durable batch and snapshot bytes.",
                source,
                required_action="Restore the immutable resolution artifacts before advancing.",
            )
        try:
            snapshot = decode_resolution_snapshot(source.output_content)
            batch = decode_resolution_batch(source.resolution_batch_content)
        except (TypeError, ValueError) as exc:
            raise _conflict(
                "PIPELINE_RESOLUTION_CONTENT_INVALID",
                "The resolution batch or snapshot violates its canonical contract.",
                source,
                context={"causeType": type(exc).__name__},
                required_action="Repair the immutable resolution artifacts at their producer.",
            ) from exc
        if snapshot.snapshot_digest != action.snapshot_digest or (
            batch.batch_digest != action.batch_digest
        ):
            raise _conflict(
                "PIPELINE_RESOLUTION_DIGEST_CONFLICT",
                "The durable resolution bytes differ from the registered plan.",
                source,
                context={
                    "snapshotDigest": snapshot.snapshot_digest,
                    "planSnapshotDigest": action.snapshot_digest,
                },
                required_action="Re-register advancement from the exact durable snapshot.",
            )
        profile = self._profile(source)
        existing = self._existing_resolution(connection, source.run_id, snapshot.snapshot_digest)
        if existing is not None:
            # This exact snapshot is already the run's recorded resolution, so a replay adds
            # nothing and must not be re-validated against later owner state.
            return resolution_snapshot_result_digest(plan)
        decisions = manual_decisions_for_revisions(
            connection,
            self._persisted_revisions(connection, source),
        )
        expected = self._expected_batch(connection, source, batch, profile, decisions=decisions)
        if expected.batch_digest != batch.batch_digest:
            raise _conflict(
                "PIPELINE_RESOLUTION_BATCH_NOT_DERIVABLE",
                (
                    "The snapshot was not produced from this run's persisted candidate "
                    "revisions, decisions and prior resolution."
                ),
                source,
                context={
                    "batchDigest": batch.batch_digest,
                    "expectedBatchDigest": expected.batch_digest,
                },
                required_action=(
                    "Re-publish the resolution batch from the exact persisted revisions and "
                    "resolve it again."
                ),
            )
        now_utc = self._now_utc()
        resolution_id = uuid5(
            _RESOLUTION_NAMESPACE,
            f"{source.run_id}:{snapshot.snapshot_digest}",
        )
        supersedes = self._current_resolution(connection, source.run_id)
        policy_digest = quality_profile_digest(profile)
        self._insert_resolution(
            connection,
            source=source,
            resolution_id=resolution_id,
            snapshot=snapshot,
            supersedes=supersedes,
            policy_digest=policy_digest,
            # The resolution is recorded together with the exact operator decisions it
            # reflected, so a later decision can be told apart from the one that produced it.
            decision_basis_digest=decision_basis_digest(decisions),
            now_utc=now_utc,
            correlation_id=correlation_id,
        )
        candidate_revisions = self._revision_coordinates(connection, source.run_id)
        self._insert_pairs(connection, resolution_id, snapshot)
        self._insert_clusters(connection, resolution_id, snapshot)
        self._insert_quality(
            connection,
            resolution_id=resolution_id,
            snapshot=snapshot,
            policy_digest=policy_digest,
            candidate_revisions=candidate_revisions,
            now_utc=now_utc,
            correlation_id=correlation_id,
        )
        return resolution_snapshot_result_digest(plan)

    @staticmethod
    def _valid_plan(source: SucceededWorkOutput, plan: PipelineTransitionPlan) -> bool:
        return all(
            (
                len(plan.resolution_snapshot_actions) == 1,
                plan.source_work_unit_id == source.source_work_unit_id,
                plan.transition_key == _TRANSITION_KEY,
                plan.disposition is PipelineTransitionDisposition.APPLY,
                plan.blocker is None,
                not plan.website_actions,
                not plan.extraction_actions,
                not plan.normalization_actions,
                not plan.source_record_mapping_actions,
                not plan.candidate_intake_actions,
                source.stage is WorkStage.ENTITY_RESOLUTION,
                source.capability == WorkCapability.ENTITY_RESOLUTION.value,
                source.output_contract == _OUTPUT_CONTRACT,
                source.output_artifact.role == "resolution_snapshot",
            )
        )

    @staticmethod
    def _profile(source: SucceededWorkOutput) -> QualityProfileDocument:
        documents = _bundle_documents(source)
        document = documents.get(_QUALITY_PROFILE_DOCUMENT)
        if document is None:
            raise _conflict(
                "PIPELINE_RESOLUTION_QUALITY_PROFILE_MISSING",
                "The sealed campaign snapshot declares no quality profile.",
                source,
                required_action="Author the campaign quality profile and re-seal the run.",
            )
        try:
            return QualityProfileDocument.model_validate(document)
        except ValueError as exc:
            raise _conflict(
                "PIPELINE_RESOLUTION_QUALITY_PROFILE_INVALID",
                "The campaign quality profile does not satisfy its contract.",
                source,
                context={"causeType": type(exc).__name__},
                required_action="Correct the authored quality profile and re-seal the run.",
            ) from exc

    def _expected_batch(
        self,
        connection: Connection,
        source: SucceededWorkOutput,
        batch: ResolutionBatch,
        profile: QualityProfileDocument,
        *,
        decisions: Sequence[ManualResolutionDecision],
    ) -> ResolutionBatch:
        revisions = self._persisted_revisions(connection, source)
        try:
            # Neither the supplied prior clusters nor the supplied manual decisions are trusted:
            # both are re-derived from the owner's own persisted state, so a sealed payload can
            # only ever describe evidence this run actually stored.
            returns = assemble_resolution_batch(
                run_id=source.run_id,
                market_area=ResolutionMarketArea(
                    revision=batch.market_area_revision,
                    digest=batch.market_area_digest,
                ),
                quality_profile=profile,
                revisions=revisions,
                prior_clusters=read_prior_clusters(connection, source.run_id),
                manual_decisions=decisions,
            )
        except ResolutionAssemblyError as exc:
            raise _conflict(
                exc.code,
                str(exc) or "Persisted revisions cannot be re-expressed as a resolution batch.",
                source,
                context=exc.context,
                required_action="Correct the persisted revision or the campaign quality profile.",
            ) from exc
        return returns

    def _persisted_revisions(
        self,
        connection: Connection,
        source: SucceededWorkOutput,
    ) -> tuple[ResolutionCandidateSource, ...]:
        coordinates = self._revision_coordinates(connection, source.run_id)
        revisions: list[ResolutionCandidateSource] = []
        for candidate_id, revision in coordinates:
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
                    "A persisted candidate revision vanished during resolution.",
                    source,
                    context={"candidateId": str(candidate_id), "revision": revision},
                    required_action="Restore the immutable candidate revision.",
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

    def _revision_coordinates(
        self,
        connection: Connection,
        run_id: UUID,
    ) -> tuple[tuple[UUID, int], ...]:
        """The exact (candidate, revision) coordinates this run's intake transitions created."""
        return run_candidate_intake_actions(
            connection,
            self._catalog,
            run_id,
            correlation_id=f"resolution-applier:{run_id}",
        )

    def _existing_resolution(
        self,
        connection: Connection,
        run_id: UUID,
        snapshot_digest: str,
    ) -> UUID | None:
        value = connection.execute(
            sa.text(
                "SELECT resolution_id FROM candidates.resolution_runs "
                "WHERE run_id = :run_id AND snapshot_digest = :snapshot_digest"
            ),
            {"run_id": run_id, "snapshot_digest": snapshot_digest},
        ).scalar_one_or_none()
        return None if value is None else UUID(str(value))

    def _current_resolution(self, connection: Connection, run_id: UUID) -> UUID | None:
        rows = (
            connection.execute(
                sa.text(
                    "SELECT r.resolution_id FROM candidates.resolution_runs AS r "
                    "WHERE r.run_id = :run_id AND NOT EXISTS ("
                    "  SELECT 1 FROM candidates.resolution_runs AS s "
                    "  WHERE s.supersedes_resolution_id = r.resolution_id)"
                ),
                {"run_id": run_id},
            )
            .scalars()
            .all()
        )
        if len(rows) > 1:
            raise PipelineAdvancementConflict(
                code="PIPELINE_RESOLUTION_HISTORY_AMBIGUOUS",
                message="The run has more than one current resolution.",
                context={
                    "runId": str(run_id),
                    "resolutionIds": sorted(str(item) for item in rows),
                },
                required_action="Repair the resolution supersession chain under its owner.",
            )
        return None if not rows else UUID(str(rows[0]))

    def _insert_resolution(
        self,
        connection: Connection,
        *,
        source: SucceededWorkOutput,
        resolution_id: UUID,
        snapshot: ResolutionSnapshot,
        supersedes: UUID | None,
        policy_digest: str,
        decision_basis_digest: str,
        now_utc: datetime,
        correlation_id: str,
    ) -> None:
        connection.execute(
            sa.text(
                "INSERT INTO candidates.resolution_runs ("
                "resolution_id, run_id, work_id, batch_id, batch_digest, snapshot_digest,"
                "market_area_revision, market_area_digest, quality_policy_digest,"
                "decision_basis_digest, supersedes_resolution_id, applied_at_utc,"
                "correlation_id"
                ") VALUES ("
                ":resolution_id, :run_id, :work_id, :batch_id, :batch_digest, :snapshot_digest,"
                ":market_area_revision, :market_area_digest, :quality_policy_digest,"
                ":decision_basis_digest, :supersedes_resolution_id, :applied_at_utc,"
                ":correlation_id)"
            ),
            {
                "resolution_id": resolution_id,
                "run_id": source.run_id,
                "work_id": source.source_work_unit_id,
                "batch_id": snapshot.batch_id,
                "batch_digest": snapshot.batch_digest,
                "snapshot_digest": snapshot.snapshot_digest,
                "market_area_revision": snapshot.market_area_revision,
                "market_area_digest": snapshot.market_area_digest,
                "quality_policy_digest": policy_digest,
                "decision_basis_digest": decision_basis_digest,
                "supersedes_resolution_id": supersedes,
                "applied_at_utc": now_utc,
                "correlation_id": correlation_id,
            },
        )

    @staticmethod
    def _insert_pairs(
        connection: Connection,
        resolution_id: UUID,
        snapshot: ResolutionSnapshot,
    ) -> None:
        for pair in snapshot.pair_resolutions:
            connection.execute(
                sa.text(
                    "INSERT INTO candidates.resolution_pairs ("
                    "resolution_id, pair_id, left_candidate_id, right_candidate_id,"
                    "disposition, reason_codes, evidence_strength"
                    ") VALUES (:resolution_id, :pair_id, :left_id, :right_id, :disposition,"
                    ":reason_codes, :evidence_strength)"
                ),
                {
                    "resolution_id": resolution_id,
                    "pair_id": pair.features.pair_id,
                    "left_id": pair.features.left_candidate_id,
                    "right_id": pair.features.right_candidate_id,
                    "disposition": pair.disposition.value,
                    "reason_codes": list(pair.reason_codes),
                    "evidence_strength": pair.evidence_strength,
                },
            )
        for edge in snapshot.blocked_match_edges:
            connection.execute(
                sa.text(
                    "INSERT INTO candidates.resolution_blocked_edges ("
                    "resolution_id, blocked_edge_id, attempted_pair_id, separation_pair_id,"
                    "reason_code) VALUES (:resolution_id, :blocked_edge_id, :attempted_pair_id,"
                    ":separation_pair_id, :reason_code)"
                ),
                {
                    "resolution_id": resolution_id,
                    "blocked_edge_id": edge.blocked_edge_id,
                    "attempted_pair_id": edge.attempted_pair_id,
                    "separation_pair_id": edge.separation_pair_id,
                    "reason_code": edge.reason_code,
                },
            )

    @staticmethod
    def _insert_clusters(
        connection: Connection,
        resolution_id: UUID,
        snapshot: ResolutionSnapshot,
    ) -> None:
        for cluster in snapshot.clusters:
            connection.execute(
                sa.text(
                    "INSERT INTO candidates.resolution_clusters ("
                    "resolution_id, cluster_id, lineage_kind, parent_cluster_ids,"
                    "parent_membership_digest, blocked_match_edge_ids"
                    ") VALUES (:resolution_id, :cluster_id, :lineage_kind, :parent_cluster_ids,"
                    ":parent_membership_digest, :blocked_match_edge_ids)"
                ),
                {
                    "resolution_id": resolution_id,
                    "cluster_id": cluster.cluster_id,
                    "lineage_kind": cluster.lineage_kind.value,
                    "parent_cluster_ids": [str(item) for item in cluster.parent_cluster_ids],
                    "parent_membership_digest": cluster.parent_membership_digest,
                    "blocked_match_edge_ids": list(cluster.blocked_match_edge_ids),
                },
            )
            for member in cluster.member_candidate_ids:
                connection.execute(
                    sa.text(
                        "INSERT INTO candidates.resolution_cluster_members ("
                        "resolution_id, cluster_id, candidate_id"
                        ") VALUES (:resolution_id, :cluster_id, :candidate_id)"
                    ),
                    {
                        "resolution_id": resolution_id,
                        "cluster_id": cluster.cluster_id,
                        "candidate_id": member,
                    },
                )

    @staticmethod
    def _insert_quality(
        connection: Connection,
        *,
        resolution_id: UUID,
        snapshot: ResolutionSnapshot,
        policy_digest: str,
        candidate_revisions: tuple[tuple[UUID, int], ...],
        now_utc: datetime,
        correlation_id: str,
    ) -> None:
        revisions = dict(candidate_revisions)
        for cluster, assessment in zip(
            snapshot.clusters, snapshot.quality_assessments, strict=True
        ):
            blockers = tuple(sorted({issue.code for issue in assessment.issues}))
            for member in cluster.member_candidate_ids:
                revision = revisions.get(member)
                if revision is None:
                    raise PipelineAdvancementConflict(
                        code="PIPELINE_RESOLUTION_MEMBER_UNKNOWN",
                        message="A resolved cluster member has no persisted candidate revision.",
                        context={
                            "resolutionId": str(resolution_id),
                            "candidateId": str(member),
                        },
                        required_action=(
                            "Re-publish the resolution batch from the exact persisted revisions."
                        ),
                    )
                evaluation_digest = canonical_digest(
                    {
                        "resolutionId": str(resolution_id),
                        "snapshotDigest": snapshot.snapshot_digest,
                        "policyDigest": policy_digest,
                        "candidateId": str(member),
                        "candidateRevision": revision,
                        "clusterId": str(cluster.cluster_id),
                        "assessment": assessment.model_dump(mode="json", by_alias=True),
                    }
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO quality.quality_evaluations ("
                        "evaluation_id, candidate_id, candidate_revision, policy_digest,"
                        "export_eligible, blockers, evaluation_digest, evaluated_at_utc,"
                        "correlation_id, resolution_id"
                        ") VALUES (:evaluation_id, :candidate_id, :candidate_revision,"
                        ":policy_digest, :export_eligible, :blockers, :evaluation_digest,"
                        ":evaluated_at_utc, :correlation_id, :resolution_id)"
                    ),
                    {
                        "evaluation_id": uuid5(
                            _RESOLUTION_NAMESPACE,
                            f"{resolution_id}:{member}:{revision}:quality",
                        ),
                        "candidate_id": member,
                        "candidate_revision": revision,
                        "policy_digest": policy_digest,
                        "export_eligible": assessment.export_eligible,
                        "blockers": list(blockers),
                        "evaluation_digest": evaluation_digest,
                        "evaluated_at_utc": now_utc,
                        "correlation_id": correlation_id,
                        "resolution_id": resolution_id,
                    },
                )

    def _now_utc(self) -> datetime:
        return self._clock().astimezone(UTC)


def resolution_snapshot_transition_appliers(
    catalog: PostgresSucceededWorkCatalog,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Callable[..., str]]:
    applier = PostgresResolutionSnapshotApplier(catalog, clock=clock)
    return {_TRANSITION_KEY: applier}


def _bundle_documents(source: SucceededWorkOutput) -> Mapping[str, object]:
    if source.campaign_bundle_content is None:
        raise _conflict(
            "PIPELINE_RESOLUTION_SNAPSHOT_MISSING",
            "Resolution persistence requires the exact sealed campaign snapshot.",
            source,
            required_action="Restore the run-bound campaign snapshot before resolution.",
        )
    try:
        bundle = json.loads(source.campaign_bundle_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _conflict(
            "PIPELINE_RESOLUTION_SNAPSHOT_INVALID",
            "The sealed campaign snapshot is not canonical JSON.",
            source,
            required_action="Restore the exact sealed campaign snapshot.",
        ) from exc
    if not isinstance(bundle, dict) or set(bundle) != {"campaignKey", "components"}:
        raise _conflict(
            "PIPELINE_RESOLUTION_SNAPSHOT_INVALID",
            "The sealed campaign snapshot has an unsupported owner shape.",
            source,
            required_action="Restore the exact sealed campaign snapshot.",
        )
    return {
        str(component["path"]): component["content"]
        for component in bundle["components"]
        if isinstance(component, dict) and set(component) == {"path", "digest", "content"}
    }


def _conflict(
    code: str,
    message: str,
    source: SucceededWorkOutput,
    *,
    context: Mapping[str, object] | None = None,
    required_action: str,
) -> PipelineAdvancementConflict:
    payload: dict[str, object] = dict(context or {})
    payload["sourceWorkUnitId"] = str(source.source_work_unit_id)
    return PipelineAdvancementConflict(
        code=code,
        message=message,
        context=payload,
        required_action=required_action,
    )
