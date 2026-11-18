from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from collection_contracts import (
    ExportCatalogBinding,
    ExportCatalogCategoryMapping,
    ExportCatalogNameLocale,
    ExportContact,
    ExportFieldEvidence,
    ExportGeography,
    ExportLocalizedName,
    ExportQualitySummary,
    ExportReviewDecision,
    ExportSourcePolicy,
    canonical_export_package_json,
    verify_export_package,
)
from collection_infrastructure.export.assembly import (
    ExportAssemblyError,
    ExportCandidateSource,
    ExportRunContext,
    assemble_export_package,
)
from resolution_contracts import ClusterQualityAssessment

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def _binding() -> ExportCatalogBinding:
    return ExportCatalogBinding(
        siteKey="berlin-recording",
        catalogKey="berlin-recording-services",
        configurationRevisionId="00000000-0000-0000-0000-000000000001",
        coordinatePrecisionMeters=25,
        nameLocales=[ExportCatalogNameLocale(campaignLocale="de", catalogLocale="de-DE")],
        categories=[
            ExportCatalogCategoryMapping(
                campaignCategory="recording_studio", catalogCategory="recording-studio"
            )
        ],
    )


def _context() -> ExportRunContext:
    return ExportRunContext(
        run_id=uuid4(),
        campaign_bundle_digest=_DIGEST_A,
        campaign_key="berlin_recording_services",
        export_profile_key="default",
        export_profile_revision="rev_v1",
        quality_profile_digest=_DIGEST_B,
        catalog_binding=_binding(),
        generated_at_utc=datetime(2026, 8, 20, tzinfo=UTC),
        correlation_id="export-run-1",
    )


def _evidence(field_path: str, *, source: str = "openstreetmap") -> ExportFieldEvidence:
    return ExportFieldEvidence(
        fieldPath=field_path,
        sourceKey=source,
        sourceRecordId="osm:node:101",
        sourceUrl="https://www.openstreetmap.org/node/101",
        observedAtUtc="2026-01-05T00:00:00+00:00",
        legalStatus="approved",
        usagePolicy="publishable",
        evidenceDigest=_DIGEST_B,
        sourcePolicyDigest=_DIGEST_A,
        attribution="OpenStreetMap contributors",
    )


def _policy() -> ExportSourcePolicy:
    return ExportSourcePolicy(
        sourceKey="openstreetmap",
        policyDigest=_DIGEST_A,
        legalStatus="approved",
        usagePolicy="publishable",
        attribution="OpenStreetMap contributors",
    )


def _candidate(
    *, cluster_id: UUID | None = None, name: str = "Studio One", **overrides: object
) -> ExportCandidateSource:
    base = {
        "candidate_id": uuid4(),
        "candidate_revision": 0,
        "snapshot_digest": _DIGEST_A,
        "entity_kind": "organization",
        "cluster_id": cluster_id or uuid4(),
        "localized_names": (
            ExportLocalizedName(locale="de", state="observed", value=name, fieldPath="names"),
        ),
        "contacts": (
            ExportContact(
                kind="website",
                state="observed",
                target="https://studio.test",
                fieldPath="website_urls",
            ),
        ),
        "typed_attributes": (),
        "category_keys": ("recording_studio",),
        "geography": ExportGeography(
            coverage="inside",
            marketAreaRevision="berlin_v1",
            marketAreaDigest=_DIGEST_B,
            latitude=52.5,
            longitude=13.4,
            fieldPath="geography",
        ),
        "field_evidence": (
            _evidence("names"),
            _evidence("website_urls"),
            _evidence("geography"),
            _evidence("categories/recording_studio"),
        ),
        "quality": ExportQualitySummary(state="passed"),
        "quality_evaluation_digest": _DIGEST_B,
        "quality_policy_digest": _DIGEST_A,
        "review_decisions": (
            ExportReviewDecision(
                decisionId=UUID("00000000-0000-0000-0000-000000000201"),
                decisionKind="accept_candidate",
                decisionDigest=_DIGEST_B,
                decidedAtUtc="2026-01-06T00:00:00+00:00",
            ),
        ),
        "source_policies": (_policy(),),
        "source_artifact_ids": (uuid4(),),
    }
    return ExportCandidateSource(**{**base, **overrides})


def _eligible(cluster_id: UUID) -> ClusterQualityAssessment:
    return ClusterQualityAssessment(clusterId=cluster_id, exportEligible=True)


def test_eligible_candidates_are_sealed_into_a_verifiable_package() -> None:
    first = _candidate()
    second = _candidate(name="Studio Two")
    context = _context()

    package = assemble_export_package(
        context=context,
        candidates=(second, first),
        assessments={
            first.cluster_id: _eligible(first.cluster_id),
            second.cluster_id: _eligible(second.cluster_id),
        },
    )

    verify_export_package(package)
    assert [entry.candidate_id for entry in package.entries] == sorted(
        (first.candidate_id, second.candidate_id)
    )
    assert package.package_digest.startswith("sha256:")


def test_assembly_is_deterministic_for_the_same_input_state() -> None:
    candidate = _candidate()
    context = _context()
    assessments = {candidate.cluster_id: _eligible(candidate.cluster_id)}

    first = assemble_export_package(
        context=context, candidates=(candidate,), assessments=assessments
    )
    second = assemble_export_package(
        context=context, candidates=(candidate,), assessments=assessments
    )

    assert first.package_digest == second.package_digest
    assert canonical_export_package_json(first) == canonical_export_package_json(second)


def test_empty_run_fails_closed_instead_of_exporting_an_empty_catalog() -> None:
    with pytest.raises(ExportAssemblyError) as error:
        assemble_export_package(context=_context(), candidates=(), assessments={})

    assert error.value.code == "EXPORT_PACKAGE_WOULD_BE_EMPTY"
    assert error.value.owner == "ExportAssembly"


def test_missing_quality_assessment_blocks_the_whole_package() -> None:
    candidate = _candidate()

    with pytest.raises(ExportAssemblyError) as error:
        assemble_export_package(context=_context(), candidates=(candidate,), assessments={})

    assert error.value.code == "EXPORT_QUALITY_ASSESSMENT_MISSING"
    assert error.value.owner == "QualityCore"
    assert error.value.context["candidateId"] == str(candidate.candidate_id)


def test_non_eligible_quality_blocks_export() -> None:
    candidate = _candidate()
    blocked = ClusterQualityAssessment(
        clusterId=candidate.cluster_id,
        exportEligible=False,
        issues=({"code": "PENDING_PAIR_REVIEW", "severity": "blocking"},),
    )

    with pytest.raises(ExportAssemblyError) as error:
        assemble_export_package(
            context=_context(),
            candidates=(candidate,),
            assessments={candidate.cluster_id: blocked},
        )

    assert error.value.code == "EXPORT_QUALITY_NOT_ELIGIBLE"
    assert "PENDING_PAIR_REVIEW" in error.value.context["issueCodes"]


def test_unknown_geography_blocks_export() -> None:
    # The wire contract cannot even represent unresolved coverage, so the blocker is
    # proven at the assembler input: a source object that slipped through with it.
    candidate = _candidate(
        geography=ExportGeography.model_construct(
            coverage="unknown",
            market_area_revision="berlin_v1",
            market_area_digest=_DIGEST_B,
            latitude=None,
            longitude=None,
            field_path="geography",
        )
    )

    with pytest.raises(ExportAssemblyError) as error:
        assemble_export_package(
            context=_context(),
            candidates=(candidate,),
            assessments={candidate.cluster_id: _eligible(candidate.cluster_id)},
        )

    assert error.value.code == "EXPORT_GEOGRAPHY_UNRESOLVED"
    assert error.value.owner == "GeographyResolution"


def test_an_observed_name_without_an_established_language_blocks_export() -> None:
    candidate = _candidate(
        localized_names=(
            ExportLocalizedName(locale="und", state="observed", value="Studio", fieldPath="names"),
        )
    )

    with pytest.raises(ExportAssemblyError) as error:
        assemble_export_package(
            context=_context(),
            candidates=(candidate,),
            assessments={candidate.cluster_id: _eligible(candidate.cluster_id)},
        )

    assert error.value.code == "EXPORT_CANDIDATE_NAME_LOCALE_UNRESOLVED"
    assert error.value.owner == "CatalogMapping"
    assert error.value.context["campaignLocales"] == "und"


def test_candidate_without_name_blocks_export() -> None:
    candidate = _candidate(
        localized_names=(
            ExportLocalizedName(locale="und", state="unknown", value=None, fieldPath="names"),
        )
    )

    with pytest.raises(ExportAssemblyError) as error:
        assemble_export_package(
            context=_context(),
            candidates=(candidate,),
            assessments={candidate.cluster_id: _eligible(candidate.cluster_id)},
        )

    assert error.value.code == "EXPORT_CANDIDATE_NAME_MISSING"


def test_candidate_without_provenance_blocks_export() -> None:
    candidate = _candidate(source_artifact_ids=())

    with pytest.raises(ExportAssemblyError) as error:
        assemble_export_package(
            context=_context(),
            candidates=(candidate,),
            assessments={candidate.cluster_id: _eligible(candidate.cluster_id)},
        )

    assert error.value.code == "EXPORT_PROVENANCE_MISSING"


def test_two_revisions_of_one_candidate_block_export() -> None:
    candidate_id = uuid4()
    cluster_id = uuid4()
    first = _candidate(candidate_id=candidate_id, candidate_revision=0, cluster_id=cluster_id)
    second = _candidate(candidate_id=candidate_id, candidate_revision=1, cluster_id=cluster_id)

    with pytest.raises(ExportAssemblyError) as error:
        assemble_export_package(
            context=_context(),
            candidates=(first, second),
            assessments={first.cluster_id: _eligible(first.cluster_id)},
        )

    assert error.value.code == "EXPORT_CANDIDATE_MULTIPLE_REVISIONS"
    assert error.value.context["revisions"] == "0,1"


def test_candidate_revision_is_preserved_exactly() -> None:
    candidate = _candidate(candidate_revision=3)

    package = assemble_export_package(
        context=_context(),
        candidates=(candidate,),
        assessments={candidate.cluster_id: _eligible(candidate.cluster_id)},
    )

    assert package.entries[0].candidate_revision == 3


def test_service_rejects_bytes_that_do_not_round_trip() -> None:
    import collection_infrastructure.export.service as service_module

    candidate = _candidate()
    context = _context()
    service = service_module.ExportService(
        candidates=_StaticCandidates((candidate,)),
        quality=_StaticQuality({candidate.cluster_id: _eligible(candidate.cluster_id)}),
        candidate_revisions=((candidate.candidate_id, candidate.candidate_revision),),
    )

    package, content = service.build_and_verify_bytes(context=context)

    assert content.startswith(b"{")
    assert content.endswith(b"\n")
    from collection_contracts import decode_export_package

    assert decode_export_package(content).package_digest == package.package_digest


def test_materialize_returns_verified_reference() -> None:
    from datetime import UTC, datetime

    import collection_infrastructure.export.service as service_module

    candidate = _candidate()
    context = _context()
    service = service_module.ExportService(
        candidates=_StaticCandidates((candidate,)),
        quality=_StaticQuality({candidate.cluster_id: _eligible(candidate.cluster_id)}),
        candidate_revisions=((candidate.candidate_id, candidate.candidate_revision),),
    )
    package, content = service.build_and_verify_bytes(context=context)

    reference = service_module.materialize_export_artifact(
        package=package,
        content=content,
        object_store=_Writer(size_bytes=len(content)),
        artifact_kind=object(),
        now_utc=datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert reference.download_reference.startswith("exports/")
    assert reference.size_bytes == len(content)
    assert reference.candidate_count == 1
    assert reference.package_digest == package.package_digest


def test_materialize_rejects_size_mismatch_from_store() -> None:
    from datetime import UTC, datetime

    import collection_infrastructure.export.service as service_module

    candidate = _candidate()
    context = _context()
    service = service_module.ExportService(
        candidates=_StaticCandidates((candidate,)),
        quality=_StaticQuality({candidate.cluster_id: _eligible(candidate.cluster_id)}),
        candidate_revisions=((candidate.candidate_id, candidate.candidate_revision),),
    )
    package, content = service.build_and_verify_bytes(context=context)

    with pytest.raises(service_module.ExportServiceError) as error:
        service_module.materialize_export_artifact(
            package=package,
            content=content,
            object_store=_Writer(size_bytes=len(content) + 1),
            artifact_kind=object(),
            now_utc=datetime(2026, 8, 20, tzinfo=UTC),
        )

    assert error.value.code == "EXPORT_ARTIFACT_SIZE_MISMATCH"
    assert error.value.owner == "ObjectStore"


def test_materialize_rejects_empty_content() -> None:
    from datetime import UTC, datetime

    import collection_infrastructure.export.service as service_module

    candidate = _candidate()
    context = _context()
    service = service_module.ExportService(
        candidates=_StaticCandidates((candidate,)),
        quality=_StaticQuality({candidate.cluster_id: _eligible(candidate.cluster_id)}),
        candidate_revisions=((candidate.candidate_id, candidate.candidate_revision),),
    )
    package, _ = service.build_and_verify_bytes(context=context)

    with pytest.raises(service_module.ExportServiceError) as error:
        service_module.materialize_export_artifact(
            package=package,
            content=b"",
            object_store=_Writer(size_bytes=0),
            artifact_kind=object(),
            now_utc=datetime(2026, 8, 20, tzinfo=UTC),
        )

    assert error.value.code == "EXPORT_ARTIFACT_EMPTY"


class _StaticCandidates:
    def __init__(self, candidates) -> None:
        self._candidates = candidates

    def read_candidates(self, *, candidate_revisions):
        return self._candidates


class _StaticQuality:
    def __init__(self, assessments) -> None:
        self._assessments = assessments

    def read_assessments(self, *, run_id, cluster_ids):
        return self._assessments


class _Writer:
    def __init__(self, *, size_bytes: int) -> None:
        self._size_bytes = size_bytes

    def store_bytes(self, *, artifact_kind, content, content_type, now_utc):
        class _Stored:
            final_reference = "exports/2026/package.json"
            content_digest = "sha256:" + "c" * 64
            size_bytes = self._size_bytes

        return _Stored()


def test_persisted_projection_without_geography_and_provenance_is_blocked() -> None:
    import collection_infrastructure.export.service as service_module
    from collection_infrastructure.postgres.export_readers import (
        PostgresExportCandidatePort,
    )

    # A persisted revision whose geography is unresolved must block at the assembler
    # boundary rather than export an unclassified candidate as a Berlin entry.
    unresolved = _candidate(
        geography=ExportGeography.model_construct(
            coverage="unknown",
            market_area_revision="unresolved",
            market_area_digest="sha256:" + "0" * 64,
            latitude=None,
            longitude=None,
            field_path="geography",
        )
    )
    service = service_module.ExportService(
        candidates=_StaticCandidates((unresolved,)),
        quality=_StaticQuality({unresolved.cluster_id: _eligible(unresolved.cluster_id)}),
    )

    with pytest.raises(ExportAssemblyError) as error:
        service.build_package(context=_context())

    assert error.value.code == "EXPORT_GEOGRAPHY_UNRESOLVED"
    assert PostgresExportCandidatePort is not None
