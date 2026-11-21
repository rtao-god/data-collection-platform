from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from candidate_materialization import materialize_candidate
from collection_application.candidate_geography import (
    CandidateGeographyDecision,
    CandidateGeographyError,
    classify_candidate_geography,
    read_candidate_geography_boundary,
)
from collection_application.pipeline_advancement import (
    CandidateIntakeAction,
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    SucceededWorkOutput,
    candidate_intake_result_digest,
)
from collection_contracts import (
    CandidateRevision,
    EvidenceLocatorKind,
    EvidenceReference,
    FieldObservation,
    FieldObservationPayload,
    NormalizationProfile,
    ObservationBatch,
    ObservationBatchPayload,
    SourceRecordObservation,
    SourceRecordObservationBatch,
    candidate_snapshot_digest,
    decode_manual_record_lineage,
    decode_source_record_observation_batch,
    seal_field_observation,
    seal_observation_batch,
    verify_observation_batch,
)
from collection_contracts.campaign_config import SourcePolicy
from collection_contracts.review_identity import review_case_id
from collection_infrastructure.postgres.resolution_scheduling import (
    PostgresResolutionBatchScheduler,
)
from resolution_contracts import (
    CandidateField,
    EntityKind,
    GeographyCoverage,
    GeographyReference,
)

_CANDIDATE_INTAKE_TRANSITION_KEY = "field-observation-batch-candidate-intake-v1"
_SOURCE_RECORD_INTAKE_TRANSITION_KEY = "source-record-observation-batch-candidate-intake-v1"
_OUTPUT_CONTRACT = "field-observation-batch@1"
_SOURCE_RECORD_OUTPUT_CONTRACT = "source-record-observation-batch@1"
_CANDIDATE_INTAKE_OWNER = "CandidateIntake"
_REVIEW_OPEN_STATE = "open"
_REVIEW_OPEN_REASON = "CANDIDATE_REQUIRES_REVIEW"
_ENTITY_KIND_UNRESOLVED = "source_entity_kind_unresolved"
_CANDIDATE_INTAKE_TRANSITION_KEYS = (
    _CANDIDATE_INTAKE_TRANSITION_KEY,
    _SOURCE_RECORD_INTAKE_TRANSITION_KEY,
)
_BLOCKED_GEOGRAPHY_REVISION = "unclassified"
_BLOCKED_GEOGRAPHY_DIGEST = "sha256:" + "0" * 64
_UNRESOLVED_ENTITY_KIND = EntityKind.ORGANIZATION
_SOURCE_ENTITY_KINDS = {
    "organization": EntityKind.ORGANIZATION,
    "place": EntityKind.PLACE,
    "provider": EntityKind.PROVIDER,
}
_SOURCE_MAPPING_PROFILE_FIELDS = frozenset(
    {
        "campaignKey",
        "entityKind",
        "fieldProjection",
        "mappingRevision",
        "runId",
        "sourceBindingKey",
        "sourceKey",
        "sourcePolicyDigest",
    }
)


class PostgresCandidateIntakeApplier:
    """Atomically persists one immutable candidate revision and its review admission."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        resolution_scheduler: PostgresResolutionBatchScheduler | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._resolution_scheduler = resolution_scheduler

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
                code="PIPELINE_CANDIDATE_INTAKE_PLAN_INVALID",
                message="The candidate intake applier received unrelated or incomplete evidence.",
                context={
                    "sourceWorkUnitId": str(source.source_work_unit_id),
                    "transitionKey": plan.transition_key,
                    "actionCount": len(plan.candidate_intake_actions),
                },
                required_action=(
                    "Recompute candidate intake from the exact observation batch and lineage."
                ),
            )
        if plan.transition_key == _SOURCE_RECORD_INTAKE_TRANSITION_KEY:
            return self._intake_source_record(
                connection,
                source,
                plan,
                correlation_id=correlation_id,
            )
        action = plan.candidate_intake_actions[0]
        if source.output_content is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_BATCH_MISSING",
                message="Candidate intake requires the exact observation batch bytes.",
                context={"sourceWorkUnitId": str(source.source_work_unit_id)},
                required_action="Re-run normalization so its output artifact is durable.",
            )
        try:
            batch = ObservationBatch.model_validate_json(source.output_content)
            verify_observation_batch(batch)
        except (TypeError, ValueError) as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_BATCH_INVALID",
                message="The observation batch output is not canonical or self-consistent.",
                context={"causeType": type(exc).__name__},
                required_action="Repair the normalization output at its producing stage.",
            ) from exc
        if batch.content_digest != source.output_digest:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_BATCH_DIGEST_CONFLICT",
                message="The observation batch digest differs from the succeeded output digest.",
                context={
                    "batchDigest": batch.content_digest,
                    "outputDigest": source.output_digest,
                },
                required_action="Recompute candidate intake from the exact durable batch bytes.",
            )
        if source.manual_record_lineage_content is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_LINEAGE_MISSING",
                message="Candidate intake requires the exact verified manual-record lineage.",
                context={"sourceWorkUnitId": str(source.source_work_unit_id)},
                required_action=(
                    "Restore the exact verified manual-record lineage before advancement."
                ),
            )
        try:
            lineage = decode_manual_record_lineage(source.manual_record_lineage_content)
        except (TypeError, ValueError) as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_LINEAGE_INVALID",
                message="The manual-record lineage violates its canonical contract.",
                context={"causeType": type(exc).__name__},
                required_action="Repair the lineage at its producing stage.",
            ) from exc
        if lineage.run_id != source.run_id:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_LINEAGE_RUN_CONFLICT",
                message="The manual-record lineage belongs to another run.",
                context={
                    "lineageRunId": str(lineage.run_id),
                    "workRunId": str(source.run_id),
                },
                required_action="Correct the lineage or the succeeded work provenance.",
            )
        if lineage.manual_record_content_digest != batch.source_record_id:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_RECORD_IDENTITY_CONFLICT",
                message="The observation batch does not belong to the bound manual record.",
                context={
                    "batchSourceRecordId": batch.source_record_id,
                    "lineageRecordDigest": lineage.manual_record_content_digest,
                },
                required_action=(
                    "Repair the manual-record lineage or the normalization output at its owner."
                ),
            )
        try:
            entity_kind = EntityKind(lineage.expected_entity_kind)
        except ValueError as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_ENTITY_KIND_UNSUPPORTED",
                message="The verified lineage declares an unsupported entity kind.",
                context={"expectedEntityKind": lineage.expected_entity_kind},
                required_action="Correct the manual-record lineage at its producing stage.",
            ) from exc
        if source.source_binding_key is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_SOURCE_KEY_MISSING",
                message="Candidate materialization requires the exact source binding key.",
                context={"sourceWorkUnitId": str(source.source_work_unit_id)},
                required_action="Repair the run snapshot binding before retrying intake.",
            )
        if source.normalization_profile_content is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_PROFILE_MISSING",
                message="Candidate intake requires the exact normalization profile.",
                context={"sourceWorkUnitId": str(source.source_work_unit_id)},
                required_action=(
                    "Restore the exact normalization profile input before advancement."
                ),
            )
        try:
            profile = NormalizationProfile.model_validate_json(
                source.normalization_profile_content, strict=True
            )
            field_projection = _candidate_field_projection(batch, profile)
        except (TypeError, ValueError) as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_PROJECTION_UNSUPPORTED",
                message=(
                    "The exact normalization profile does not authorize an explicit "
                    "candidate projection."
                ),
                context={"causeType": type(exc).__name__},
                required_action=(
                    "Publish an explicit candidate field projection in the normalization "
                    "profile; do not guess missing projections."
                ),
            ) from exc
        now_utc = self._now_utc()
        try:
            revision, _ = materialize_candidate(
                batch,
                candidate_id=action.candidate_id,
                entity_kind=entity_kind,
                source_key=source.source_binding_key,
                source_artifact_ids=action.source_artifact_ids,
                observation_artifact_ids=_observation_artifact_ids(
                    connection, observations=batch.observations
                ),
                field_projection=field_projection,
                geography=self._candidate_geography(
                    source=source,
                    observations=_geography_observations(batch.observations),
                ).as_reference(),
                recorded_at_utc=now_utc,
                correlation_id=correlation_id,
                revision=action.revision,
            )
        except ValueError as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_MATERIALIZATION_FAILED",
                message="Exact observations cannot form a valid candidate revision.",
                context={"causeType": type(exc).__name__},
                required_action="Repair the observation batch or its exact lineage.",
            ) from exc
        policy = self._export_policy(source)
        revision = _revision_with_export_facts(revision, policy=policy)
        self._insert_candidate(
            connection,
            entity_kind=entity_kind,
            now_utc=now_utc,
            correlation_id=correlation_id,
            action=action,
        )
        self._insert_revision(connection, revision=revision)
        self._insert_evidence(connection, revision=revision)
        # Quality is owned by the entity-resolution stage: intake records the candidate and
        # opens its review case, and never declares an export verdict it cannot measure.
        self._insert_review_case(
            connection,
            revision=revision,
            now_utc=now_utc,
            correlation_id=correlation_id,
        )
        self._schedule_resolution(connection, source=source, correlation_id=correlation_id)
        return candidate_intake_result_digest(plan)

    def _schedule_resolution(
        self,
        connection: Connection,
        *,
        source: SucceededWorkOutput,
        correlation_id: str,
    ) -> None:
        """Hand the run to entity resolution once no further revision can appear.

        Resolution covers every persisted revision of the run, so it is dispatched from the
        intake that leaves the run's candidate-producing work drained. The scheduler is
        idempotent: replaying this transition re-derives the same batch identity.
        """
        if self._resolution_scheduler is None:
            return
        self._resolution_scheduler.schedule_if_drained(
            connection,
            source=source,
            correlation_id=correlation_id,
        )

    def _intake_source_record(
        self,
        connection: Connection,
        source: SucceededWorkOutput,
        plan: PipelineTransitionPlan,
        *,
        correlation_id: str,
    ) -> str:
        """Persist a candidate from a verified source-native observation batch.

        Provenance comes from the source record itself: binding key, policy digest,
        dataset release and the record artifact identity. No manual-record lineage and
        no HTML extracted record is fabricated.
        """
        action = plan.candidate_intake_actions[0]
        if source.output_content is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_BATCH_MISSING",
                message="Source-record intake requires the exact batch bytes.",
                context={"sourceWorkUnitId": str(source.source_work_unit_id)},
                required_action="Re-run source-record mapping so its output is durable.",
            )
        try:
            batch = decode_source_record_observation_batch(source.output_content)
        except (TypeError, ValueError) as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_BATCH_INVALID",
                message="The source-record batch is not canonical or self-consistent.",
                context={"causeType": type(exc).__name__},
                required_action="Repair the mapping output at its producing stage.",
            ) from exc
        # The batch content digest covers the batch payload; the succeeded output digest
        # covers the artifact bytes. Both must hold, so compare the re-derived payload
        # digest against the batch's own and the artifact bytes against the output.
        if _digest_bytes(source.output_content) != source.output_digest:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_BATCH_DIGEST_CONFLICT",
                message="The durable batch bytes differ from the succeeded output digest.",
                context={"outputDigest": source.output_digest},
                required_action="Recompute intake from the exact durable batch bytes.",
            )
        if batch.run_id != source.run_id:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_RUN_CONFLICT",
                message="The source-record batch belongs to another run.",
                context={"batchRunId": str(batch.run_id), "workRunId": str(source.run_id)},
                required_action="Repair the batch or work provenance.",
            )
        if source.source_binding_key is None or batch.source_binding_key != (
            source.source_binding_key
        ):
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_BINDING_CONFLICT",
                message="The source-record batch binding differs from the succeeded work.",
                context={
                    "batchBindingKey": batch.source_binding_key,
                    "workBindingKey": source.source_binding_key,
                },
                required_action="Repair the run snapshot binding before retrying intake.",
            )
        now_utc = self._now_utc()
        try:
            entity_kind, field_projection, kind_resolved = _resolve_source_mapping_policy(source)
        except ValueError as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_MAPPING_POLICY_INVALID",
                message="The run snapshot declares no usable source mapping policy.",
                context={"causeType": type(exc).__name__},
                required_action="Repair the source mapping profile at its owner.",
            ) from exc
        self._insert_candidate(
            connection,
            entity_kind=entity_kind,
            now_utc=now_utc,
            correlation_id=correlation_id,
            action=action,
        )
        revision_number, is_new_revision = self._source_record_revision(
            connection,
            action=action,
        )
        try:
            revision, _ = materialize_candidate(
                _source_record_as_observations(batch),
                candidate_id=action.candidate_id,
                entity_kind=entity_kind,
                source_key=batch.source_key,
                source_artifact_ids=_source_record_artifact_ids(action, batch),
                observation_artifact_ids=_source_record_observation_artifact_ids(batch),
                field_projection=field_projection,
                geography=self._candidate_geography(
                    source=source,
                    observations=_geography_observations(batch.observations),
                ).as_reference(),
                recorded_at_utc=now_utc,
                correlation_id=correlation_id,
                revision=revision_number,
            )
        except ValueError as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_MATERIALIZATION_FAILED",
                message="Exact source observations cannot form a valid candidate revision.",
                context={"causeType": type(exc).__name__},
                required_action="Repair the source-record batch or its evidence.",
            ) from exc
        policy = self._export_policy(source)
        revision = _revision_with_export_facts(
            revision, policy=policy, source_observations=batch.observations
        )
        self._insert_revision(connection, revision=revision)
        self._insert_evidence(connection, revision=revision)
        if is_new_revision:
            self._record_source_entity_revision(
                connection,
                action=action,
                revision_number=revision_number,
                now_utc=now_utc,
                correlation_id=correlation_id,
            )
        # Quality is owned by the entity-resolution stage: intake records the candidate and
        # opens its review case, and never declares an export verdict it cannot measure.
        self._insert_review_case(
            connection,
            revision=revision,
            now_utc=now_utc,
            correlation_id=correlation_id,
            extra_reason_codes=() if kind_resolved else (_ENTITY_KIND_UNRESOLVED,),
        )
        self._schedule_resolution(connection, source=source, correlation_id=correlation_id)
        return candidate_intake_result_digest(plan)

    @staticmethod
    def _valid_plan(source: SucceededWorkOutput, plan: PipelineTransitionPlan) -> bool:
        if len(plan.candidate_intake_actions) != 1:
            return False
        if plan.transition_key == _SOURCE_RECORD_INTAKE_TRANSITION_KEY:
            return all(
                (
                    plan.source_work_unit_id == source.source_work_unit_id,
                    source.output_contract == _SOURCE_RECORD_OUTPUT_CONTRACT,
                    plan.disposition is PipelineTransitionDisposition.APPLY,
                    plan.blocker is None,
                    not plan.website_actions,
                    not plan.extraction_actions,
                    not plan.normalization_actions,
                    source.output_artifact.role == "source_record_observation_batch",
                )
            )
        return all(
            (
                plan.source_work_unit_id == source.source_work_unit_id,
                plan.transition_key == _CANDIDATE_INTAKE_TRANSITION_KEY,
                source.output_contract == _OUTPUT_CONTRACT,
                plan.disposition is PipelineTransitionDisposition.APPLY,
                plan.blocker is None,
                not plan.website_actions,
                not plan.extraction_actions,
                not plan.normalization_actions,
                source.output_artifact.role == "field_observation_batch",
            )
        )

    def _now_utc(self) -> datetime:
        return self._clock().astimezone(UTC)

    @staticmethod
    def _export_policy(source: SucceededWorkOutput) -> _ExportPolicy:
        """Decide the export-side usage policy of the run's exact source policy.

        The collector's `legal_status` is not a redistribution licence. Only an approved
        source maps to a redistributable usage policy; `reference_only` stays link-only,
        and `research_only`/`blocked`/`expired` cannot enter an export at all.
        """

        if source.campaign_bundle_content is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_SOURCE_POLICY_MISSING",
                message="Candidate intake requires the exact run source policy.",
                context={"sourceWorkUnitId": str(source.source_work_unit_id)},
                required_action="Restore the run config bundle before advancing intake.",
            )
        try:
            bundle = _decode_campaign_bundle(source.campaign_bundle_content)
            policy = _source_policy_for_binding(
                bundle, source.source_binding_key, _row_context_key=source.run_id
            )
        except (TypeError, ValueError) as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_SOURCE_POLICY_INVALID",
                message="The run snapshot has no valid source policy for this binding.",
                context={"causeType": type(exc).__name__},
                required_action="Repair the campaign source policy at its owner.",
            ) from exc
        # The exact legal status is recorded, never converted: intake keeps the candidate so
        # internal evidence, quality and review stay possible, while the sealed record states
        # precisely which redistribution right, if any, the source grant carries.
        return _ExportPolicy(
            source_key=policy.source_key,
            legal_status=policy.legal_status,
            usage_policy=_USAGE_POLICY_BY_LEGAL_STATUS.get(policy.legal_status),
            attribution=_ATTRIBUTION_BY_SOURCE.get(policy.source_key)
            if policy.attribution_required
            else None,
        )

    @staticmethod
    def _candidate_geography(
        *,
        source: SucceededWorkOutput,
        observations: tuple[tuple[str, str, str], ...],
    ) -> _CandidateGeography:
        """Decide the candidate's exact market-area coverage or fail with owner context.

        The run snapshot is the only authority for the boundary: without it the export
        gate would receive an unbacked market area, so a missing declaration is a hard
        failure rather than an `UNKNOWN` default.
        """

        if source.campaign_bundle_content is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_GEOGRAPHY_SNAPSHOT_MISSING",
                message="Candidate geography requires the exact run campaign snapshot.",
                context={"sourceWorkUnitId": str(source.source_work_unit_id)},
                required_action=(
                    "Restore the run config bundle before advancing candidate intake."
                ),
            )
        try:
            bundle = _decode_campaign_bundle(source.campaign_bundle_content)
            boundary = read_candidate_geography_boundary(bundle)
            if boundary is None:
                raise PipelineAdvancementConflict(
                    code="PIPELINE_CANDIDATE_GEOGRAPHY_UNCLASSIFIABLE",
                    message="The campaign declares no verified market-area boundary.",
                    context={"campaignKey": bundle.get("campaignKey")},
                    required_action=(
                        "Declare the exact boundary artifact for this campaign; a "
                        "candidate cannot enter a catalog without proven coverage."
                    ),
                )
            decision = classify_candidate_geography(boundary=boundary, observations=observations)
        except CandidateGeographyError as exc:
            raise PipelineAdvancementConflict(
                code=exc.code,
                message="The candidate geography cannot be classified from verified evidence.",
                context={str(key): str(value) for key, value in exc.context.items()},
                required_action=(
                    "Repair the geography declaration or the candidate's own coordinate "
                    "evidence at its producing owner."
                ),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_GEOGRAPHY_SNAPSHOT_INVALID",
                message="The run campaign snapshot is not a valid canonical bundle.",
                context={"causeType": type(exc).__name__},
                required_action="Repair the run config snapshot at its producing stage.",
            ) from exc
        return _CandidateGeography(decision=decision)

    @staticmethod
    def _insert_candidate(
        connection: Connection,
        *,
        entity_kind: EntityKind,
        now_utc: datetime,
        correlation_id: str,
        action: CandidateIntakeAction,
    ) -> None:
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.candidates (
                    candidate_id, entity_kind, created_at_utc, correlation_id
                ) VALUES (:candidate_id, :entity_kind, :created_at_utc, :correlation_id)
                ON CONFLICT (candidate_id) DO NOTHING
                """
            ),
            {
                "candidate_id": action.candidate_id,
                "entity_kind": entity_kind.value,
                "created_at_utc": now_utc,
                "correlation_id": correlation_id,
            },
        )
        # The row lock serialises revision allocation for one logical source entity, so two
        # runs acquiring the same entity cannot both allocate the same revision number.
        existing = connection.execute(
            sa.text(
                "SELECT entity_kind FROM candidates.candidates "
                "WHERE candidate_id = :candidate_id FOR UPDATE"
            ),
            {"candidate_id": action.candidate_id},
        ).one_or_none()
        if existing is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_IDENTITY_CONFLICT",
                message="The deterministic candidate identity could not be established.",
                context={"candidateId": str(action.candidate_id)},
                required_action="Investigate the conflicting candidate identity at its owner.",
            )
        if str(existing[0]) != entity_kind.value:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_IDENTITY_CONFLICT",
                message=("The deterministic candidate identity already exists with another kind."),
                context={
                    "candidateId": str(action.candidate_id),
                    "existingEntityKind": str(existing[0]),
                    "requestedEntityKind": entity_kind.value,
                },
                required_action="Investigate the conflicting candidate identity at its owner.",
            )

    @staticmethod
    def _source_record_revision(
        connection: Connection,
        *,
        action: CandidateIntakeAction,
    ) -> tuple[int, bool]:
        """The immutable revision this acquisition owns, and whether it is new.

        Identity is the acquisition itself: replaying one acquisition resolves to the exact
        revision it produced, so a repeated fetch never appends a revision, and a genuinely
        different acquisition becomes the next revision of the same logical candidate. The
        candidate row is already locked by the caller, which serialises concurrent runs.
        """

        provenance = action.source_record_provenance
        if provenance is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_PROVENANCE_MISSING",
                message="Source-record intake requires source-record provenance.",
                context={"candidateId": str(action.candidate_id)},
                required_action="Recompute the intake transition from the exact batch.",
            )
        existing = connection.execute(
            sa.text(
                "SELECT revision FROM candidates.source_entity_revisions "
                "WHERE candidate_id = :candidate_id AND acquisition_identity = :identity"
            ),
            {
                "candidate_id": action.candidate_id,
                "identity": provenance.acquisition_identity,
            },
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing), False
        highest = connection.execute(
            sa.text(
                "SELECT max(revision) FROM candidates.candidate_revisions "
                "WHERE candidate_id = :candidate_id"
            ),
            {"candidate_id": action.candidate_id},
        ).scalar_one_or_none()
        return 0 if highest is None else int(highest) + 1, True

    @staticmethod
    def _record_source_entity_revision(
        connection: Connection,
        *,
        action: CandidateIntakeAction,
        revision_number: int,
        now_utc: datetime,
        correlation_id: str,
    ) -> None:
        provenance = action.source_record_provenance
        if provenance is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_PROVENANCE_MISSING",
                message="Source-record intake requires source-record provenance.",
                context={"candidateId": str(action.candidate_id)},
                required_action="Recompute the intake transition from the exact batch.",
            )
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.source_entity_revisions (
                    candidate_id, revision, acquisition_identity, source_record_id,
                    source_record_artifact_id, dataset_release, run_id, acquired_at_utc,
                    correlation_id
                ) VALUES (
                    :candidate_id, :revision, :acquisition_identity, :source_record_id,
                    :source_record_artifact_id, :dataset_release, :run_id, :acquired_at_utc,
                    :correlation_id
                )
                """
            ),
            {
                "candidate_id": action.candidate_id,
                "revision": revision_number,
                "acquisition_identity": provenance.acquisition_identity,
                "source_record_id": provenance.source_record_id,
                "source_record_artifact_id": provenance.source_record_artifact.artifact_id,
                "dataset_release": provenance.dataset_release,
                "run_id": action.run_id,
                "acquired_at_utc": now_utc,
                "correlation_id": correlation_id,
            },
        )

    @staticmethod
    def _insert_revision(connection: Connection, *, revision: CandidateRevision) -> None:
        existing = connection.execute(
            sa.text(
                """
                SELECT snapshot_digest FROM candidates.candidate_revisions
                WHERE candidate_id = :candidate_id AND revision = :revision
                """
            ),
            {"candidate_id": revision.candidate_id, "revision": revision.revision},
        ).one_or_none()
        if existing is not None:
            if str(existing[0]) != revision.snapshot_digest:
                raise PipelineAdvancementConflict(
                    code="PIPELINE_CANDIDATE_REVISION_CONFLICT",
                    message="The stored candidate revision differs from the exact recomputation.",
                    context={
                        "candidateId": str(revision.candidate_id),
                        "revision": revision.revision,
                        "storedDigest": str(existing[0]),
                        "computedDigest": revision.snapshot_digest,
                    },
                    required_action="Investigate the diverging candidate revision at its owner.",
                )
            return
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.candidate_revisions (
                    candidate_id, revision, cluster_id, resolution_state, snapshot_digest,
                    source_lineage_digest, normalized_payload, recorded_at_utc, correlation_id
                ) VALUES (
                    :candidate_id, :revision, :cluster_id, :resolution_state, :snapshot_digest,
                    :source_lineage_digest, CAST(:normalized_payload AS jsonb),
                    :recorded_at_utc, :correlation_id
                )
                """
            ),
            {
                "candidate_id": revision.candidate_id,
                "revision": revision.revision,
                "cluster_id": revision.cluster_id,
                "resolution_state": revision.resolution_state,
                "snapshot_digest": revision.snapshot_digest,
                "source_lineage_digest": revision.source_lineage_digest,
                "normalized_payload": _payload_json(revision.normalized_payload),
                "recorded_at_utc": revision.recorded_at_utc,
                "correlation_id": revision.correlation_id,
            },
        )

    @staticmethod
    def _insert_evidence(connection: Connection, *, revision: CandidateRevision) -> None:
        for item in revision.evidence:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO candidates.candidate_revision_evidence (
                        candidate_id, candidate_revision, position, evidence_kind, evidence_digest
                    ) VALUES (
                        :candidate_id, :candidate_revision, :position, :evidence_kind,
                        :evidence_digest
                    )
                    ON CONFLICT (candidate_id, candidate_revision, position) DO NOTHING
                    """
                ),
                {
                    "candidate_id": revision.candidate_id,
                    "candidate_revision": revision.revision,
                    "position": item.position,
                    "evidence_kind": item.evidence_kind,
                    "evidence_digest": item.evidence_digest,
                },
            )

    @staticmethod
    def _insert_review_case(
        connection: Connection,
        *,
        revision: CandidateRevision,
        now_utc: datetime,
        correlation_id: str,
        extra_reason_codes: tuple[str, ...] = (),
    ) -> None:
        reason_codes = (_REVIEW_OPEN_REASON, *extra_reason_codes)
        case_id = review_case_id(revision.candidate_id, revision.revision, reason_codes)
        connection.execute(
            sa.text(
                """
                INSERT INTO review.review_cases (
                    case_id, candidate_id, candidate_revision, opened_at_utc, correlation_id
                ) VALUES (
                    :case_id, :candidate_id, :candidate_revision, :opened_at_utc, :correlation_id
                )
                ON CONFLICT (case_id) DO NOTHING
                """
            ),
            {
                "case_id": case_id,
                "candidate_id": revision.candidate_id,
                "candidate_revision": revision.revision,
                "opened_at_utc": now_utc,
                "correlation_id": correlation_id,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO review.review_case_revisions (
                    case_id, revision, state, reason_codes, current_decision_id,
                    recorded_at_utc, correlation_id
                ) VALUES (
                    :case_id, :revision, :state, :reason_codes, NULL,
                    :recorded_at_utc, :correlation_id
                )
                ON CONFLICT (case_id, revision) DO NOTHING
                """
            ).bindparams(sa.bindparam("reason_codes", type_=sa.ARRAY(sa.Text()))),
            {
                "case_id": case_id,
                "revision": 0,
                "state": _REVIEW_OPEN_STATE,
                "reason_codes": list(reason_codes),
                "recorded_at_utc": now_utc,
                "correlation_id": correlation_id,
            },
        )


def _payload_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def candidate_intake_transition_appliers(
    *,
    clock: Callable[[], datetime] | None = None,
    resolution_scheduler: PostgresResolutionBatchScheduler | None = None,
) -> Mapping[str, PostgresCandidateIntakeApplier]:
    """Build the observation-batch-to-candidate intake applier registration."""
    applier = PostgresCandidateIntakeApplier(
        clock=clock,
        resolution_scheduler=resolution_scheduler,
    )
    return dict.fromkeys(_CANDIDATE_INTAKE_TRANSITION_KEYS, applier)


def _source_record_evidence(item: SourceRecordObservation) -> EvidenceReference | None:
    """Carry field-level source evidence forward into the materializer shape.

    The source URL is the one the source record published; it is never derived from
    the record id, and no evidence is invented when the source recorded none.
    """
    evidence = item.evidence
    if evidence is None:
        return None
    return EvidenceReference(
        raw_artifact_digest=evidence.raw_artifact_digest,
        source_url=evidence.source_url,
        locator_kind=EvidenceLocatorKind(evidence.locator_kind.value),
        locator_value=evidence.locator_value,
        evidence_digest=evidence.evidence_digest,
        evidence_span=evidence.evidence_span,
        observed_at_utc=evidence.observed_at_utc,
        extractor_revision=evidence.mapping_revision,
    )


def _source_record_as_observations(
    batch: SourceRecordObservationBatch,
) -> ObservationBatch:
    """Express verified source-native observations in the materializer's internal shape.

    This is an adapter, not a contract change: identity, state, typed value and
    observation ids are carried unchanged. No HTML extracted-record digest and no
    manual-import lineage is manufactured.
    """
    payload = ObservationBatchPayload(
        source_record_id=batch.source_record_id,
        extracted_record_digest=batch.source_record_artifact_digest,
        raw_artifact_digest=batch.source_record_artifact_digest,
        source_policy_digest=batch.source_policy_digest,
        normalizer_revision=batch.mapping_revision,
        observations=tuple(
            seal_field_observation(
                FieldObservationPayload(
                    source_record_id=item.source_record_id,
                    field_key=item.field_key,
                    state=item.state,
                    value=item.value,
                    evidence=_source_record_evidence(item),
                    source_policy_digest=item.source_policy_digest,
                    normalizer_revision=item.mapping_revision,
                    confidence=item.confidence,
                )
            )
            for item in batch.observations
        ),
        issues=batch.issues,
    )
    # The generic batch has its own canonical content, so it must carry its own digest:
    # inheriting the source-record digest makes verify_observation_batch reject it.
    return seal_observation_batch(payload)


def _source_record_observation_artifact_ids(
    batch: SourceRecordObservationBatch,
) -> Mapping[str, UUID]:
    """The artifact identity each source-native observation already carries."""

    resolved: dict[str, UUID] = {}
    for item in batch.observations:
        evidence = item.evidence
        if evidence is None:
            continue
        recorded = resolved.setdefault(evidence.raw_artifact_digest, evidence.raw_artifact_id)
        if recorded != evidence.raw_artifact_id:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_ARTIFACT_IDENTITY_AMBIGUOUS",
                message=(
                    "Two source-native observations claim one content digest for different "
                    "artifacts."
                ),
                context={"observationCount": len(batch.observations)},
                required_action="Repair the source-record mapping evidence at its owner.",
            )
    return resolved


def _geo_point_value(document: Mapping[str, object]) -> str | None:
    """Render one observed coordinate as a canonical numeric pair, or fail closed."""

    missing = object()
    latitude = document.get("latitude", missing)
    longitude = document.get("longitude", missing)
    if latitude is missing and longitude is missing:
        return None
    if latitude is missing or longitude is missing:
        raise CandidateGeographyError(
            code="CANDIDATE_GEOGRAPHY_POINT_INCOMPLETE",
            context={"reason": "latitude_and_longitude_must_be_observed_together"},
        )
    return json.dumps(
        {"latitude": str(latitude), "longitude": str(longitude)},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _source_record_artifact_ids(
    action: CandidateIntakeAction,
    batch: SourceRecordObservationBatch,
) -> tuple[UUID, ...]:
    return tuple(sorted({*action.source_artifact_ids, batch.source_record_artifact_id}))


def _observation_artifact_ids(
    connection: Connection,
    *,
    observations: Sequence[FieldObservation],
) -> Mapping[str, UUID]:
    """Resolve the exact acquired document each observation was read from.

    A field observation seals the content digest of the document it was extracted from, and
    the artifact store guarantees one object per kind and digest. The identity is therefore
    looked up by digest alone; a digest with no artifact, or with more than one, is a defect
    rather than a value to guess.
    """

    digests = sorted(
        {
            item.evidence.raw_artifact_digest
            for item in observations
            if item.evidence is not None and item.evidence.raw_artifact_digest
        }
    )
    rows = connection.execute(
        sa.text(
            """
            SELECT object_id, content_digest
            FROM sources.artifact_objects
            WHERE artifact_kind = 'raw_artifact'
              AND content_digest = ANY(:digests)
            """
        ),
        {"digests": digests},
    ).all()
    grouped: dict[str, set[UUID]] = {}
    for object_id, digest in rows:
        grouped.setdefault(digest, set()).add(object_id)
    resolved: dict[str, UUID] = {}
    for digest, object_ids in grouped.items():
        if len(object_ids) != 1:
            raise PipelineAdvancementConflict(
                code="PIPELINE_CANDIDATE_ARTIFACT_IDENTITY_AMBIGUOUS",
                message=(
                    "More than one acquired artifact carries the same content digest, so an "
                    "observation cannot be bound to exactly one artifact."
                ),
                context={"matchingArtifactCount": len(object_ids)},
                required_action=(
                    "Repair the acquisition provenance so each observation binds one artifact."
                ),
            )
        resolved[digest] = next(iter(object_ids))
    return resolved


_SOURCE_RECORD_TARGET_FIELDS = {
    "display_name": CandidateField.NAMES,
    "name": CandidateField.NAMES,
    "website": CandidateField.WEBSITE_URLS,
    "phone": CandidateField.PHONES,
    "email": CandidateField.EMAILS,
    "address": CandidateField.ADDRESSES,
}
# The one declared projection whose target is geometry rather than a candidate field.
_SOURCE_RECORD_GEOMETRY_FIELD = "coordinates"


def _resolve_source_mapping_policy(
    source: SucceededWorkOutput,
) -> tuple[EntityKind, Mapping[str, CandidateField], bool]:
    """Resolve entity kind and field projection from the immutable mapping profile.

    Both decisions are campaign-owned. An unset entity kind is unresolved: intake
    refuses rather than silently defaulting to a kind the source never proved.
    """
    payload = source.normalization_profile_content
    if payload is None:
        raise PipelineAdvancementConflict(
            code="PIPELINE_SOURCE_RECORD_PROFILE_MISSING",
            message="Source-record intake requires the immutable mapping profile.",
            context={"sourceWorkUnitId": str(source.source_work_unit_id)},
            required_action="Restore the mapping profile input artifact before intake.",
        )
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineAdvancementConflict(
            code="PIPELINE_SOURCE_RECORD_PROFILE_INVALID",
            message="The mapping profile is not valid JSON.",
            context={"causeType": type(exc).__name__},
            required_action="Repair the mapping profile at its producing stage.",
        ) from exc
    # `fieldAliases` is additive: it widens which source tags feed a target but changes
    # no typed target, so a profile carrying it must resolve identically to one without.
    known_shapes = {
        _SOURCE_MAPPING_PROFILE_FIELDS,
        _SOURCE_MAPPING_PROFILE_FIELDS | {"fieldAliases"},
    }
    if not isinstance(document, dict) or set(document) not in known_shapes:
        raise PipelineAdvancementConflict(
            code="PIPELINE_SOURCE_RECORD_PROFILE_INVALID",
            message="The mapping profile has an unexpected shape.",
            context={"reason": "profile_shape"},
            required_action="Repair the mapping profile at its producing stage.",
        )
    raw_kind = document["entityKind"]
    if raw_kind is None:
        # Unresolved is a durable, operator-visible state, not a silent default:
        # the candidate is materialized as a review-only placeholder and the review
        # case carries the reason so an operator resolves the kind explicitly.
        return (
            _UNRESOLVED_ENTITY_KIND,
            _projection(document, raw_kind_optional=True),
            False,
        )
    try:
        entity_kind = _SOURCE_ENTITY_KINDS[str(raw_kind)]
    except KeyError as exc:
        raise PipelineAdvancementConflict(
            code="PIPELINE_SOURCE_RECORD_ENTITY_KIND_UNSUPPORTED",
            message="The mapping profile declares an unsupported entity kind.",
            context={"entityKind": str(raw_kind)},
            required_action="Correct the campaign binding entity kind.",
        ) from exc
    return entity_kind, _projection(document, raw_kind_optional=False), True


def _projection(
    document: Mapping[str, object], *, raw_kind_optional: bool
) -> dict[str, CandidateField]:
    raw_projection = document["fieldProjection"]
    if not isinstance(raw_projection, dict) or not raw_projection:
        raise PipelineAdvancementConflict(
            code="PIPELINE_SOURCE_RECORD_PROJECTION_INVALID",
            message="The mapping profile declares no explicit field projection.",
            context={"sourceBindingKey": str(document["sourceBindingKey"])},
            required_action="Declare source_field_projection in the campaign binding.",
        )
    projection: dict[str, CandidateField] = {}
    for field_key, target in raw_projection.items():
        # A source-native point is projected into the candidate's geography decision by the
        # geography owner, not onto a candidate field, so its declared projection is not a
        # claim about CandidateField. Every other declared target must be a real one.
        if (
            str(field_key) == _SOURCE_RECORD_GEOMETRY_FIELD
            and str(target) == _SOURCE_RECORD_GEOMETRY_FIELD
        ):
            continue
        try:
            projection[str(field_key)] = CandidateField(str(target))
        except ValueError as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_SOURCE_RECORD_PROJECTION_INVALID",
                message="The mapping profile targets an unknown candidate field.",
                context={"sourceField": str(field_key), "candidateField": str(target)},
                required_action="Correct the campaign binding field projection.",
            ) from exc
    return projection


def _candidate_field_projection(
    batch: ObservationBatch,
    profile: NormalizationProfile,
) -> Mapping[str, CandidateField]:
    """Derive the candidate projection strictly from the exact normalization profile."""
    targets = tuple(rule.target_field for rule in profile.field_rules) + tuple(
        rule.target_field for rule in profile.boolean_pattern_rules
    )
    projection: dict[str, CandidateField] = {}
    for target in targets:
        try:
            projection[target] = CandidateField(target)
        except ValueError as exc:
            raise ValueError(
                f"normalization profile target {target!r} has no candidate field"
            ) from exc
    observed = {item.field_key for item in batch.observations}
    unknown = observed - set(projection)
    if unknown:
        raise ValueError(
            "observation batch contains fields absent from the exact normalization profile: "
            + ", ".join(sorted(unknown))
        )
    return projection


_COORDINATE_VALUE_KEY = "coordinates"


def _geography_observations(observations: Sequence[object]) -> tuple[tuple[str, str, str], ...]:
    """Keep only the observations that can carry a point, with their exact identity.

    The classifier reads `(fieldKey, observationId, pointText)` and rejects any entry that
    is not a point object, so every other observed field is filtered out here instead of
    being passed as an empty string a classifier would have to reinterpret.
    """

    result: list[tuple[str, str, str]] = []
    for observation in observations:
        if getattr(observation, "field_key", None) != _COORDINATE_VALUE_KEY:
            continue
        observation_id = getattr(observation, "observation_id", None)
        value = _geography_value(observation)
        if observation_id and value:
            result.append((_COORDINATE_VALUE_KEY, str(observation_id), value))
    return tuple(result)


def _geography_value(observation: object) -> str:
    """Render one observation as the coordinate pair the classifier reads.

    Only an observed coordinate carries a point; every other field (and every
    non-observed state) contributes an empty string that the classifier ignores.
    """

    field_key = getattr(observation, "field_key", None)
    value = getattr(observation, "value", None)
    state = getattr(observation, "state", None)
    if field_key != _COORDINATE_VALUE_KEY or state is None or str(state) != "observed":
        return ""
    if value is None or getattr(value, "kind", None) != "geo_point":
        return ""
    return (
        _geo_point_value(
            {
                "latitude": getattr(value, "latitude", None),
                "longitude": getattr(value, "longitude", None),
            }
        )
        or ""
    )


def _decode_campaign_bundle(content: bytes) -> Mapping[str, object]:
    """Decode the exact run snapshot, keeping its canonical component ledger.

    The ledger itself is returned under `components` so the geography owner reads the
    same document shape it validates, while digests are verified here so a tampered
    component can never reach a candidate revision.
    """

    try:
        bundle = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("campaign bundle is not canonical JSON") from exc
    if not isinstance(bundle, dict) or set(bundle) != {"campaignKey", "components"}:
        raise ValueError("campaign bundle has an unsupported owner shape")
    components = bundle["components"]
    if not isinstance(components, list):
        raise ValueError("campaign bundle has no component ledger")
    for component in components:
        if not isinstance(component, dict) or set(component) != {"path", "digest", "content"}:
            raise ValueError("campaign component has an invalid shape")
        if not isinstance(component["path"], str):
            raise ValueError("campaign component path is invalid")
        if _json_digest(component["content"]) != component["digest"]:
            raise ValueError("campaign component differs from its declared digest")
    if not isinstance(bundle["campaignKey"], str):
        raise ValueError("campaign bundle key is invalid")
    return {"campaignKey": bundle["campaignKey"], "components": components}


def _json_digest(value: object) -> str:
    return _digest_bytes(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


class _CandidateGeography:
    """Binds a geography decision to the reference the candidate revision carries."""

    def __init__(self, *, decision: CandidateGeographyDecision) -> None:
        self._decision = decision

    def as_reference(self) -> GeographyReference:
        decision = self._decision
        point = decision.point
        if decision.coverage == "unknown":
            return GeographyReference(
                coverage=GeographyCoverage.UNKNOWN,
                market_area_revision=decision.boundary.market_area_revision,
                market_area_digest=decision.boundary.market_area_digest,
            )
        if point is None:
            raise CandidateGeographyError(
                code="CANDIDATE_GEOGRAPHY_POINT_MISSING",
                context={"coverage": decision.coverage},
            )
        return GeographyReference(
            coverage=_GEOGRAPHY_COVERAGE[decision.coverage],
            market_area_revision=decision.boundary.market_area_revision,
            market_area_digest=decision.boundary.market_area_digest,
            latitude=point.latitude,
            longitude=point.longitude,
            observation_ids=(uuid5(_GEOGRAPHY_OBSERVATION_NAMESPACE, point.observation_id),),
        )


_GEOGRAPHY_OBSERVATION_NAMESPACE = UUID("a4b02859-eecb-5653-8f31-3011deae05dd")
# The classifier reports literal coverage; the resolution contract names the same states.
_GEOGRAPHY_COVERAGE = {
    "inside": GeographyCoverage.INSIDE,
    "boundary": GeographyCoverage.BOUNDARY,
    "outside": GeographyCoverage.OUTSIDE,
}


@dataclass(frozen=True, slots=True)
class _ExportPolicy:
    """The exact source policy facts a sealed entry carries from the run snapshot.

    `usage_policy` is only set when the source's legal status actually grants that
    redistribution right. A status that grants nothing keeps it unset, so no downstream
    reader can mistake research-only evidence for a redistribution authorization.
    """

    source_key: str
    legal_status: str
    usage_policy: str | None
    attribution: str | None


# Collector legal status -> the redistribution policy an export may declare. Only
# `approved` authorizes a publishable catalog entry; `reference_only` stays link-only and
# every restricted status is absent, so intake fails instead of exporting it.
_USAGE_POLICY_BY_LEGAL_STATUS = {
    "approved": "publishable",
    "reference_only": "link_only",
}
# Statuses outside the map grant no redistribution right at all. They are recorded with
# `legalStatus` only, never converted into a weaker-but-different authorization such as
# `link_only`, so the original restriction survives intake unchanged.
_ATTRIBUTION_BY_SOURCE = {
    "openstreetmap": "© OpenStreetMap contributors",
}


def _source_policy_for_binding(
    bundle: Mapping[str, object],
    source_binding_key: str | None,
    *,
    _row_context_key: object,
) -> SourcePolicy:
    """Resolve the exact source policy of one bound source from the run snapshot."""

    del _row_context_key
    if source_binding_key is None:
        raise ValueError("source binding key is required to resolve a source policy")
    components = bundle.get("components")
    if not isinstance(components, list):
        raise ValueError("the run snapshot has no component ledger")
    for component in components:
        payload = component["content"]
        if component["path"] != "source_bindings.yaml" or not isinstance(payload, Mapping):
            continue
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("source bindings component has no item list")
        for item in items:
            if not isinstance(item, Mapping) or item.get("key") != source_binding_key:
                continue
            policy_key = item.get("source_policy_key")
            if not isinstance(policy_key, str):
                raise ValueError("source binding declares no policy key")
            policy_path = f"source_policies/{policy_key}.yaml"
            for candidate in components:
                content = candidate["content"]
                if candidate["path"] == policy_path and isinstance(content, Mapping):
                    return SourcePolicy.model_validate(content)
            raise ValueError(f"source policy component {policy_path!r} is missing")
        raise ValueError(f"source binding {source_binding_key!r} is not declared")
    raise ValueError("campaign bundle declares no source bindings")


def _revision_with_export_facts(
    revision: CandidateRevision,
    *,
    policy: _ExportPolicy,
    source_observations: Sequence[SourceRecordObservation] = (),
) -> CandidateRevision:
    """Re-seal the revision with the export-side facts an export is entitled to state.

    The usage policy and attribution are decided from the run's exact source policy, so
    they are sealed into the immutable revision instead of being re-derived later from
    mutable campaign state. A source-native candidate also keeps the observation ledger
    the export proves each field against: value, state and identity are the verified
    batch's own, and only the generic wrapper is replaced, because it cannot carry the
    source artifact each field was read from.

    The snapshot digest is recomputed over the new payload, so a revision that carries
    these facts is still self-verifying.
    """

    payload = dict(revision.normalized_payload)
    payload["exportPolicy"] = {
        "sourceKey": policy.source_key,
        "legalStatus": policy.legal_status,
        "usagePolicy": policy.usage_policy,
        "attribution": policy.attribution,
    }
    if source_observations:
        payload["fieldObservations"] = [
            observation.model_dump(mode="json", by_alias=True)
            for observation in sorted(source_observations, key=lambda item: item.observation_id)
        ]
    return CandidateRevision(
        candidate_id=revision.candidate_id,
        revision=revision.revision,
        entity_kind=revision.entity_kind,
        cluster_id=revision.cluster_id,
        resolution_state=revision.resolution_state,
        snapshot_digest=candidate_snapshot_digest(
            candidate_id=revision.candidate_id,
            revision=revision.revision,
            entity_kind=revision.entity_kind,
            cluster_id=revision.cluster_id,
            resolution_state=revision.resolution_state,
            normalized_payload=payload,
            evidence=revision.evidence,
            source_lineage_digest=revision.source_lineage_digest,
        ),
        source_lineage_digest=revision.source_lineage_digest,
        normalized_payload=payload,
        evidence=revision.evidence,
        recorded_at_utc=revision.recorded_at_utc,
        correlation_id=revision.correlation_id,
    )
