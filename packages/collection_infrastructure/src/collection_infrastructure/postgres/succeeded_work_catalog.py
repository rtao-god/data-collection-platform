from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import Select

from collection_application import WorkCapability, WorkStage
from collection_application.canonicalization import canonical_json_bytes, sha256_json
from collection_application.manual_import_plan_document import (
    verify_manual_import_record_source_provenance,
)
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineAdvancementConflict,
    SucceededWorkOutput,
)
from collection_application.pipeline_supervision import (
    PipelineDiscoveryFailure,
    SuccessfulWorkDiscoveryBatch,
)
from collection_contracts import (
    WIKIDATA_RESULT_CONTRACT,
    CampaignDocument,
    ManualImportRecordDocument,
    ManualRecordLineage,
    NormalizationProfile,
    NormalizationProfilesDocument,
    SourceBindingsDocument,
    SourcePolicy,
    canonical_observation_batch_json,
    decode_extracted_record,
    decode_manual_record_lineage,
    decode_observation_batch,
    decode_wikidata_entity_result,
    decode_wikidata_query_spec,
)
from collection_infrastructure.object_store import ArtifactObjectStoreError, S3ArtifactObjectStore
from collection_infrastructure.postgres.artifact_metadata import (
    artifact_objects,
    artifact_records,
    work_input_artifacts,
    work_output_artifacts,
)
from collection_infrastructure.postgres.metadata import (
    config_bundle_artifacts,
    config_bundle_components,
    config_bundles,
)
from collection_infrastructure.postgres.pipeline_advancement_metadata import (
    pipeline_advancement_discovery_cursor,
    pipeline_advancements,
)
from collection_infrastructure.postgres.work_metadata import collection_runs, stage_runs, work_units
from official_http_contracts import HttpAcquisitionManifest

_OSM_RESULT_CONTRACT = "osm-overpass-result@1"
_SOURCE_RECORD_BATCH_CONTRACT = "source-record-observation-batch@1"
_SOURCE_RECORD_BATCH_ROLE = "source_record_observation_batch"
_SOURCE_MAPPING_PROFILE_ROLE = "source_mapping_profile"
_OSM_RAW_RESPONSE_ROLE = "osm_raw_response"
_OSM_OBSERVATIONS_ROLE = "osm_observations"
_WIKIDATA_RESULT_CONTRACT = WIKIDATA_RESULT_CONTRACT
_ENTITY_RESOLUTION_SNAPSHOT_CONTRACT = "entity-resolution-snapshot@1"
_WIKIDATA_RESULT_ROLE = "wikidata_entity_result"
_WIKIDATA_RAW_ROLE = "wikidata_raw_response"
_WIKIDATA_QUERY_ROLE = "wikidata_entity_query_spec"
_WIKIDATA_MAX_RAW_RESPONSE_BYTES = 64 * 1024 * 1024
_WIKIDATA_MAX_RESULT_BYTES = 64 * 1024 * 1024
_WIKIDATA_MAX_QUERY_SPEC_BYTES = 256 * 1024
_OSM_RAW_RESPONSE_CONTENT_TYPE = "application/json"
_OSM_OBSERVATIONS_CONTENT_TYPE = "application/vnd.collection.osm-observations+json"
_OSM_MAX_RAW_RESPONSE_BYTES = 128 * 1024 * 1024
_OSM_MAX_OBSERVATIONS_BYTES = 128 * 1024 * 1024
_SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class OsmObservationBatchEnvelope:
    """Bounded, contract-checked view of the `osm_observations` artifact.

    The connector that produces these bytes owns the full contract. Collection
    infrastructure must not import it, so the envelope pins the exact document shape
    it accepts and refuses anything else rather than trusting the producer.
    """

    query_digest: str
    response_digest: str
    osm_ids: tuple[tuple[str, int], ...]
    osm_base_timestamp_utc: str | None
    attribution: str

    @property
    def element_count(self) -> int:
        return len(self.osm_ids)


@dataclass(frozen=True, slots=True)
class _BoundArtifact:
    position: int
    artifact: ArtifactIdentity


class PostgresSucceededWorkCatalog:
    """Read canonical succeeded-work output and discover unregistered work."""

    def __init__(
        self,
        engine: Engine,
        *,
        object_store: S3ArtifactObjectStore | None = None,
    ) -> None:
        self._engine = engine
        self._object_store = object_store

    def __call__(
        self,
        connection: Connection,
        source_work_unit_id: UUID,
    ) -> SucceededWorkOutput:
        return self.read(connection, source_work_unit_id)

    def read(
        self,
        connection: Connection,
        source_work_unit_id: UUID,
    ) -> SucceededWorkOutput:
        work = connection.execute(_work_statement(source_work_unit_id)).mappings().one_or_none()
        if work is None:
            raise _catalog_conflict(
                "PIPELINE_SOURCE_WORK_NOT_FOUND",
                "The source work unit does not exist.",
                source_work_unit_id,
                "Refresh canonical work state and select an existing work unit.",
            )
        if _required_text(work, "state") != "succeeded":
            raise _catalog_conflict(
                "PIPELINE_SOURCE_WORK_NOT_SUCCEEDED",
                "Pipeline advancement accepts only successfully completed work.",
                source_work_unit_id,
                "Wait for canonical successful completion before registering advancement.",
            )

        run_id = _required_uuid(work, "run_id")
        stage_run_id = _required_uuid(work, "stage_run_id")
        stage = (
            connection.execute(
                sa.select(stage_runs).where(stage_runs.c.stage_run_id == stage_run_id)
            )
            .mappings()
            .one_or_none()
        )
        if stage is None:
            raise _catalog_conflict(
                "PIPELINE_STAGE_OWNER_NOT_FOUND",
                "The successful work has no canonical stage owner.",
                source_work_unit_id,
                "Repair stage ownership before advancing the successful output.",
            )
        if _required_uuid(stage, "run_id") != run_id:
            raise _catalog_conflict(
                "PIPELINE_STAGE_RUN_CONFLICT",
                "The work and stage owners belong to different collection runs.",
                source_work_unit_id,
                "Repair the corrupted run/stage ownership before advancement.",
            )

        inputs = _load_artifacts(connection, work_input_artifacts, source_work_unit_id)
        outputs = _load_artifacts(connection, work_output_artifacts, source_work_unit_id)
        lineage_bindings = tuple(
            item.artifact for item in inputs if item.artifact.role == "manual_record_lineage"
        )
        if len(lineage_bindings) > 1:
            raise _catalog_conflict(
                "PIPELINE_MANUAL_RECORD_LINEAGE_BINDINGS_INVALID",
                "Work has duplicate manual-record lineage input artifacts.",
                source_work_unit_id,
                "Repair the exact lineage input binding at the work producer.",
            )
        manual_record_lineage_content = (
            self._read_artifact_content(
                connection,
                lineage_bindings[0],
                source_work_unit_id,
                maximum_bytes=64 * 1024,
            )
            if lineage_bindings
            else None
        )
        manual_record_lineage: ManualRecordLineage | None = None
        if manual_record_lineage_content is not None:
            manual_record_lineage = self._verify_manual_record_lineage(
                connection,
                lineage_content=manual_record_lineage_content,
                lineage_artifact=lineage_bindings[0],
                run_id=run_id,
                source_work_unit_id=source_work_unit_id,
            )
        capability = _required_text(work, "capability")
        output_contract = _required_text(work, "output_contract")
        http_request_content = None
        normalization_profile_content = None
        is_normalization = (
            capability == "normalization" and output_contract == "field-observation-batch@1"
        )
        website_route_context_content = None
        extraction_request_content = None
        needs_page_snapshot = False
        needs_extraction_snapshot = False
        is_official_http = (
            capability == "http_fetch" and output_contract == "official-http-acquisition@1"
        )
        is_extracted_record = capability == "extraction" and output_contract == "extracted-record@1"
        is_osm_result = (
            capability == WorkCapability.OSM_QUERY.value and output_contract == _OSM_RESULT_CONTRACT
        )
        is_source_record_mapping = (
            capability == WorkCapability.SOURCE_RECORD_MAPPING.value
            and output_contract == _SOURCE_RECORD_BATCH_CONTRACT
        )
        is_wikidata_result = (
            capability == WorkCapability.WIKIDATA_ENTITY_QUERY.value
            and output_contract == _WIKIDATA_RESULT_CONTRACT
        )
        is_entity_resolution = (
            capability == WorkCapability.ENTITY_RESOLUTION.value
            and output_contract == _ENTITY_RESOLUTION_SNAPSHOT_CONTRACT
        )
        resolution_batch_content: bytes | None = None
        wikidata_query_spec_content: bytes | None = None
        if is_official_http:
            by_role = {item.artifact.role: item.artifact for item in outputs}
            if (
                len(outputs) not in {1, 2}
                or len(by_role) != len(outputs)
                or "http_acquisition_manifest" not in by_role
                or set(by_role) - {"http_acquisition_manifest", "http_raw_response"}
            ):
                raise _catalog_conflict(
                    "PIPELINE_HTTP_OUTPUT_BINDINGS_INVALID",
                    "Official HTTP success must bind one manifest and at most one raw response.",
                    source_work_unit_id,
                    "Repair successful HTTP completion with its canonical manifest/raw roles.",
                )
            output = by_role["http_acquisition_manifest"]
            raw_output = by_role.get("http_raw_response")
            manifest_content = self._read_artifact_content(
                connection,
                output,
                source_work_unit_id,
                maximum_bytes=2 * 1024 * 1024,
            )
            manifest = _verify_official_http_bindings(
                manifest_content,
                output_digest=_required_text(work, "output_digest"),
                manifest_digest=output.content_digest,
                raw_output=raw_output,
                input_artifacts=inputs,
                source_work_unit_id=source_work_unit_id,
            )
            output_content = manifest_content
            context_bindings = tuple(
                item.artifact for item in inputs if item.artifact.role == "website_route_context"
            )
            needs_page_snapshot = manifest.request_kind == "page" and len(context_bindings) == 1
            request_bindings = tuple(
                item.artifact for item in inputs if item.artifact.role == "http_request"
            )
            if len(request_bindings) != 1 or (context_bindings and len(context_bindings) != 1):
                raise _catalog_conflict(
                    "PIPELINE_WEBSITE_ROUTE_BINDINGS_INVALID",
                    "HTTP work must bind one request and, when routed, one route context.",
                    source_work_unit_id,
                    "Repair the exact HTTP request and optional route-context artifact roles.",
                )
            http_request_content = self._read_artifact_content(
                connection,
                request_bindings[0],
                source_work_unit_id,
                maximum_bytes=1024 * 1024,
            )
            if context_bindings:
                website_route_context_content = self._read_artifact_content(
                    connection,
                    context_bindings[0],
                    source_work_unit_id,
                    maximum_bytes=256 * 1024,
                )
            related_output_artifacts = (raw_output,) if raw_output is not None else ()
        elif is_extracted_record:
            by_role = {item.artifact.role: item.artifact for item in outputs}
            raw_inputs = tuple(
                item.artifact for item in inputs if item.artifact.role == "raw_source_document"
            )
            request_inputs = tuple(
                item.artifact for item in inputs if item.artifact.role == "extraction_request"
            )
            lineage_inputs = tuple(
                item.artifact for item in inputs if item.artifact.role == "manual_record_lineage"
            )
            if (
                len(outputs) != 1
                or len(by_role) != 1
                or "extracted_record" not in by_role
                or len(inputs) != 3
                or len(raw_inputs) != 1
                or len(request_inputs) != 1
                or len(lineage_inputs) != 1
                or {item.artifact.role for item in inputs}
                != {"raw_source_document", "extraction_request", "manual_record_lineage"}
            ):
                raise _catalog_conflict(
                    "PIPELINE_EXTRACTION_OUTPUT_BINDINGS_INVALID",
                    "Extraction success must bind one extracted record, one raw source, "
                    "one request, and one manual-record lineage artifact.",
                    source_work_unit_id,
                    "Repair the exact extraction output and its immutable request, raw, "
                    "and lineage roles.",
                )
            output = by_role["extracted_record"]
            if output.content_type != "application/vnd.collection.extracted-record+json":
                raise _catalog_conflict(
                    "PIPELINE_EXTRACTION_OUTPUT_CONTRACT_INVALID",
                    "The extracted-record artifact content type is unsupported.",
                    source_work_unit_id,
                    "Republish the extracted record using its declared JSON content type.",
                )
            output_content = self._read_artifact_content(
                connection,
                output,
                source_work_unit_id,
                maximum_bytes=64 * 1024 * 1024,
            )
            if manual_record_lineage is None:
                raise _catalog_conflict(
                    "PIPELINE_EXTRACTION_LINEAGE_MISSING",
                    "The extraction output has no verified manual-record lineage.",
                    source_work_unit_id,
                    "Restore exact verified manual-record lineage before advancement.",
                )
            try:
                extracted_record = decode_extracted_record(output_content)
            except ValueError as exc:
                raise _catalog_conflict(
                    "PIPELINE_EXTRACTION_OUTPUT_INVALID",
                    "The extracted record is invalid and cannot resolve manual lineage.",
                    source_work_unit_id,
                    "Repair the immutable extraction output at its producer.",
                ) from exc
            if (
                extracted_record.source_record_id
                != manual_record_lineage.manual_record_content_digest
            ):
                raise _manual_lineage_conflict(
                    source_work_unit_id,
                    manual_record_lineage.manual_record_work_id,
                    reason="extracted_record_source_record_mismatch",
                )
            extraction_request_content = self._read_artifact_content(
                connection,
                request_inputs[0],
                source_work_unit_id,
                maximum_bytes=1024 * 1024,
            )
            needs_extraction_snapshot = True
            related_output_artifacts = ()
        elif is_normalization:
            if manual_record_lineage is None:
                raise _catalog_conflict(
                    "PIPELINE_NORMALIZATION_LINEAGE_MISSING",
                    "The normalization batch has no verified manual-record lineage.",
                    source_work_unit_id,
                    "Restore the exact verified manual-record lineage before advancement.",
                )
            output, output_content, normalization_profile_content = self._read_normalization_output(
                connection,
                source_work_unit_id,
                outputs=outputs,
                inputs=inputs,
                manual_record_lineage=manual_record_lineage,
                output_digest=_required_text(work, "output_digest"),
            )
            needs_extraction_snapshot = True
            related_output_artifacts = ()
        elif is_source_record_mapping:
            output, output_content, normalization_profile_content = (
                self._read_source_record_mapping_output(
                    connection,
                    source_work_unit_id,
                    outputs=outputs,
                    inputs=inputs,
                    output_digest=_required_text(work, "output_digest"),
                )
            )
            related_output_artifacts = ()
        elif is_osm_result:
            raw_artifact, observations_artifact, observations_content = self._read_osm_result(
                connection,
                source_work_unit_id,
                outputs=outputs,
                inputs=inputs,
                output_digest=_required_text(work, "output_digest"),
            )
            output = observations_artifact
            output_content = observations_content
            related_output_artifacts = (raw_artifact,)
        elif is_entity_resolution:
            output, output_content, resolution_batch_content = self._read_resolution_output(
                connection,
                source_work_unit_id,
                outputs=outputs,
                inputs=inputs,
                output_digest=_required_text(work, "output_digest"),
            )
            related_output_artifacts = ()
        elif is_wikidata_result:
            raw_artifact, envelope_artifact, envelope_content, wikidata_query_spec_content = (
                self._read_wikidata_result(
                    connection,
                    source_work_unit_id,
                    outputs=outputs,
                    inputs=inputs,
                    output_digest=_required_text(work, "output_digest"),
                )
            )
            output = envelope_artifact
            output_content = envelope_content
            related_output_artifacts = (raw_artifact,)
        else:
            if len(outputs) != 1:
                raise PipelineAdvancementConflict(
                    code="PIPELINE_OUTPUT_ARTIFACT_CARDINALITY",
                    message="Successful work must bind exactly one canonical output artifact.",
                    context={
                        "sourceWorkUnitId": str(source_work_unit_id),
                        "outputArtifactCount": len(outputs),
                    },
                    required_action=(
                        "Repair work completion so the exact output contract has one "
                        "canonical artifact."
                    ),
                )
            output = outputs[0].artifact
            output_content = None
            related_output_artifacts = ()

        input_ids = {item.artifact.artifact_id for item in inputs}
        if any(item.artifact_id in input_ids for item in (output, *related_output_artifacts)):
            raise _catalog_conflict(
                "PIPELINE_ARTIFACT_DIRECTION_CONFLICT",
                "One artifact is bound as both pipeline input and output.",
                source_work_unit_id,
                "Repair immutable artifact bindings before advancement.",
            )
        output_digest = _required_text(work, "output_digest")
        run = (
            connection.execute(sa.select(collection_runs).where(collection_runs.c.run_id == run_id))
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise _catalog_conflict(
                "PIPELINE_RUN_OWNER_NOT_FOUND",
                "The successful work has no canonical collection run owner.",
                source_work_unit_id,
                "Repair the run ownership before advancing the successful output.",
            )
        config_bundle_digest = _required_text(run, "config_bundle_digest")
        campaign_key = _required_text(run, "campaign_key")
        campaign_bundle_content = None
        is_manual_record = (
            capability == "manual_record" and output_contract == "manual-import-record@1"
        )
        if (
            is_manual_record
            or needs_page_snapshot
            or needs_extraction_snapshot
            or is_wikidata_result
            or is_source_record_mapping
            or is_entity_resolution
        ):
            campaign_bundle_content = self._read_campaign_bundle(
                connection,
                config_bundle_digest,
                source_work_unit_id,
                expected_campaign_key=campaign_key,
            )
        if is_manual_record:
            output_content = self._read_artifact_content(
                connection,
                output,
                source_work_unit_id,
                maximum_bytes=4 * 1024 * 1024,
            )
        source_binding_key = (
            str(work["source_binding_key"]) if work["source_binding_key"] is not None else None
        )
        if is_normalization:
            if campaign_bundle_content is None or normalization_profile_content is None:
                raise _catalog_conflict(
                    "PIPELINE_NORMALIZATION_SNAPSHOT_MISSING",
                    "Normalization profile provenance requires the exact run snapshot.",
                    source_work_unit_id,
                    "Restore the run-bound snapshot and profile artifact before candidate intake.",
                )
            self._verify_normalization_profile_snapshot(
                source_work_unit_id,
                snapshot_content=campaign_bundle_content,
                source_binding_key=source_binding_key,
                profile_content=normalization_profile_content,
                batch_content=output_content,
            )
        return SucceededWorkOutput(
            source_work_unit_id=source_work_unit_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage=WorkStage(_required_text(stage, "stage")),
            capability=_required_text(work, "capability"),
            output_contract=_required_text(work, "output_contract"),
            output_digest=output_digest,
            output_artifact=output,
            source_binding_key=source_binding_key,
            config_bundle_digest=config_bundle_digest,
            campaign_bundle_content=campaign_bundle_content,
            output_content=output_content,
            input_artifacts=tuple(item.artifact for item in inputs),
            related_output_artifacts=related_output_artifacts,
            http_request_content=http_request_content,
            website_route_context_content=website_route_context_content,
            extraction_request_content=extraction_request_content,
            normalization_profile_content=normalization_profile_content,
            wikidata_query_spec_content=wikidata_query_spec_content,
            resolution_batch_content=resolution_batch_content,
            manual_record_lineage_content=manual_record_lineage_content,
        )

    def _read_resolution_output(
        self,
        connection: Connection,
        source_work_unit_id: UUID,
        *,
        outputs: tuple[_BoundArtifact, ...],
        inputs: tuple[_BoundArtifact, ...],
        output_digest: str,
    ) -> tuple[ArtifactIdentity, bytes, bytes]:
        """Read the exact snapshot and the exact batch the resolution unit consumed."""
        out_bindings = {item.artifact.role: item.artifact for item in outputs}
        in_bindings = {item.artifact.role: item.artifact for item in inputs}
        if len(outputs) != 1 or set(out_bindings) != {"resolution_snapshot"}:
            raise _catalog_conflict(
                "PIPELINE_RESOLUTION_OUTPUT_ROLE_CONFLICT",
                "Entity resolution must publish exactly one snapshot artifact.",
                source_work_unit_id,
                "Repair resolution publication so the snapshot is the only output role.",
            )
        if len(inputs) != 1 or set(in_bindings) != {"resolution_batch"}:
            raise _catalog_conflict(
                "PIPELINE_RESOLUTION_INPUT_ROLE_CONFLICT",
                "Entity resolution must consume exactly one batch artifact.",
                source_work_unit_id,
                "Repair resolution input bindings before advancement.",
            )
        output = out_bindings["resolution_snapshot"]
        if output.content_digest != output_digest:
            raise _catalog_conflict(
                "PIPELINE_RESOLUTION_SNAPSHOT_DIGEST_CONFLICT",
                "The snapshot artifact digest differs from the succeeded output digest.",
                source_work_unit_id,
                "Repair the immutable resolution snapshot at its producer.",
            )
        batch_content = self._read_artifact_content(
            connection,
            in_bindings["resolution_batch"],
            source_work_unit_id,
            maximum_bytes=64 * 1024 * 1024,
        )
        snapshot_content = self._read_artifact_content(
            connection,
            output,
            source_work_unit_id,
            maximum_bytes=64 * 1024 * 1024,
        )
        return output, snapshot_content, batch_content

    @staticmethod
    def _verify_normalization_profile_snapshot(
        source_work_unit_id: UUID,
        *,
        snapshot_content: bytes,
        source_binding_key: str | None,
        profile_content: bytes,
        batch_content: bytes | None,
    ) -> None:
        try:
            if source_binding_key is None or batch_content is None:
                raise ValueError("normalization binding or output is missing")
            snapshot = json.loads(snapshot_content)
            components = {item["path"]: item["content"] for item in snapshot["components"]}
            campaign = CampaignDocument.model_validate(components["campaign.yaml"])
            bindings = SourceBindingsDocument.model_validate(components["source_bindings.yaml"])
            matching_bindings = tuple(
                item for item in bindings.items if item.key == source_binding_key
            )
            if len(matching_bindings) != 1:
                raise ValueError("source binding is absent or ambiguous in the run snapshot")
            binding = matching_bindings[0]
            if binding.normalization_profile is None:
                raise ValueError("source binding has no normalization profile")
            profiles = NormalizationProfilesDocument.model_validate(
                components["normalization_profiles.yaml"]
            )
            matching_profiles = tuple(
                item for item in profiles.items if item.key == binding.normalization_profile
            )
            if len(matching_profiles) != 1:
                raise ValueError("binding profile is absent or ambiguous in the run snapshot")
            profile = matching_profiles[0].profile
            expected_profile_bytes = json.dumps(
                profile.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            batch = decode_observation_batch(batch_content)
            policy_document = components[f"source_policies/{binding.source_policy_key}.yaml"]
            policy = SourcePolicy.model_validate(policy_document)
            source_fields = {rule.source_field for rule in profile.field_rules}
            target_fields = {
                *(rule.target_field for rule in profile.field_rules),
                *(rule.target_field for rule in profile.boolean_pattern_rules),
            }
            if (
                expected_profile_bytes != profile_content
                or batch.normalizer_revision != profile.normalizer_revision
                or batch.source_policy_digest != sha256_json(policy.model_dump(mode="json"))
                or policy.source_key != binding.source_key
                or policy.policy_key != binding.source_policy_key
                or campaign.campaign_key != snapshot["campaignKey"]
                or binding.key not in campaign.enabled_source_bindings
                or not source_fields.issubset(policy.allowed_fields)
                or not target_fields.issubset(policy.allowed_fields)
                or target_fields.intersection(policy.prohibited_fields)
                or target_fields.intersection(profile.prohibited_fields)
            ):
                raise ValueError("normalization profile differs from exact run snapshot policy")
        except (KeyError, TypeError, ValueError) as exc:
            raise _catalog_conflict(
                "PIPELINE_NORMALIZATION_PROFILE_SNAPSHOT_MISMATCH",
                "The normalization profile artifact is not the exact profile authorized by "
                "the run snapshot.",
                source_work_unit_id,
                "Re-run normalization using the exact immutable snapshot profile and "
                "source policy.",
            ) from exc

    def _verify_manual_record_lineage(
        self,
        connection: Connection,
        *,
        lineage_content: bytes,
        lineage_artifact: ArtifactIdentity,
        run_id: UUID,
        source_work_unit_id: UUID,
    ) -> ManualRecordLineage:
        try:
            lineage = decode_manual_record_lineage(lineage_content)
        except (TypeError, ValueError) as exc:
            raise _manual_lineage_conflict(
                source_work_unit_id,
                None,
                reason="lineage_contract_invalid",
            ) from exc

        if (
            lineage.run_id != run_id
            or lineage_artifact.content_type
            != "application/vnd.collection.manual-record-lineage+json"
            or lineage_artifact.size_bytes != len(lineage_content)
            or _content_digest(lineage_content) != lineage_artifact.content_digest
        ):
            raise _manual_lineage_conflict(
                source_work_unit_id,
                lineage.manual_record_work_id,
                reason="lineage_identity_mismatch",
            )

        manual_work = (
            connection.execute(
                sa.select(work_units).where(work_units.c.work_id == lineage.manual_record_work_id)
            )
            .mappings()
            .one_or_none()
        )
        if (
            manual_work is None
            or _required_text(manual_work, "state") != "succeeded"
            or _required_uuid(manual_work, "run_id") != run_id
            or _required_text(manual_work, "stage") != WorkStage.DISCOVERY.value
            or _required_text(manual_work, "capability") != "manual_record"
            or _required_text(manual_work, "output_contract") != "manual-import-record@1"
        ):
            raise _manual_lineage_conflict(
                source_work_unit_id,
                lineage.manual_record_work_id,
                reason="manual_record_work_owner_invalid",
            )

        stage_row = (
            connection.execute(
                sa.select(stage_runs).where(
                    stage_runs.c.stage_run_id == _required_uuid(manual_work, "stage_run_id")
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            stage_row is None
            or _required_uuid(stage_row, "run_id") != run_id
            or _required_text(stage_row, "stage") != WorkStage.DISCOVERY.value
        ):
            raise _manual_lineage_conflict(
                source_work_unit_id,
                lineage.manual_record_work_id,
                reason="manual_record_stage_owner_invalid",
            )

        record_outputs = _load_artifacts(
            connection,
            work_output_artifacts,
            lineage.manual_record_work_id,
        )
        record_inputs = _load_artifacts(
            connection,
            work_input_artifacts,
            lineage.manual_record_work_id,
        )
        if (
            len(record_outputs) != 1
            or record_outputs[0].artifact.role != "manual_import_record"
            or record_outputs[0].artifact.artifact_id != lineage.manual_record_artifact_id
            or record_outputs[0].artifact.content_digest != lineage.manual_record_artifact_digest
            or record_outputs[0].artifact.content_type
            != "application/vnd.collection.manual-import-record+json"
        ):
            raise _manual_lineage_conflict(
                source_work_unit_id,
                lineage.manual_record_work_id,
                reason="manual_record_output_binding_mismatch",
                artifactId=str(lineage.manual_record_artifact_id),
            )

        output_owner_ids = tuple(
            connection.execute(
                sa.select(artifact_records.c.work_id).where(
                    artifact_records.c.artifact_id == lineage.manual_record_artifact_id
                )
            )
            .scalars()
            .all()
        )
        if output_owner_ids != (lineage.manual_record_work_id,):
            raise _manual_lineage_conflict(
                source_work_unit_id,
                lineage.manual_record_work_id,
                reason="manual_record_artifact_owner_mismatch",
                artifactId=str(lineage.manual_record_artifact_id),
            )

        try:
            record_content = self._read_artifact_content(
                connection,
                record_outputs[0].artifact,
                source_work_unit_id,
                maximum_bytes=4 * 1024 * 1024,
            )
            record = ManualImportRecordDocument.model_validate_json(record_content, strict=True)
            record_payload = record.model_dump(mode="json", by_alias=True)
            record_payload_without_digest = dict(record_payload)
            del record_payload_without_digest["contentDigest"]
            expected_record_digest = sha256_json(
                {
                    "sourceDigest": record.source_digest,
                    "locator": record.locator.model_dump(mode="json", by_alias=True),
                    "record": record.record.model_dump(mode="json", by_alias=True),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _manual_lineage_conflict(
                source_work_unit_id,
                lineage.manual_record_work_id,
                reason="manual_record_artifact_invalid",
                artifactId=str(lineage.manual_record_artifact_id),
            ) from exc

        if (
            canonical_json_bytes(record_payload) + b"\n" != record_content
            or record.content_digest != sha256_json(record_payload_without_digest)
            or record.record_digest != expected_record_digest
            or record.source_digest != lineage.source_artifact_digest
            or record.record_digest != lineage.record_digest
            or record.content_digest != lineage.manual_record_content_digest
            or record.record.expected_entity_kind != lineage.expected_entity_kind
            or _required_text(manual_work, "output_digest") != record.content_digest
            or len(record_inputs) != 2
        ):
            raise _manual_lineage_conflict(
                source_work_unit_id,
                lineage.manual_record_work_id,
                reason="manual_record_document_lineage_mismatch",
                artifactId=str(lineage.manual_record_artifact_id),
            )

        source_inputs = tuple(
            item.artifact
            for item in record_inputs
            if item.artifact.artifact_id == lineage.source_artifact_id
        )
        expected_roles = {
            record.source_artifact_role,
            f"manual_import_plan_record:{record.plan_record_position}",
        }
        if (
            len(source_inputs) != 1
            or source_inputs[0].role != record.source_artifact_role
            or source_inputs[0].content_digest != lineage.source_artifact_digest
            or {item.artifact.role for item in record_inputs} != expected_roles
        ):
            raise _manual_lineage_conflict(
                source_work_unit_id,
                lineage.manual_record_work_id,
                reason="source_artifact_input_binding_mismatch",
                artifactId=str(lineage.source_artifact_id),
            )
        source_content = self._read_artifact_content(
            connection,
            source_inputs[0],
            source_work_unit_id,
            maximum_bytes=16 * 1024 * 1024,
        )
        if _content_digest(source_content) != lineage.source_artifact_digest:
            raise _manual_lineage_conflict(
                source_work_unit_id,
                lineage.manual_record_work_id,
                reason="source_artifact_bytes_mismatch",
                artifactId=str(lineage.source_artifact_id),
            )

        self._verify_manual_import_plan_provenance(
            connection,
            record=record,
            record_inputs=record_inputs,
            source_artifact=source_inputs[0],
            source_content=source_content,
            run_id=run_id,
            source_work_unit_id=source_work_unit_id,
        )
        return lineage

    def _verify_manual_import_plan_provenance(
        self,
        connection: Connection,
        *,
        record: ManualImportRecordDocument,
        record_inputs: tuple[_BoundArtifact, ...],
        source_artifact: ArtifactIdentity,
        source_content: bytes,
        run_id: UUID,
        source_work_unit_id: UUID,
    ) -> None:
        plan_role = f"manual_import_plan_record:{record.plan_record_position}"
        plan_inputs = tuple(
            item.artifact for item in record_inputs if item.artifact.role == plan_role
        )
        if len(plan_inputs) != 1:
            raise _manual_lineage_conflict(
                source_work_unit_id,
                None,
                reason="manual_import_plan_input_binding_invalid",
            )
        plan_artifact = plan_inputs[0]
        if plan_artifact.content_type != "application/vnd.collection.manual-import-plan+json":
            raise _manual_lineage_conflict(
                source_work_unit_id,
                None,
                reason="manual_import_plan_content_type_invalid",
                artifactId=str(plan_artifact.artifact_id),
            )
        plan_owner_ids = tuple(
            connection.execute(
                sa.select(artifact_records.c.work_id).where(
                    artifact_records.c.artifact_id == plan_artifact.artifact_id
                )
            )
            .scalars()
            .all()
        )
        if len(plan_owner_ids) != 1 or plan_owner_ids[0] is None:
            raise _manual_lineage_conflict(
                source_work_unit_id,
                None,
                reason="manual_import_plan_artifact_owner_missing",
                artifactId=str(plan_artifact.artifact_id),
            )
        plan_work_id = _uuid_value(plan_owner_ids[0])
        plan_work = (
            connection.execute(sa.select(work_units).where(work_units.c.work_id == plan_work_id))
            .mappings()
            .one_or_none()
        )
        if (
            plan_work is None
            or _required_text(plan_work, "state") != "succeeded"
            or _required_uuid(plan_work, "run_id") != run_id
            or _required_text(plan_work, "stage") != WorkStage.DISCOVERY.value
            or _required_text(plan_work, "capability") != "manual_import"
            or _required_text(plan_work, "output_contract") != "manual-import-plan@1"
        ):
            raise _manual_lineage_conflict(
                source_work_unit_id,
                plan_work_id,
                reason="manual_import_plan_work_owner_invalid",
            )
        plan_stage = (
            connection.execute(
                sa.select(stage_runs).where(
                    stage_runs.c.stage_run_id == _required_uuid(plan_work, "stage_run_id")
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            plan_stage is None
            or _required_uuid(plan_stage, "run_id") != run_id
            or _required_text(plan_stage, "stage") != WorkStage.DISCOVERY.value
        ):
            raise _manual_lineage_conflict(
                source_work_unit_id,
                plan_work_id,
                reason="manual_import_plan_stage_owner_invalid",
            )
        plan_outputs = _load_artifacts(connection, work_output_artifacts, plan_work_id)
        plan_work_inputs = _load_artifacts(connection, work_input_artifacts, plan_work_id)
        plan_sources = tuple(
            item.artifact
            for item in plan_work_inputs
            if item.artifact.artifact_id == source_artifact.artifact_id
        )
        if (
            len(plan_outputs) != 1
            or plan_outputs[0].artifact.content_type
            != "application/vnd.collection.manual-import-plan+json"
            or plan_outputs[0].artifact.artifact_id != plan_artifact.artifact_id
            or plan_outputs[0].artifact.role != "manual_import_plan"
            or len(plan_work_inputs) != 1
            or len(plan_sources) != 1
            or plan_sources[0].role != record.source_artifact_role
            or plan_sources[0].content_digest != source_artifact.content_digest
        ):
            raise _manual_lineage_conflict(
                source_work_unit_id,
                plan_work_id,
                reason="manual_import_plan_source_or_output_binding_mismatch",
                artifactId=str(plan_artifact.artifact_id),
            )
        plan_content = self._read_artifact_content(
            connection,
            plan_artifact,
            source_work_unit_id,
            maximum_bytes=64 * 1024 * 1024,
        )
        try:
            verify_manual_import_record_source_provenance(
                source_content=source_content,
                plan_content=plan_content,
                plan_artifact_digest=plan_artifact.content_digest,
                source_artifact_role=record.source_artifact_role,
                record=record,
            )
        except (TypeError, ValueError) as exc:
            raise _manual_lineage_conflict(
                source_work_unit_id,
                plan_work_id,
                reason="manual_import_plan_record_provenance_mismatch",
                artifactId=str(plan_artifact.artifact_id),
            ) from exc
        if _required_text(plan_work, "output_digest") != record.plan_digest:
            raise _manual_lineage_conflict(
                source_work_unit_id,
                plan_work_id,
                reason="manual_import_plan_output_digest_mismatch",
                artifactId=str(plan_artifact.artifact_id),
            )

    def _read_normalization_output(
        self,
        connection: Connection,
        source_work_unit_id: UUID,
        *,
        outputs: tuple[_BoundArtifact, ...],
        inputs: tuple[_BoundArtifact, ...],
        manual_record_lineage: ManualRecordLineage,
        output_digest: str,
    ) -> tuple[ArtifactIdentity, bytes, bytes]:
        output_bindings = {item.artifact.role: item.artifact for item in outputs}
        input_bindings = {item.artifact.role: item.artifact for item in inputs}
        expected_input_roles = {
            "extracted_record",
            "normalization_profile",
            "manual_record_lineage",
        }
        if (
            len(outputs) != 1
            or set(output_bindings) != {"field_observation_batch"}
            or len(inputs) != 3
            or set(input_bindings) != expected_input_roles
        ):
            raise _catalog_conflict(
                "PIPELINE_NORMALIZATION_ARTIFACT_BINDINGS_INVALID",
                "Normalization success must bind one observation batch, extracted record, "
                "profile, and manual-record lineage.",
                source_work_unit_id,
                "Repair the exact normalization artifact roles before advancement.",
            )
        output = output_bindings["field_observation_batch"]
        extracted_artifact = input_bindings["extracted_record"]
        profile_artifact = input_bindings["normalization_profile"]
        if (
            output.content_type != "application/vnd.collection.field-observation-batch+json"
            or extracted_artifact.content_type != "application/vnd.collection.extracted-record+json"
            or profile_artifact.content_type
            != "application/vnd.collection.normalization-profile+json"
        ):
            raise _catalog_conflict(
                "PIPELINE_NORMALIZATION_ARTIFACT_CONTRACT_INVALID",
                "Normalization artifact content types do not match their exact roles.",
                source_work_unit_id,
                "Repair the immutable normalization output and input artifact metadata.",
            )
        batch_content = self._read_artifact_content(
            connection, output, source_work_unit_id, maximum_bytes=16 * 1024 * 1024
        )
        extracted_content = self._read_artifact_content(
            connection, extracted_artifact, source_work_unit_id, maximum_bytes=64 * 1024 * 1024
        )
        profile_content = self._read_artifact_content(
            connection, profile_artifact, source_work_unit_id, maximum_bytes=1024 * 1024
        )
        try:
            batch = decode_observation_batch(batch_content)
            extracted = decode_extracted_record(extracted_content)
            profile = NormalizationProfile.model_validate_json(profile_content, strict=True)
            canonical_profile = json.dumps(
                profile.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise _catalog_conflict(
                "PIPELINE_NORMALIZATION_ARTIFACT_INVALID",
                "The exact batch, extracted record, or normalization profile violates "
                "its contract.",
                source_work_unit_id,
                "Repair the immutable normalization producer artifacts before advancement.",
            ) from exc
        if (
            batch.source_record_id != manual_record_lineage.manual_record_content_digest
            or extracted.source_record_id != manual_record_lineage.manual_record_content_digest
        ):
            raise _manual_lineage_conflict(
                source_work_unit_id,
                manual_record_lineage.manual_record_work_id,
                reason="observation_lineage_source_record_mismatch",
            )
        if (
            canonical_observation_batch_json(batch).encode("utf-8") != batch_content
            or batch.content_digest != output_digest
            or canonical_profile != profile_content
            or batch.source_record_id != extracted.source_record_id
            or batch.extracted_record_digest != extracted.content_digest
            or batch.raw_artifact_digest != extracted.raw_artifact_digest
            or batch.source_policy_digest != extracted.source_policy_digest
            or batch.normalizer_revision != profile.normalizer_revision
        ):
            raise _catalog_conflict(
                "PIPELINE_NORMALIZATION_LINEAGE_MISMATCH",
                "The normalized batch, extracted record, profile, and work output do not "
                "share exact lineage.",
                source_work_unit_id,
                "Repair the exact upstream artifact bindings; do not infer normalization "
                "provenance.",
            )
        return output, batch_content, profile_content

    def _read_source_record_mapping_output(
        self,
        connection: Connection,
        source_work_unit_id: UUID,
        *,
        outputs: tuple[_BoundArtifact, ...],
        inputs: tuple[_BoundArtifact, ...],
        output_digest: str,
    ) -> tuple[ArtifactIdentity, bytes, bytes]:
        """Read the mapping output and the exact immutable profile that produced it."""
        batches = tuple(item for item in outputs if item.artifact.role == _SOURCE_RECORD_BATCH_ROLE)
        profiles = tuple(
            item for item in inputs if item.artifact.role == _SOURCE_MAPPING_PROFILE_ROLE
        )
        if len(batches) != 1:
            raise _catalog_conflict(
                "PIPELINE_SOURCE_RECORD_BATCH_AMBIGUOUS",
                "Source-record mapping must publish exactly one observation batch.",
                source_work_unit_id,
                "Repair the mapping output binding.",
            )
        if len(profiles) != 1:
            raise _catalog_conflict(
                "PIPELINE_SOURCE_RECORD_PROFILE_AMBIGUOUS",
                "Source-record mapping must consume exactly one mapping profile.",
                source_work_unit_id,
                "Repair the mapping profile input binding.",
            )
        batch = batches[0].artifact
        if batch.content_digest != output_digest:
            raise _catalog_conflict(
                "PIPELINE_SOURCE_RECORD_BATCH_DIGEST_CONFLICT",
                "The published batch digest differs from the succeeded output digest.",
                source_work_unit_id,
                "Publish the exact durable batch bytes.",
            )
        batch_content = self._read_artifact_content(
            connection,
            batch,
            source_work_unit_id,
            maximum_bytes=8 * 1024 * 1024,
        )
        profile_content = self._read_artifact_content(
            connection,
            profiles[0].artifact,
            source_work_unit_id,
            maximum_bytes=1024 * 1024,
        )
        return batch, batch_content, profile_content

    def _read_osm_result(
        self,
        connection: Connection,
        source_work_unit_id: UUID,
        *,
        outputs: tuple[_BoundArtifact, ...],
        inputs: tuple[_BoundArtifact, ...],
        output_digest: str,
    ) -> tuple[ArtifactIdentity, ArtifactIdentity, bytes]:
        """Verify the two-role `osm-overpass-result@1` success without a generic fallback."""

        bindings = {item.artifact.role: item.artifact for item in outputs}
        if len(outputs) != 2 or set(bindings) != {
            _OSM_RAW_RESPONSE_ROLE,
            _OSM_OBSERVATIONS_ROLE,
        }:
            raise _catalog_conflict(
                "PIPELINE_OSM_OUTPUT_BINDINGS_INVALID",
                "OSM success must bind exactly one raw response and one observation artifact.",
                source_work_unit_id,
                "Repair OSM work completion using its canonical two output roles.",
            )
        raw_artifact = bindings[_OSM_RAW_RESPONSE_ROLE]
        observations_artifact = bindings[_OSM_OBSERVATIONS_ROLE]
        if (
            raw_artifact.content_type != _OSM_RAW_RESPONSE_CONTENT_TYPE
            or observations_artifact.content_type != _OSM_OBSERVATIONS_CONTENT_TYPE
        ):
            raise _catalog_conflict(
                "PIPELINE_OSM_OUTPUT_CONTRACT_INVALID",
                "OSM output artifact content types do not match their exact roles.",
                source_work_unit_id,
                "Republish the OSM result using its declared raw and observation content types.",
            )
        query_bindings = tuple(
            item.artifact for item in inputs if item.artifact.role == "overpass_query_spec"
        )
        if len(query_bindings) != 1:
            raise _catalog_conflict(
                "PIPELINE_OSM_SOURCE_IDENTITY_MISSING",
                "OSM success must bind exactly one approved Overpass query specification.",
                source_work_unit_id,
                "Restore the exact query artifact that authorised this OSM request.",
            )
        raw_content = self._read_artifact_content(
            connection,
            raw_artifact,
            source_work_unit_id,
            maximum_bytes=_OSM_MAX_RAW_RESPONSE_BYTES,
        )
        observations_content = self._read_artifact_content(
            connection,
            observations_artifact,
            source_work_unit_id,
            maximum_bytes=_OSM_MAX_OBSERVATIONS_BYTES,
        )
        try:
            envelope = decode_osm_observation_batch(observations_content)
        except ValueError as exc:
            raise _catalog_conflict(
                "PIPELINE_OSM_OBSERVATIONS_INVALID",
                "The OSM observation artifact violates its bounded contract.",
                source_work_unit_id,
                "Repair the immutable OSM observation producer output before advancement.",
            ) from exc
        if envelope.response_digest != _content_digest(raw_content):
            raise _catalog_conflict(
                "PIPELINE_OSM_RAW_DIGEST_MISMATCH",
                "The OSM observation envelope does not describe the bound raw response.",
                source_work_unit_id,
                "Repair the exact raw/observation artifact pairing before advancement.",
            )
        expected_digest = _osm_result_digest(
            output_contract=_OSM_RESULT_CONTRACT,
            raw_digest=raw_artifact.content_digest,
            observation_digest=observations_artifact.content_digest,
        )
        if output_digest != expected_digest:
            raise _catalog_conflict(
                "PIPELINE_OSM_RESULT_DIGEST_MISMATCH",
                "The OSM result digest does not match its exact output artifacts.",
                source_work_unit_id,
                "Recompute and republish the OSM result from its immutable artifacts.",
            )
        return raw_artifact, observations_artifact, observations_content

    def _read_wikidata_result(
        self,
        connection: Connection,
        source_work_unit_id: UUID,
        *,
        outputs: tuple[_BoundArtifact, ...],
        inputs: tuple[_BoundArtifact, ...],
        output_digest: str,
    ) -> tuple[ArtifactIdentity, ArtifactIdentity, bytes, bytes]:
        """Verify the exact raw response, result envelope and declared query set.

        The collector decodes the wire contract, not the connector: a consumer never depends
        on source-specific adaptation code to decide whether a producer output is
        trustworthy, and every accepted record is re-verified against its own digest.
        """

        by_role = {item.artifact.role: item.artifact for item in outputs}
        if (
            len(outputs) != 2
            or len(by_role) != len(outputs)
            or set(by_role)
            != {
                _WIKIDATA_RAW_ROLE,
                _WIKIDATA_RESULT_ROLE,
            }
        ):
            raise _catalog_conflict(
                "PIPELINE_WIKIDATA_OUTPUT_BINDINGS_INVALID",
                "Wikidata success must bind exactly one raw response and one result envelope.",
                source_work_unit_id,
                "Repair successful acquisition completion with its canonical artifact roles.",
            )
        raw_artifact = by_role[_WIKIDATA_RAW_ROLE]
        envelope_artifact = by_role[_WIKIDATA_RESULT_ROLE]
        raw_content = self._read_artifact_content(
            connection,
            raw_artifact,
            source_work_unit_id,
            maximum_bytes=_WIKIDATA_MAX_RAW_RESPONSE_BYTES,
        )
        envelope_content = self._read_artifact_content(
            connection,
            envelope_artifact,
            source_work_unit_id,
            maximum_bytes=_WIKIDATA_MAX_RESULT_BYTES,
        )
        try:
            envelope = decode_wikidata_entity_result(envelope_content)
        except ValueError as exc:
            raise _catalog_conflict(
                "PIPELINE_WIKIDATA_RESULT_INVALID",
                "The Wikidata result envelope violates its bounded contract.",
                source_work_unit_id,
                "Repair the immutable Wikidata result before advancement.",
            ) from exc
        if envelope.content_digest != envelope_artifact.content_digest:
            raise _catalog_conflict(
                "PIPELINE_WIKIDATA_RESULT_INVALID",
                "The Wikidata result envelope bytes differ from their artifact digest.",
                source_work_unit_id,
                "Repair the immutable Wikidata result before advancement.",
            )
        if envelope.response_digest != _content_digest(raw_content):
            raise _catalog_conflict(
                "PIPELINE_WIKIDATA_RAW_DIGEST_MISMATCH",
                "The Wikidata result envelope does not describe the bound raw response.",
                source_work_unit_id,
                "Repair the exact raw/result artifact pairing before advancement.",
            )
        expected_digest = _wikidata_result_digest(
            output_contract=_WIKIDATA_RESULT_CONTRACT,
            raw_digest=raw_artifact.content_digest,
            envelope_digest=envelope_artifact.content_digest,
        )
        if output_digest != expected_digest:
            raise _catalog_conflict(
                "PIPELINE_WIKIDATA_RESULT_DIGEST_MISMATCH",
                "The Wikidata result digest does not match its exact output artifacts.",
                source_work_unit_id,
                "Recompute and republish the Wikidata result from its immutable artifacts.",
            )
        spec_bindings = tuple(
            item.artifact for item in inputs if item.artifact.role == _WIKIDATA_QUERY_ROLE
        )
        if len(spec_bindings) != 1:
            raise _catalog_conflict(
                "PIPELINE_WIKIDATA_QUERY_BINDINGS_INVALID",
                "Wikidata acquisition must bind exactly one declared entity query.",
                source_work_unit_id,
                "Repair the work input so it names the exact query specification.",
            )
        spec_content = self._read_artifact_content(
            connection,
            spec_bindings[0],
            source_work_unit_id,
            maximum_bytes=_WIKIDATA_MAX_QUERY_SPEC_BYTES,
        )
        try:
            spec = decode_wikidata_query_spec(spec_content)
        except ValueError as exc:
            raise _catalog_conflict(
                "PIPELINE_WIKIDATA_QUERY_SPEC_INVALID",
                "The bound Wikidata query specification violates its contract.",
                source_work_unit_id,
                "Repair the query specification at its producing stage.",
            ) from exc
        if spec.entity_ids != envelope.requested_entity_ids:
            raise _catalog_conflict(
                "PIPELINE_WIKIDATA_ENTITY_SET_CONFLICT",
                "The Wikidata result answers a different entity set than the bound query.",
                source_work_unit_id,
                "Re-run the acquisition for the declared entity set.",
            )
        return raw_artifact, envelope_artifact, envelope_content, spec_content

    def _read_campaign_bundle(
        self,
        connection: Connection,
        bundle_digest: str,
        source_work_unit_id: UUID,
        *,
        expected_campaign_key: str,
    ) -> bytes:
        if self._object_store is None:
            raise _catalog_conflict(
                "PIPELINE_ARTIFACT_READER_UNAVAILABLE",
                "The succeeded-work catalog cannot read immutable snapshot content.",
                source_work_unit_id,
                (
                    "Configure the verified object-store reader before "
                    "registering pipeline advancement."
                ),
            )
        bundle = (
            connection.execute(
                sa.select(config_bundles).where(config_bundles.c.bundle_digest == bundle_digest)
            )
            .mappings()
            .one_or_none()
        )
        components = tuple(
            connection.execute(
                sa.select(
                    config_bundle_components.c.path,
                    config_bundle_components.c.component_digest,
                )
                .where(config_bundle_components.c.bundle_digest == bundle_digest)
                .order_by(config_bundle_components.c.position)
            ).all()
        )
        artifact_ids = tuple(
            connection.execute(
                sa.select(config_bundle_artifacts.c.artifact_id).where(
                    config_bundle_artifacts.c.bundle_digest == bundle_digest
                )
            ).scalars()
        )
        if (
            bundle is None
            or bundle["readiness"] != "ready"
            or bundle["campaign_key"] != expected_campaign_key
            or len(artifact_ids) != 1
        ):
            raise _catalog_conflict(
                "PIPELINE_SNAPSHOT_IDENTITY_INVALID",
                "The run's immutable campaign snapshot has incomplete canonical metadata.",
                source_work_unit_id,
                "Repair the exact campaign snapshot metadata before advancing its work.",
            )
        content, artifact_kind = self._read_artifact_identity(
            connection,
            artifact_ids[0],
            source_work_unit_id,
            maximum_bytes=16 * 1024 * 1024,
        )
        if artifact_kind != "config_bundle" or _content_digest(content) != bundle_digest:
            raise _catalog_conflict(
                "PIPELINE_SNAPSHOT_ARTIFACT_CONFLICT",
                "The run snapshot artifact does not match its immutable bundle identity.",
                source_work_unit_id,
                "Repair the snapshot artifact binding and preserve the exact run digest.",
            )
        try:
            document = json.loads(content.decode("utf-8"))
            components_in_artifact = tuple(
                (item["path"], item["digest"]) for item in document["components"]
            )
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise _catalog_conflict(
                "PIPELINE_SNAPSHOT_ARTIFACT_INVALID",
                "The exact campaign snapshot artifact has no valid component ledger.",
                source_work_unit_id,
                (
                    "Repair the immutable snapshot content through its owner; "
                    "do not rebuild it during read."
                ),
            ) from exc
        expected_components = tuple((row.path, row.component_digest) for row in components)
        if (
            document.get("campaignKey") != bundle["campaign_key"]
            or components_in_artifact != expected_components
        ):
            raise _catalog_conflict(
                "PIPELINE_SNAPSHOT_METADATA_CONFLICT",
                "Snapshot bytes and canonical component metadata disagree.",
                source_work_unit_id,
                "Repair the immutable snapshot owner metadata before advancing work.",
            )
        return content

    def _read_artifact_content(
        self,
        connection: Connection,
        identity: ArtifactIdentity,
        source_work_unit_id: UUID,
        *,
        maximum_bytes: int,
    ) -> bytes:
        content, _ = self._read_artifact_identity(
            connection,
            identity.artifact_id,
            source_work_unit_id,
            maximum_bytes=maximum_bytes,
            expected_identity=identity,
        )
        return content

    def _read_artifact_identity(
        self,
        connection: Connection,
        artifact_id: UUID,
        source_work_unit_id: UUID,
        *,
        maximum_bytes: int,
        expected_identity: ArtifactIdentity | None = None,
    ) -> tuple[bytes, str]:
        if self._object_store is None:
            raise _catalog_conflict(
                "PIPELINE_ARTIFACT_READER_UNAVAILABLE",
                "The succeeded-work catalog cannot read immutable artifact content.",
                source_work_unit_id,
                (
                    "Configure the verified object-store reader before "
                    "registering pipeline advancement."
                ),
            )
        row = (
            connection.execute(
                sa.select(
                    artifact_records.c.content_type,
                    artifact_objects.c.artifact_kind,
                    artifact_objects.c.content_digest,
                    artifact_objects.c.size_bytes,
                    artifact_objects.c.storage_reference,
                )
                .select_from(
                    artifact_records.join(
                        artifact_objects,
                        artifact_objects.c.object_id == artifact_records.c.object_id,
                    )
                )
                .where(artifact_records.c.artifact_id == artifact_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise _catalog_conflict(
                "PIPELINE_ARTIFACT_METADATA_NOT_FOUND",
                "The exact immutable artifact has no canonical metadata owner.",
                source_work_unit_id,
                "Repair the artifact binding before pipeline advancement.",
            )
        if expected_identity is not None and (
            row["content_digest"] != expected_identity.content_digest
            or row["size_bytes"] != expected_identity.size_bytes
            or row["content_type"] != expected_identity.content_type
        ):
            raise _catalog_conflict(
                "PIPELINE_ARTIFACT_IDENTITY_CONFLICT",
                "Artifact metadata differs from the successful work binding.",
                source_work_unit_id,
                "Repair the immutable artifact binding before pipeline advancement.",
            )
        try:
            content = self._object_store.read_verified(
                storage_reference=_required_text(row, "storage_reference"),
                expected_digest=_required_text(row, "content_digest"),
                expected_size_bytes=_non_negative_int(row["size_bytes"], "artifact size"),
                expected_content_type=_required_text(row, "content_type"),
                maximum_bytes=maximum_bytes,
            )
        except ArtifactObjectStoreError as exc:
            raise PipelineAdvancementConflict(
                code=exc.code,
                message=exc.message,
                context={
                    "sourceWorkUnitId": str(source_work_unit_id),
                    **dict(exc.context),
                },
                required_action=exc.required_action,
            ) from exc
        return content, _required_text(row, "artifact_kind")

    def list_unregistered_succeeded(
        self,
        *,
        limit: int,
        correlation_id: str,
    ) -> SuccessfulWorkDiscoveryBatch:
        if not 1 <= limit <= 1_000:
            raise ValueError("successful work discovery limit must be between 1 and 1000")
        try:
            candidates = _advance_discovery_cursor(
                self._engine,
                limit=limit,
                correlation_id=correlation_id,
            )
            return _rehydrate_discovery_candidates(self, candidates, correlation_id)
        except PipelineAdvancementConflict:
            raise
        except SQLAlchemyError as exc:
            raise PipelineAdvancementConflict(
                code="PIPELINE_DISCOVERY_STORAGE_FAILED",
                message="Successful work discovery did not complete.",
                context={"causeType": type(exc).__name__},
                required_action="Inspect PostgreSQL owner state and retry exact discovery.",
            ) from exc


def _advance_discovery_cursor(
    engine: Engine,
    *,
    limit: int,
    correlation_id: str,
) -> tuple[RowMapping, ...]:
    with engine.begin() as connection:
        cursor_row = (
            connection.execute(
                sa.select(
                    pipeline_advancement_discovery_cursor.c.last_scanned_updated_at_utc,
                    pipeline_advancement_discovery_cursor.c.last_scanned_work_id,
                    pipeline_advancement_discovery_cursor.c.cycle_max_updated_at_utc,
                    pipeline_advancement_discovery_cursor.c.cycle_max_work_id,
                )
                .where(pipeline_advancement_discovery_cursor.c.cursor_id == 1)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if cursor_row is None:
            raise PipelineAdvancementConflict(
                code="PIPELINE_DISCOVERY_CURSOR_MISSING",
                message="The canonical succeeded-work discovery cursor is unavailable.",
                context={"correlationId": correlation_id},
                required_action="Apply the Pipeline Advancement discovery-cursor migration.",
            )
        cursor_time = cursor_row["last_scanned_updated_at_utc"]
        cursor_id = cursor_row["last_scanned_work_id"]
        cycle_max_time = cursor_row["cycle_max_updated_at_utc"]
        cycle_max_id = cursor_row["cycle_max_work_id"]
        if not _valid_discovery_cursor(cursor_time, cursor_id, cycle_max_time, cycle_max_id):
            raise PipelineAdvancementConflict(
                code="PIPELINE_DISCOVERY_CURSOR_INVALID",
                message="The succeeded-work discovery cursor is incomplete or invalid.",
                context={"correlationId": correlation_id},
                required_action="Repair the Pipeline Advancement discovery cursor under its owner.",
            )
        after_work_id = _cursor_uuid(cursor_id, correlation_id)
        upper_work_id = _cursor_uuid(cycle_max_id, correlation_id)
        if cycle_max_time is None:
            upper_row = (
                connection.execute(_unregistered_succeeded_high_water_statement())
                .mappings()
                .one_or_none()
            )
            if upper_row is None:
                _store_discovery_cursor(connection, None, None, None, None, correlation_id)
                return ()
            cycle_max_time = upper_row["updated_at_utc"]
            cycle_max_id = upper_row["work_id"]
            if not isinstance(cycle_max_time, datetime):
                raise PipelineAdvancementConflict(
                    code="PIPELINE_DISCOVERY_CANDIDATE_INVALID",
                    message="The succeeded-work high-water candidate has no valid timestamp.",
                    context={"correlationId": correlation_id},
                    required_action="Inspect canonical work-unit timestamps under their owner.",
                )
            upper_work_id = _cursor_uuid(cycle_max_id, correlation_id)
        candidates = tuple(
            connection.execute(
                _unregistered_succeeded_statement(
                    limit,
                    after_updated_at_utc=cursor_time,
                    after_work_id=after_work_id,
                    through_updated_at_utc=cycle_max_time,
                    through_work_id=upper_work_id,
                )
            )
            .mappings()
            .all()
        )
        finished_cycle = len(candidates) < limit or bool(
            candidates
            and candidates[-1]["updated_at_utc"] == cycle_max_time
            and candidates[-1]["work_id"] == upper_work_id
        )
        if finished_cycle:
            _store_discovery_cursor(connection, None, None, None, None, correlation_id)
        else:
            last = candidates[-1]
            last_time = last["updated_at_utc"]
            if not isinstance(last_time, datetime):
                raise PipelineAdvancementConflict(
                    code="PIPELINE_DISCOVERY_CANDIDATE_INVALID",
                    message="A succeeded-work candidate has no valid update timestamp.",
                    context={"correlationId": correlation_id},
                    required_action="Inspect the canonical work-unit timestamp under its owner.",
                )
            _store_discovery_cursor(
                connection,
                last_time,
                _cursor_uuid(last["work_id"], correlation_id),
                cycle_max_time,
                upper_work_id,
                correlation_id,
            )
    return candidates


def _valid_discovery_cursor(
    cursor_time: object,
    cursor_id: object,
    cycle_max_time: object,
    cycle_max_id: object,
) -> bool:
    return (
        (cursor_time is None) == (cursor_id is None)
        and (cycle_max_time is None) == (cycle_max_id is None)
        and (cursor_time is None or isinstance(cursor_time, datetime))
        and (cycle_max_time is None or isinstance(cycle_max_time, datetime))
        and (cursor_time is None or cycle_max_time is not None)
    )


def _cursor_uuid(value: object, correlation_id: str) -> UUID | None:
    if value is None:
        return None
    try:
        return _uuid_value(value)
    except (TypeError, ValueError) as exc:
        raise PipelineAdvancementConflict(
            code="PIPELINE_DISCOVERY_CURSOR_INVALID",
            message="The succeeded-work discovery cursor contains an invalid work ID.",
            context={"correlationId": correlation_id},
            required_action="Repair the Pipeline Advancement discovery cursor under its owner.",
        ) from exc


def _store_discovery_cursor(
    connection: Connection,
    cursor_time: datetime | None,
    cursor_id: UUID | None,
    cycle_max_time: datetime | None,
    cycle_max_id: UUID | None,
    correlation_id: str,
) -> None:
    update = connection.execute(
        sa.update(pipeline_advancement_discovery_cursor)
        .where(pipeline_advancement_discovery_cursor.c.cursor_id == 1)
        .values(
            last_scanned_updated_at_utc=cursor_time,
            last_scanned_work_id=cursor_id,
            cycle_max_updated_at_utc=cycle_max_time,
            cycle_max_work_id=cycle_max_id,
        )
    )
    if update.rowcount != 1:
        raise PipelineAdvancementConflict(
            code="PIPELINE_DISCOVERY_CURSOR_UPDATE_CONFLICT",
            message="The succeeded-work discovery cursor changed unexpectedly.",
            context={"correlationId": correlation_id},
            required_action="Inspect Pipeline Advancement cursor ownership and retry discovery.",
        )


def _rehydrate_discovery_candidates(
    catalog: PostgresSucceededWorkCatalog,
    candidates: tuple[RowMapping, ...],
    correlation_id: str,
) -> SuccessfulWorkDiscoveryBatch:
    outputs: list[SucceededWorkOutput] = []
    failures: list[PipelineDiscoveryFailure] = []
    with catalog._engine.connect() as connection:
        for row in candidates:
            work_id = _uuid_value(row["work_id"])
            try:
                outputs.append(catalog.read(connection, work_id))
            except PipelineAdvancementConflict as exc:
                failures.append(
                    PipelineDiscoveryFailure(
                        source_work_unit_id=work_id,
                        code=exc.code,
                        message=exc.message,
                        required_action=exc.required_action,
                        context={"correlationId": correlation_id, **dict(exc.context)},
                    )
                )
            except (TypeError, ValueError) as exc:
                failures.append(
                    PipelineDiscoveryFailure(
                        source_work_unit_id=work_id,
                        code="PIPELINE_DISCOVERY_OUTPUT_INVALID",
                        message="The succeeded work output could not be rehydrated.",
                        required_action=(
                            "Inspect the exact work, snapshot, and artifact owners; repair invalid "
                            "metadata before retrying advancement."
                        ),
                        context={
                            "correlationId": correlation_id,
                            "causeType": type(exc).__name__,
                        },
                    )
                )
    return SuccessfulWorkDiscoveryBatch(
        outputs=tuple(outputs),
        failures=tuple(failures),
    )


def _work_statement(source_work_unit_id: UUID) -> Select[tuple[object, ...]]:
    return (
        sa.select(work_units)
        .where(work_units.c.work_id == source_work_unit_id)
        .with_for_update(read=True)
    )


def _artifact_statement(
    binding: sa.Table,
    source_work_unit_id: UUID,
) -> Select[tuple[object, ...]]:
    return (
        sa.select(
            binding.c.position.label("binding_position"),
            binding.c.role.label("binding_role"),
            artifact_records.c.artifact_id.label("artifact_id"),
            artifact_objects.c.content_digest.label("content_digest"),
            artifact_objects.c.size_bytes.label("size_bytes"),
            artifact_records.c.content_type.label("content_type"),
        )
        .select_from(
            binding.join(
                artifact_records,
                binding.c.artifact_id == artifact_records.c.artifact_id,
            ).join(
                artifact_objects,
                artifact_records.c.object_id == artifact_objects.c.object_id,
            )
        )
        .where(binding.c.work_id == source_work_unit_id)
        .order_by(binding.c.position, artifact_records.c.artifact_id)
    )


def _unregistered_succeeded_high_water_statement() -> Select[tuple[UUID, datetime]]:
    return (
        sa.select(work_units.c.work_id, work_units.c.updated_at_utc)
        .where(
            work_units.c.state == "succeeded",
            ~sa.exists(
                sa.select(sa.literal(1)).where(
                    pipeline_advancements.c.source_work_unit_id == work_units.c.work_id
                )
            ),
        )
        .order_by(work_units.c.updated_at_utc.desc(), work_units.c.work_id.desc())
        .limit(1)
    )


def _unregistered_succeeded_statement(
    limit: int,
    *,
    after_updated_at_utc: datetime | None,
    after_work_id: UUID | None,
    through_updated_at_utc: datetime | None,
    through_work_id: UUID | None,
) -> Select[tuple[UUID, datetime]]:
    if (after_updated_at_utc is None) != (after_work_id is None):
        raise ValueError("discovery cursor time and work ID must both be set or both be absent")
    if (through_updated_at_utc is None) != (through_work_id is None):
        raise ValueError("discovery high-water time and work ID must both be set or both be absent")
    statement = sa.select(work_units.c.work_id, work_units.c.updated_at_utc).where(
        work_units.c.state == "succeeded",
        ~sa.exists(
            sa.select(sa.literal(1)).where(
                pipeline_advancements.c.source_work_unit_id == work_units.c.work_id
            )
        ),
    )
    if after_updated_at_utc is not None and after_work_id is not None:
        statement = statement.where(
            sa.or_(
                work_units.c.updated_at_utc > after_updated_at_utc,
                sa.and_(
                    work_units.c.updated_at_utc == after_updated_at_utc,
                    work_units.c.work_id > after_work_id,
                ),
            )
        )
    if through_updated_at_utc is not None and through_work_id is not None:
        statement = statement.where(
            sa.or_(
                work_units.c.updated_at_utc < through_updated_at_utc,
                sa.and_(
                    work_units.c.updated_at_utc == through_updated_at_utc,
                    work_units.c.work_id <= through_work_id,
                ),
            )
        )
    return statement.order_by(work_units.c.updated_at_utc, work_units.c.work_id).limit(limit)


def _load_artifacts(
    connection: Connection,
    binding: sa.Table,
    source_work_unit_id: UUID,
) -> tuple[_BoundArtifact, ...]:
    rows = connection.execute(_artifact_statement(binding, source_work_unit_id)).mappings()
    result = tuple(_bound_artifact(row) for row in rows)
    artifact_ids = tuple(item.artifact.artifact_id for item in result)
    if len(artifact_ids) != len(set(artifact_ids)):
        raise _catalog_conflict(
            "PIPELINE_ARTIFACT_BINDING_DUPLICATE",
            "Successful work contains duplicate artifact bindings.",
            source_work_unit_id,
            "Repair duplicate immutable bindings before advancement.",
        )
    roles = tuple(item.artifact.role for item in result)
    if len(roles) != len(set(roles)):
        raise _catalog_conflict(
            "PIPELINE_ARTIFACT_ROLE_DUPLICATE",
            "Successful work contains duplicate artifact roles.",
            source_work_unit_id,
            "Repair duplicate immutable roles before advancement.",
        )
    return result


def _bound_artifact(row: RowMapping) -> _BoundArtifact:
    position = row["binding_position"]
    if not isinstance(position, int) or position < 0:
        raise TypeError("persisted artifact binding position is invalid")
    return _BoundArtifact(
        position=position,
        artifact=ArtifactIdentity(
            artifact_id=_uuid_value(row["artifact_id"]),
            role=_text_value(row["binding_role"], "artifact binding role"),
            content_digest=_text_value(row["content_digest"], "artifact digest"),
            size_bytes=_non_negative_int(row["size_bytes"], "artifact size"),
            content_type=_text_value(row["content_type"], "artifact content type"),
        ),
    )


def _manual_lineage_conflict(
    source_work_unit_id: UUID,
    manual_record_work_id: UUID | None,
    *,
    reason: str,
    **context: object,
) -> PipelineAdvancementConflict:
    details: dict[str, object] = {
        "sourceWorkUnitId": str(source_work_unit_id),
        "reason": reason,
        **context,
    }
    if manual_record_work_id is not None:
        details["manualRecordWorkId"] = str(manual_record_work_id)
    return PipelineAdvancementConflict(
        code="PIPELINE_MANUAL_RECORD_LINEAGE_PROVENANCE_INVALID",
        message="The manual-record lineage does not resolve to exact canonical work and artifacts.",
        context=details,
        required_action=(
            "Repair the exact manual-record work, artifact bindings, and stored bytes at their "
            "canonical owners before advancing this output."
        ),
    )


def _catalog_conflict(
    code: str,
    message: str,
    source_work_unit_id: UUID,
    required_action: str,
) -> PipelineAdvancementConflict:
    return PipelineAdvancementConflict(
        code=code,
        message=message,
        context={"sourceWorkUnitId": str(source_work_unit_id)},
        required_action=required_action,
    )


def _required_uuid(row: RowMapping, key: str) -> UUID:
    return _uuid_value(row[key])


def _required_text(row: RowMapping, key: str) -> str:
    return _text_value(row[key], key)


def _uuid_value(value: object) -> UUID:
    if value is None:
        raise TypeError("persisted UUID value is null")
    return value if isinstance(value, UUID) else UUID(str(value))


def _text_value(value: object, meaning: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"persisted {meaning} is not non-empty text")
    return value


def _content_digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _osm_result_digest(
    *,
    output_contract: str,
    raw_digest: str,
    observation_digest: str,
) -> str:
    """Recompute the producer-owned OSM result digest independently.

    The payload shape is fixed by `apps/osm_worker` publishing `osm-overpass-result@1`;
    it is restated here as a literal because architecture rules forbid infrastructure
    from importing a connector.
    """

    payload = json.dumps(
        {
            "contract": output_contract,
            "observationsDigest": observation_digest,
            "rawResponseDigest": raw_digest,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _content_digest(payload)


def _wikidata_result_digest(
    *,
    output_contract: str,
    raw_digest: str,
    envelope_digest: str,
) -> str:
    payload = json.dumps(
        {
            "contract": output_contract,
            "envelopeDigest": envelope_digest,
            "rawResponseDigest": raw_digest,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _content_digest(payload)


def decode_osm_observation_batch(content: bytes) -> OsmObservationBatchEnvelope:
    """Validate the bounded `osm_observations` document without the connector import."""

    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("OSM observations artifact is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("OSM observations artifact must be a JSON object")
    required = {
        "attribution",
        "generator",
        "observations",
        "osmBaseTimestampUtc",
        "queryDigest",
        "responseDigest",
    }
    if set(document) != required:
        raise ValueError("OSM observations artifact has an unexpected document shape")
    query_digest = document["queryDigest"]
    response_digest = document["responseDigest"]
    for name, value in (("queryDigest", query_digest), ("responseDigest", response_digest)):
        if not isinstance(value, str) or _SHA256_DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError(f"OSM observations {name} is not a canonical digest")
    attribution = document["attribution"]
    if attribution != "© OpenStreetMap contributors":
        raise ValueError("OSM observations must keep its canonical ODbL attribution")
    base_timestamp = document["osmBaseTimestampUtc"]
    if base_timestamp is not None and not isinstance(base_timestamp, str):
        raise ValueError("OSM base timestamp must be a string or null")
    observations = document["observations"]
    if not isinstance(observations, list):
        raise ValueError("OSM observations must be a list")
    identities: list[tuple[str, int]] = []
    for entry in observations:
        if not isinstance(entry, dict):
            raise ValueError("OSM observation entry must be a JSON object")
        element_type = entry.get("elementType")
        osm_id = entry.get("osmId")
        if element_type not in {"node", "way", "relation"}:
            raise ValueError("OSM observation has an unsupported element type")
        if not isinstance(osm_id, int) or isinstance(osm_id, bool) or osm_id <= 0:
            raise ValueError("OSM observation has an invalid element id")
        if entry.get("sourceUrl") != f"https://www.openstreetmap.org/{element_type}/{osm_id}":
            raise ValueError("OSM observation source URL does not match its identity")
        if not isinstance(entry.get("tags"), dict):
            raise ValueError("OSM observation tags must be an object")
        if not isinstance(entry.get("address"), dict):
            raise ValueError("OSM observation address must be an object")
        latitude = entry.get("latitude")
        longitude = entry.get("longitude")
        if not isinstance(latitude, (int, float)) or isinstance(latitude, bool):
            raise ValueError("OSM observation latitude is not numeric")
        if not isinstance(longitude, (int, float)) or isinstance(longitude, bool):
            raise ValueError("OSM observation longitude is not numeric")
        if not -90.0 <= float(latitude) <= 90.0 or not -180.0 <= float(longitude) <= 180.0:
            raise ValueError("OSM observation coordinates are out of range")
        identities.append((str(element_type), int(osm_id)))
    if tuple(sorted(identities)) != tuple(identities):
        raise ValueError("OSM observations must be sorted by element identity")
    if len(set(identities)) != len(identities):
        raise ValueError("OSM observations contain duplicate element identities")
    return OsmObservationBatchEnvelope(
        query_digest=str(query_digest),
        response_digest=str(response_digest),
        osm_ids=tuple(identities),
        osm_base_timestamp_utc=base_timestamp,
        attribution=attribution,
    )


def _verify_official_http_bindings(
    manifest_content: bytes,
    *,
    output_digest: str,
    manifest_digest: str,
    raw_output: ArtifactIdentity | None,
    input_artifacts: tuple[_BoundArtifact, ...],
    source_work_unit_id: UUID,
) -> HttpAcquisitionManifest:
    try:
        envelope = json.loads(manifest_content)
        if not isinstance(envelope, dict) or not {
            "contract",
            "contractRevision",
        }.issubset(envelope):
            raise ValueError("required manifest discriminators are missing")
        manifest = HttpAcquisitionManifest.model_validate_json(manifest_content, strict=True)
    except ValueError as exc:
        raise _catalog_conflict(
            "PIPELINE_HTTP_MANIFEST_INVALID",
            "The official HTTP manifest violates its typed producer contract.",
            source_work_unit_id,
            "Repair the producer-owned official HTTP output before advancement.",
        ) from exc

    if (manifest.raw_artifact_digest is not None) != (raw_output is not None):
        raise _catalog_conflict(
            "PIPELINE_HTTP_RAW_BINDING_CARDINALITY",
            "The manifest raw-artifact reference does not match the bound output roles.",
            source_work_unit_id,
            "Bind exactly the raw artifact declared by the typed manifest.",
        )
    if raw_output is not None and manifest.raw_artifact_digest != raw_output.content_digest:
        raise _catalog_conflict(
            "PIPELINE_HTTP_RAW_MANIFEST_CONFLICT",
            "The raw artifact binding does not match the official HTTP manifest.",
            source_work_unit_id,
            "Repair the exact raw artifact identity and manifest digest relationship.",
        )

    raw_or_reused_digest = manifest.raw_artifact_digest
    if manifest.outcome == "unchanged":
        previous_artifacts = tuple(
            item.artifact
            for item in input_artifacts
            if item.artifact.role == "previous_raw_artifact"
        )
        if len(previous_artifacts) != 1 or (
            previous_artifacts[0].artifact_id != manifest.reused_artifact_id
            or previous_artifacts[0].content_digest != manifest.reused_content_digest
        ):
            raise _catalog_conflict(
                "PIPELINE_HTTP_REUSED_ARTIFACT_CONFLICT",
                "The unchanged result does not reference the exact previous raw artifact input.",
                source_work_unit_id,
                "Bind the exact prior raw artifact under previous_raw_artifact before completion.",
            )
        raw_or_reused_digest = manifest.reused_content_digest

    payload = json.dumps(
        {
            "contract": "official-http-acquisition@1",
            "manifestDigest": manifest_digest,
            "rawOrReusedDigest": raw_or_reused_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if output_digest != _content_digest(payload):
        raise _catalog_conflict(
            "PIPELINE_HTTP_OUTPUT_DIGEST_CONFLICT",
            "The successful HTTP result digest does not bind its manifest and raw content.",
            source_work_unit_id,
            "Repair the canonical completion digest through the Worker Gateway owner.",
        )
    return manifest


def _non_negative_int(value: object, meaning: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise TypeError(f"persisted {meaning} is not a non-negative integer")
    return value
