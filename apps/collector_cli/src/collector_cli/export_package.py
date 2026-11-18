"""Sealed production export: assemble, seal, store and report one package.

This command is a thin transport around the existing owner implementation. It performs no
assembly, no policy decision and no digest computation of its own: `ExportService` owns the
package, `PostgresExportCandidatePort` and `PostgresExportQualityReader` own the read path,
and `materialize_export_artifact` owns the verified write. Anything this module decided
locally would be a second source of truth for the export contract.

Incomplete and restricted runs are refused rather than partially emitted. A package that
silently omitted a blocked candidate would report complete coverage while withholding a
candidate that needs an external decision.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

from collection_application import CampaignSnapshotService
from collection_application.artifacts import ArtifactKind
from collection_contracts import (
    CampaignSnapshot,
    CatalogCategoryMappingDocument,
    ExportCatalogBinding,
    ExportCatalogCategoryMapping,
    ExportCatalogNameLocale,
)
from collection_infrastructure import FilesystemCampaignBundleSource
from collection_infrastructure.export.assembly import ExportAssemblyError, ExportRunContext
from collection_infrastructure.export.service import (
    ExportService,
    ExportServiceError,
    materialize_export_artifact,
)
from collection_infrastructure.object_store.s3 import S3ArtifactObjectStore
from collection_infrastructure.postgres.export_readers import (
    PostgresExportCandidatePort,
    PostgresExportQualityReader,
)
from collector_cli.candidate_selection import SelectionError, parse_candidate_revisions

_QUALITY_PROFILE_COMPONENT = "campaign.yaml"


class _ConfigurationError(Exception):
    pass


def add_arguments(
    command: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    export = command.add_parser(
        "export",
        help="Emit the sealed export package for an explicit set of candidate revisions.",
    )
    export.add_argument(
        "--campaign-key",
        required=True,
        help="Campaign whose sealed snapshot binds the run facts.",
    )
    export.add_argument(
        "--run-id",
        required=True,
        type=UUID,
        help="Run whose candidate revisions this export covers.",
    )
    export.add_argument(
        "--candidate-revision",
        action="append",
        default=[],
        metavar="CANDIDATE_ID:REVISION",
        help="Exact candidate revision to include. Repeat for each revision in the run.",
    )
    export.add_argument(
        "--correlation-id",
        required=True,
        help="Correlation id recorded in the package and any failure envelope.",
    )
    export.add_argument(
        "--generated-at-utc",
        type=datetime.fromisoformat,
        default=None,
        help=(
            "Fixed generation timestamp for reproducible output. Omit to use the current "
            "time, which makes the package digest differ per run."
        ),
    )


def run(arguments: argparse.Namespace) -> int:
    try:
        revisions = parse_candidate_revisions(arguments.candidate_revision)
    except SelectionError as exc:
        return _failure(
            code="CLI_EXPORT_SELECTION_INVALID",
            message=str(exc),
            context={"detail": str(exc)},
            correlation_id=arguments.correlation_id,
        )

    generated_at = arguments.generated_at_utc or datetime.now(UTC)
    if generated_at.tzinfo is None:
        return _failure(
            code="CLI_EXPORT_SELECTION_INVALID",
            message="The generation timestamp must carry an explicit timezone.",
            context={"generatedAtUtc": generated_at.isoformat()},
            correlation_id=arguments.correlation_id,
        )

    try:
        source = FilesystemCampaignBundleSource(_campaigns_root())
        compiled = CampaignSnapshotService(source).compile(
            arguments.campaign_key, arguments.correlation_id
        )
        snapshot = compiled.snapshot
    except (OSError, ValueError) as exc:
        return _failure(
            code="CLI_EXPORT_CAMPAIGN_UNREADABLE",
            message="The campaign bundle could not be read or validated.",
            context={
                "campaignKey": arguments.campaign_key,
                "detail": str(exc),
            },
            correlation_id=arguments.correlation_id,
        )

    if snapshot.readiness != "ready":
        return _failure(
            code="CLI_EXPORT_CAMPAIGN_NOT_READY",
            message="The campaign snapshot is not ready, so no export may be emitted.",
            context={
                "campaignKey": arguments.campaign_key,
                "readiness": snapshot.readiness,
                "blockers": ",".join(blocker.code for blocker in snapshot.blockers),
            },
            correlation_id=arguments.correlation_id,
        )

    context = ExportRunContext(
        run_id=arguments.run_id,
        campaign_bundle_digest=snapshot.bundle_digest,
        campaign_key=snapshot.campaign_key,
        # The campaign declares one export profile; both package fields name that exact
        # declared value rather than a locally split key/revision pair nobody authored.
        export_profile_key=compiled.documents.campaign.export_profile,
        export_profile_revision=compiled.documents.campaign.export_profile,
        quality_profile_digest=_quality_profile_digest(snapshot),
        catalog_binding=_catalog_binding(compiled.documents.catalog_mapping),
        generated_at_utc=generated_at,
        correlation_id=arguments.correlation_id,
    )

    engine = sa.create_engine(_required("COLLECTOR_DATABASE_URL"), pool_pre_ping=True)
    try:
        service = ExportService(
            candidates=PostgresExportCandidatePort(engine),
            quality=PostgresExportQualityReader(engine),
            candidate_revisions=revisions,
        )
        package, content = service.build_and_verify_bytes(context=context)
        content_path = _content_path()
        if content_path:
            with open(content_path, "wb") as handle:  # noqa: PTH123 - exact sealed bytes
                handle.write(content)
        reference = materialize_export_artifact(
            package=package,
            content=content,
            object_store=_object_store(),
            artifact_kind=ArtifactKind.EXPORT_ARTIFACT,
            now_utc=generated_at,
        )
    except _ConfigurationError as exc:
        return _failure(
            code="CLI_EXPORT_CONFIGURATION_INVALID",
            message=str(exc),
            context={"detail": str(exc)},
            correlation_id=arguments.correlation_id,
        )
    except (ExportAssemblyError, ExportServiceError) as exc:
        # Both owners report the same failure shape, so a blocked export names its code,
        # owner and required action instead of surfacing as a traceback.
        return _failure(
            code=exc.code,
            message=exc.message,
            context=dict(exc.context),
            owner=exc.owner,
            required_action=exc.required_action,
            correlation_id=arguments.correlation_id,
        )
    finally:
        engine.dispose()

    print(
        json.dumps(
            {
                "packageDigest": reference.package_digest,
                "contentDigest": reference.content_digest,
                "downloadReference": reference.download_reference,
                "sizeBytes": reference.size_bytes,
                "contentType": reference.content_type,
                "candidateCount": reference.candidate_count,
                "generatedAtUtc": generated_at.isoformat(),
                "runId": str(arguments.run_id),
                "campaignBundleDigest": snapshot.bundle_digest,
                "contentPath": _content_path(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _content_path() -> str:
    """Where the sealed bytes are additionally written when requested.

    The object store is the authoritative copy. Writing a local file is only for handing the
    exact bytes to the ingestion adapter, so it is opt-in and reported in the output.
    """

    return os.getenv("COLLECTOR_EXPORT_CONTENT_PATH", "")


def _catalog_binding(mapping: CatalogCategoryMappingDocument) -> ExportCatalogBinding:
    """The sealed classification pin the campaign declares, transcribed exactly.

    The mapping document is campaign-owned and already validated against the bundle's own
    taxonomy, so this only re-states its declared pairs in the sealed export vocabulary.
    """

    return ExportCatalogBinding(
        site_key=mapping.site_key,
        catalog_key=mapping.catalog_key,
        configuration_revision_id=mapping.configuration_revision_id,
        coordinate_precision_meters=mapping.coordinate_precision_meters,
        name_locales=tuple(
            ExportCatalogNameLocale(
                campaign_locale=item.campaign_locale,
                catalog_locale=item.catalog_locale,
            )
            for item in mapping.name_locales
        ),
        categories=tuple(
            ExportCatalogCategoryMapping(
                campaign_category=item.campaign_category,
                catalog_category=item.catalog_category,
            )
            for item in mapping.categories
        ),
    )


def _quality_profile_digest(snapshot: CampaignSnapshot) -> str:
    """The sealed campaign digest that carries the declared quality profile.

    The package records one profile digest; the campaign declares exactly one quality profile
    inside its sealed bundle, so the bundle component digest is the real identity of that
    profile rather than a locally invented value.
    """

    for component in snapshot.components:
        if component.path == _QUALITY_PROFILE_COMPONENT:
            return component.digest
    raise _ConfigurationError(
        "the campaign snapshot carries no quality profile component to bind the export to"
    )


def _campaigns_root() -> Path:
    return Path(os.getenv("COLLECTOR_CAMPAIGNS_ROOT", "campaigns"))


def _object_store() -> S3ArtifactObjectStore:
    return S3ArtifactObjectStore.create(
        endpoint_url=_required("ARTIFACT_S3_ENDPOINT_URL"),
        bucket=_required("ARTIFACT_S3_BUCKET"),
        access_key_id=_required("ARTIFACT_S3_ACCESS_KEY_ID"),
        secret_access_key=_required("ARTIFACT_S3_SECRET_ACCESS_KEY"),
        region_name=_required("ARTIFACT_S3_REGION"),
    )


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise _ConfigurationError(f"required export setting {name} is missing")
    return value.strip()


def _failure(
    *,
    code: str,
    message: str,
    context: dict[str, str],
    correlation_id: str,
    owner: str = "CollectorCli",
    required_action: str = "Correct the reported state and run the export again.",
) -> int:
    payload = {
        "type": "collection/export-command-failed",
        "owner": owner,
        "code": code,
        "message": message,
        "context": context,
        "requiredAction": required_action,
        "correlationId": correlation_id,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
    return 2
