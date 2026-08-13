from __future__ import annotations

from pathlib import Path


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected source fragment is missing")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def apply(root: Path) -> None:
    # The campaign geography owner keeps Polygon and MultiPolygon coordinate shapes typed.
    geography = root / "packages/collection_application/src/collection_application/geography.py"
    _replace_once(
        geography,
        'type GeographyCoverageKind = Literal["inside", "boundary", "outside"]\n',
        'type GeographyCoverageKind = Literal["inside", "boundary", "outside"]\n'
        'type PolygonCoordinates = list[list[list[float]]]\n'
        'type MultiPolygonCoordinates = list[PolygonCoordinates]\n',
    )
    _replace_once(
        geography,
        '    if geometry_type == "Polygon":\n'
        '        canonical_coordinates = _polygon(coordinates, counter=counter, owner="coordinates")\n',
        '    canonical_coordinates: PolygonCoordinates | MultiPolygonCoordinates\n'
        '    if geometry_type == "Polygon":\n'
        '        canonical_coordinates = _polygon(coordinates, counter=counter, owner="coordinates")\n',
    )
    _replace_once(
        geography,
        '        geometry_type=cast(Literal["Polygon", "MultiPolygon"], geometry_type),\n',
        '        geometry_type=geometry_type,\n',
    )

    response = root / "connectors/osm_overpass/src/osm_overpass/response.py"
    _replace_once(
        response,
        "        element_type=cast(OsmElementType, element_type),\n",
        "        element_type=element_type,\n",
    )

    http = root / "connectors/osm_overpass/src/osm_overpass/http.py"
    _replace_once(
        http,
        "        addresses = {\n"
        "            item[4][0]\n"
        "            for item in socket.getaddrinfo(\n",
        "        addresses = {\n"
        "            str(item[4][0])\n"
        "            for item in socket.getaddrinfo(\n",
    )

    manual_contracts = root / "apps/manual_import_worker/src/manual_import_worker/contracts.py"
    _replace_once(
        manual_contracts,
        "from manual_import_core import ManualImportFormat, ManualImportMode\n",
        "from collection_contracts import ManualImportFormat, ManualImportMode\n",
    )

    _write(
        root / "connectors/osm_overpass/src/osm_overpass/__init__.py",
        '''from osm_overpass.contracts import (
    GeoPoint,
    OsmAddress,
    OsmElementObservation,
    OsmElementType,
    OsmObservationBatch,
    OsmTagFilter,
    OverpassPolygon,
    OverpassQuerySpec,
)
from osm_overpass.http import (
    OverpassEndpointPolicy,
    OverpassFetchFailure,
    OverpassFetchResult,
    OverpassHttpClient,
)
from osm_overpass.input import decode_query_spec
from osm_overpass.query import build_overpass_query, query_digest
from osm_overpass.response import OverpassResponseError, parse_overpass_response

__all__ = [
    "GeoPoint",
    "OsmAddress",
    "OsmElementObservation",
    "OsmElementType",
    "OsmObservationBatch",
    "OsmTagFilter",
    "OverpassEndpointPolicy",
    "OverpassFetchFailure",
    "OverpassFetchResult",
    "OverpassHttpClient",
    "OverpassPolygon",
    "OverpassQuerySpec",
    "OverpassResponseError",
    "build_overpass_query",
    "decode_query_spec",
    "parse_overpass_response",
    "query_digest",
]
''',
    )

    _write(
        root / "apps/manual_import_worker/src/manual_import_worker/gateway.py",
        '''from __future__ import annotations

from typing import cast

import httpx

from source_connector_sdk import (
    LeaseArtifact,
    SourceWorkerGateway,
    VerifiedUpload,
    WorkerLease,
    WorkFailureKind,
)

from manual_import_worker.contracts import ManualImportWorkerSettings

_PLAN_CONTENT_TYPE = "application/vnd.collection.manual-import-plan+json"
_PLAN_OUTPUT_ROLE = "manual_import_plan"
_PLAN_OUTPUT_CONTRACTS = frozenset({"manual-import-plan", "manual-import-plan@1"})
_FAILURE_KINDS = frozenset(
    {"transient", "permanent", "policy_blocked", "contract_invalid"}
)


class SourceWorkerGatewayAdapter:
    """Maps manual-import behavior to the canonical source-worker SDK."""

    def __init__(self, client: SourceWorkerGateway) -> None:
        self._client = client
        self._build_identity: str | None = None

    def register(self, settings: ManualImportWorkerSettings) -> None:
        self._client.register(
            build_identity=settings.build_identity,
            capabilities={"manual_import"},
            supported_output_contracts=_PLAN_OUTPUT_CONTRACTS,
            max_concurrency=1,
            resource_profile=settings.resource_profile,
        )
        self._build_identity = settings.build_identity

    def acquire(self, settings: ManualImportWorkerSettings) -> WorkerLease | None:
        return self._client.acquire_lease(
            capability="manual_import",
            lease_duration_seconds=settings.lease_duration_seconds,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    def heartbeat(self, lease: WorkerLease, settings: ManualImportWorkerSettings) -> WorkerLease:
        return self._client.heartbeat(
            lease,
            lease_duration_seconds=settings.lease_duration_seconds,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        )

    def read_source(
        self,
        lease: WorkerLease,
        source: LeaseArtifact,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        prepared = self._client.prepare_read(
            lease,
            artifact_id=source.artifact_id,
        )
        body = bytearray()
        with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
            with client.stream(prepared.method, prepared.url) as response:
                if not 200 <= response.status_code < 300:
                    raise RuntimeError(
                        f"scoped artifact read failed with status {response.status_code}"
                    )
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError(
                            "manual import source exceeds the configured byte limit"
                        )
        return bytes(body)

    def publish_plan(
        self,
        lease: WorkerLease,
        payload: bytes,
        *,
        content_digest: str,
        timeout_seconds: float,
    ) -> VerifiedUpload:
        del timeout_seconds
        upload = self._client.upload_bytes(
            lease,
            content=payload,
            artifact_kind="diagnostic_artifact",
            content_type=_PLAN_CONTENT_TYPE,
        )
        if upload.content_digest != content_digest:
            raise RuntimeError("verified manual import plan digest changed during transfer")
        return upload

    def complete(self, lease: WorkerLease, *, plan_digest: str, upload: object) -> None:
        verified = cast(VerifiedUpload, upload)
        self._client.complete(
            lease,
            output_contract=lease.expected_output_contract,
            output_digest=plan_digest,
            worker_build_identity=self._required_build_identity(),
            output_artifacts=((verified.upload_id, _PLAN_OUTPUT_ROLE),),
        )

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: str,
        code: str,
        message: str,
        required_action: str,
    ) -> None:
        if failure_kind not in _FAILURE_KINDS:
            raise ValueError("manual import failure kind is unsupported")
        self._client.fail(
            lease,
            failure_kind=cast(WorkFailureKind, failure_kind),
            code=code,
            owner="ManualImportWorker",
            message=message,
            required_action=required_action,
            worker_build_identity=self._required_build_identity(),
        )

    def _required_build_identity(self) -> str:
        if self._build_identity is None:
            raise RuntimeError("manual import worker must register before processing work")
        return self._build_identity
''',
    )

    _write(
        root / "apps/osm_worker/src/osm_worker/gateway.py",
        '''from __future__ import annotations

import json
from hashlib import sha256

from source_connector_sdk import (
    SourceWorkerGateway,
    WorkerLease,
    WorkFailureKind,
)

_RAW_RESPONSE_CONTENT_TYPE = "application/json"
_OBSERVATIONS_CONTENT_TYPE = "application/vnd.collection.osm-observations+json"
_RAW_RESPONSE_ROLE = "osm_raw_response"
_OBSERVATIONS_ROLE = "osm_observations"
_OUTPUT_CONTRACTS = frozenset({"osm-overpass-result/1"})


class SdkOsmWorkerGateway:
    """Maps OSM acquisition to the canonical Worker Gateway protocol."""

    def __init__(self, client: SourceWorkerGateway) -> None:
        self._client = client
        self._build_identity: str | None = None

    def register(self, *, build_identity: str) -> None:
        self._client.register(
            build_identity=build_identity,
            capabilities={"osm_query"},
            supported_output_contracts=_OUTPUT_CONTRACTS,
            max_concurrency=1,
            resource_profile="osm-overpass",
        )
        self._build_identity = build_identity

    def acquire(
        self,
        *,
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> WorkerLease | None:
        return self._client.acquire_lease(
            capability="osm_query",
            lease_duration_seconds=lease_duration_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )

    def heartbeat(
        self,
        lease: WorkerLease,
        *,
        lease_duration_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> WorkerLease:
        return self._client.heartbeat(
            lease,
            lease_duration_seconds=lease_duration_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )

    def read_artifact(
        self,
        lease: WorkerLease,
        *,
        role: str,
        maximum_bytes: int,
    ) -> bytes:
        artifact = lease.artifact(role)
        return self._client.read_artifact(
            lease,
            artifact_id=artifact.artifact_id,
            maximum_bytes=maximum_bytes,
        )

    def publish_result(
        self,
        lease: WorkerLease,
        *,
        raw_response: bytes,
        observations: bytes,
    ) -> None:
        raw_upload = self._client.upload_bytes(
            lease,
            content=raw_response,
            artifact_kind="raw_artifact",
            content_type=_RAW_RESPONSE_CONTENT_TYPE,
        )
        observation_upload = self._client.upload_bytes(
            lease,
            content=observations,
            artifact_kind="diagnostic_artifact",
            content_type=_OBSERVATIONS_CONTENT_TYPE,
        )
        output_digest = _result_digest(
            output_contract=lease.expected_output_contract,
            raw_digest=raw_upload.content_digest,
            observation_digest=observation_upload.content_digest,
        )
        self._client.complete(
            lease,
            output_contract=lease.expected_output_contract,
            output_digest=output_digest,
            worker_build_identity=self._required_build_identity(),
            output_artifacts=(
                (raw_upload.upload_id, _RAW_RESPONSE_ROLE),
                (observation_upload.upload_id, _OBSERVATIONS_ROLE),
            ),
        )

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: WorkFailureKind,
        error_code: str,
        message: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        required_action = _required_action(failure_kind, retry_after_seconds)
        self._client.fail(
            lease,
            failure_kind=failure_kind,
            code=error_code,
            owner="OsmWorker.Overpass",
            message=message,
            required_action=required_action,
            worker_build_identity=self._required_build_identity(),
        )

    def _required_build_identity(self) -> str:
        if self._build_identity is None:
            raise RuntimeError("OSM worker must register before processing work")
        return self._build_identity


def _result_digest(
    *,
    output_contract: str,
    raw_digest: str,
    observation_digest: str,
) -> str:
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
    return f"sha256:{sha256(payload).hexdigest()}"


def _required_action(
    failure_kind: WorkFailureKind,
    retry_after_seconds: int | None,
) -> str:
    if failure_kind == "transient":
        suffix = (
            f" Do not retry before {retry_after_seconds} seconds have elapsed."
            if retry_after_seconds is not None
            else ""
        )
        return "Restore the approved Overpass endpoint and retry the exact work unit." + suffix
    if failure_kind == "policy_blocked":
        return "Review and reactivate the exact OSM source policy before scheduling new work."
    return "Correct the OSM input or connector contract before creating replacement work."
''',
    )

    osm_worker = root / "apps/osm_worker/src/osm_worker/worker.py"
    _replace_once(
        osm_worker,
        "from typing import Literal, Protocol\n",
        "from typing import Protocol\n",
    )
    _replace_once(
        osm_worker,
        "from source_connector_sdk import WorkerLease\n\n"
        "type WorkerFailureKind = Literal[\n"
        '    "transient",\n'
        '    "permanent",\n'
        '    "policy_blocked",\n'
        '    "contract_invalid",\n'
        "]\n",
        "from source_connector_sdk import WorkerLease, WorkFailureKind\n",
    )
    _replace_once(
        osm_worker,
        "        failure_kind: WorkerFailureKind,\n",
        "        failure_kind: WorkFailureKind,\n",
    )

    _write(
        root / "packages/collection_infrastructure/src/collection_infrastructure/postgres/manual_import_metadata.py",
        '''from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from collection_infrastructure.postgres.artifact_metadata import artifact_records
from collection_infrastructure.postgres.metadata import collector_metadata
from collection_infrastructure.postgres.work_metadata import work_units

MANUAL_IMPORT_SCHEMA = "manual_import"
manual_import_metadata = collector_metadata

plan_admissions = sa.Table(
    "plan_admissions",
    manual_import_metadata,
    sa.Column("admission_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "parent_work_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(work_units.c.work_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column(
        "plan_artifact_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(artifact_records.c.artifact_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "source_artifact_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(artifact_records.c.artifact_id, ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("plan_digest", sa.Text(), nullable=False),
    sa.Column("source_digest", sa.Text(), nullable=False),
    sa.Column("mode", sa.Text(), nullable=False),
    sa.Column("plan_status", sa.Text(), nullable=False),
    sa.Column("target_stage", sa.Text(), nullable=False),
    sa.Column("target_capability", sa.Text(), nullable=False),
    sa.Column("target_output_contract", sa.Text(), nullable=False),
    sa.Column("total_record_count", sa.Integer(), nullable=False),
    sa.Column("accepted_record_count", sa.Integer(), nullable=False),
    sa.Column("rejected_record_count", sa.Integer(), nullable=False),
    sa.Column("child_work_count", sa.Integer(), nullable=False),
    sa.Column("result_digest", sa.Text(), nullable=False),
    sa.Column("admitted_at_utc", sa.DateTime(timezone=True), nullable=False),
    sa.Column("correlation_id", sa.Text(), nullable=False),
    sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
    sa.CheckConstraint("plan_status = 'ready'", name="ck_plan_admissions_ready"),
    sa.CheckConstraint(
        "accepted_record_count + rejected_record_count = total_record_count",
        name="ck_plan_admissions_counts",
    ),
    sa.CheckConstraint(
        "accepted_record_count = child_work_count",
        name="ck_plan_admissions_child_count",
    ),
    sa.CheckConstraint(
        "plan_digest ~ '^sha256:[0-9a-f]{64}$' AND "
        "source_digest ~ '^sha256:[0-9a-f]{64}$' AND "
        "result_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_plan_admissions_digest_format",
    ),
    sa.UniqueConstraint(
        "parent_work_id",
        "plan_artifact_id",
        name="uq_plan_admissions_parent_plan",
    ),
    schema=MANUAL_IMPORT_SCHEMA,
)

plan_admission_items = sa.Table(
    "plan_admission_items",
    manual_import_metadata,
    sa.Column(
        "admission_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(plan_admissions.c.admission_id, ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Column("position", sa.Integer(), primary_key=True),
    sa.Column(
        "child_work_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(work_units.c.work_id, ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("locator_kind", sa.Text(), nullable=False),
    sa.Column("locator_value", sa.Text(), nullable=False),
    sa.Column("record_digest", sa.Text(), nullable=False),
    sa.CheckConstraint("position >= 0", name="ck_plan_admission_items_position"),
    sa.CheckConstraint(
        "record_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="ck_plan_admission_items_record_digest_format",
    ),
    schema=MANUAL_IMPORT_SCHEMA,
)

MANUAL_IMPORT_TABLES = (plan_admissions, plan_admission_items)
''',
    )
