"""Export service: assemble, seal, persist, and expose the exact verified package bytes.

The service never assembles an export from unverified state. It reads candidate and quality
state through read-only ports, seals the package, writes the byte-identical canonical
document as an owner-controlled export artifact, and verifies the stored object before
returning a reference. A failed verification never yields a downloadable package.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from collection_application.artifacts import ArtifactKind
from collection_contracts import (
    ExportPackage,
    canonical_export_package_json,
    decode_export_package,
    verify_export_package,
)
from collection_infrastructure.export.assembly import (
    ExportCandidateSource,
    ExportRunContext,
    assemble_export_package,
)
from resolution_contracts import ClusterQualityAssessment

EXPORT_CONTENT_TYPE = "application/vnd.collection.export-package+json"


class ExportServiceError(Exception):
    def __init__(
        self,
        *,
        code: str,
        owner: str,
        message: str,
        context: Mapping[str, str],
        required_action: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.owner = owner
        self.message = message
        self.context = dict(context)
        self.required_action = required_action


@dataclass(frozen=True, slots=True)
class ExportArtifactReference:
    download_reference: str
    content_digest: str
    size_bytes: int
    content_type: str
    package_digest: str
    candidate_count: int
    generated_at_utc: datetime


class CandidatePort(Protocol):
    def read_candidates(
        self, *, candidate_revisions: Sequence[tuple[UUID, int]]
    ) -> Sequence[ExportCandidateSource]: ...


class QualityPort(Protocol):
    def read_assessments(
        self, *, run_id: UUID, cluster_ids: Sequence[UUID]
    ) -> Mapping[UUID, ClusterQualityAssessment]: ...


class OwnerArtifactWriter(Protocol):
    def store_bytes(
        self,
        *,
        artifact_kind: ArtifactKind,
        content: bytes,
        content_type: str,
        now_utc: datetime,
    ) -> object: ...


class PresigningReader(Protocol):
    def presign_read(self, *, reference: str, content_type: str) -> str: ...


class ExportService:
    def __init__(
        self,
        *,
        candidates: CandidatePort,
        quality: QualityPort,
        candidate_revisions: Sequence[tuple[UUID, int]] | None = None,
    ) -> None:
        self._candidates = candidates
        self._quality = quality
        self._candidate_revisions = tuple(candidate_revisions or ())

    def build_package(self, *, context: ExportRunContext) -> ExportPackage:
        candidates = self._candidates.read_candidates(candidate_revisions=self._candidate_revisions)
        if not candidates:
            raise ExportServiceError(
                code="EXPORT_RUN_HAS_NO_CANDIDATES",
                owner="ExportService",
                message="The run exposes no candidate revision to export.",
                context={"runId": str(context.run_id)},
                required_action=(
                    "Supply the exact candidate revisions produced by this run before "
                    "requesting an export."
                ),
            )
        cluster_ids = tuple(sorted({item.cluster_id for item in candidates}))
        assessments = self._quality.read_assessments(run_id=context.run_id, cluster_ids=cluster_ids)
        return assemble_export_package(
            context=context, candidates=candidates, assessments=assessments
        )

    def build_and_verify_bytes(self, *, context: ExportRunContext) -> tuple[ExportPackage, bytes]:
        package = self.build_package(context=context)
        content = canonical_export_package_json(package).encode("utf-8")
        decoded = decode_export_package(content)
        if decoded.package_digest != package.package_digest:
            raise ExportServiceError(
                code="EXPORT_PACKAGE_ROUND_TRIP_MISMATCH",
                owner="ExportService",
                message="The canonical export bytes did not round-trip to the same package.",
                context={
                    "runId": str(context.run_id),
                    "expectedDigest": package.package_digest,
                    "actualDigest": decoded.package_digest,
                },
                required_action="Inspect the contract serializer before releasing this export.",
            )
        verify_export_package(decoded)
        return package, content


def materialize_export_artifact(
    *,
    package: ExportPackage,
    content: bytes,
    object_store: OwnerArtifactWriter,
    artifact_kind: ArtifactKind,
    now_utc: datetime,
) -> ExportArtifactReference:
    if not content:
        raise ExportServiceError(
            code="EXPORT_ARTIFACT_EMPTY",
            owner="ExportService",
            message="Refusing to persist an empty export artifact.",
            context={"packageDigest": package.package_digest},
            required_action="Rebuild the package from complete run state.",
        )

    stored = object_store.store_bytes(
        artifact_kind=artifact_kind,
        content=content,
        content_type=EXPORT_CONTENT_TYPE,
        now_utc=now_utc,
    )
    final_reference = getattr(stored, "final_reference", None)
    content_digest = getattr(stored, "content_digest", None)
    size_bytes = getattr(stored, "size_bytes", None)
    if not isinstance(final_reference, str) or not isinstance(content_digest, str):
        raise ExportServiceError(
            code="EXPORT_ARTIFACT_UNVERIFIED",
            owner="ObjectStore",
            message="The object store did not confirm a verified export object.",
            context={"packageDigest": package.package_digest},
            required_action="Inspect the object store before retrying the export write.",
        )
    if size_bytes != len(content):
        raise ExportServiceError(
            code="EXPORT_ARTIFACT_SIZE_MISMATCH",
            owner="ObjectStore",
            message="The stored export object size does not match the sealed bytes.",
            context={
                "packageDigest": package.package_digest,
                "expectedSizeBytes": str(len(content)),
                "actualSizeBytes": str(size_bytes),
            },
            required_action="Discard the object and rebuild the export artifact.",
        )
    return ExportArtifactReference(
        download_reference=final_reference,
        content_digest=content_digest,
        size_bytes=size_bytes,
        content_type=EXPORT_CONTENT_TYPE,
        package_digest=package.package_digest,
        candidate_count=len(package.entries),
        generated_at_utc=package.generated_at_utc,
    )


__all__ = [
    "EXPORT_CONTENT_TYPE",
    "CandidatePort",
    "ExportArtifactReference",
    "ExportService",
    "ExportServiceError",
    "PresigningReader",
    "QualityPort",
    "materialize_export_artifact",
]
