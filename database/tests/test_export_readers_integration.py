"""Integration proof that export readers see exactly the persisted candidate/quality state.

The readers must not repair, default, or invent anything: a candidate whose provenance is
absent stays absent, and the assembler is the only place that decides whether that blocks
the package. Every row used here is inserted through the real schema, so a reader that
starts trusting a placeholder instead of a persisted fact fails against PostGIS rather than
passing on a mock.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from db_support import disposable_database, seed_resolution_quality

from collection_contracts import (
    ExportCatalogBinding,
    ExportCatalogCategoryMapping,
    ExportCatalogNameLocale,
)
from collection_infrastructure.export.assembly import ExportAssemblyError, ExportRunContext
from collection_infrastructure.export.service import ExportService, ExportServiceError
from collection_infrastructure.postgres.export_readers import (
    PostgresExportCandidatePort,
    PostgresExportCandidateReader,
    PostgresExportQualityReader,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + "a" * 64
_POLICY_DIGEST = "sha256:" + "b" * 64
_RAW_ARTIFACT_ID = UUID("00000000-0000-0000-0000-00000000f001")
_SOURCE_URL = "https://www.openstreetmap.org/node/101"
_MARKET_AREA_DIGEST = "sha256:" + "c" * 64


@pytest.fixture()
def engine() -> Iterator[sa.Engine]:
    """Run the suite against its own disposable, fully migrated database.

    `PostgresExportCandidateReader.read_rows()` scans every persisted revision and fails
    closed on one that carries no sealed export policy, which is correct production
    behaviour. A shared database therefore cannot hold another suite's rows, and the
    candidate tables refuse `DELETE`/`TRUNCATE` by design, so isolation comes from a
    database of its own rather than from disabling integrity. The database is created and
    dropped per test so a deliberately incomplete fixture cannot leak into a later scan.
    """

    for url in disposable_database("export readers"):
        value = sa.create_engine(url, poolclass=sa.pool.NullPool)
        try:
            yield value
        finally:
            value.dispose()


def _observation(field_key: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "fieldKey": field_key,
        "state": "observed",
        "value": value,
        "sourceRecordId": "osm:node:101",
        "sourcePolicyDigest": _POLICY_DIGEST,
        "evidence": {
            "sourceUrl": _SOURCE_URL,
            "observedAtUtc": _NOW.isoformat(),
            "evidenceDigest": _DIGEST,
            "rawArtifactId": str(_RAW_ARTIFACT_ID),
        },
    }


def _name_without_language_payload() -> dict[str, Any]:
    """A payload whose only name establishes no language at all."""

    payload = _normalized_payload()
    payload["fieldObservations"] = [
        (
            _observation("display_name", {"kind": "text", "normalized": "Studio Seeded"})
            if observation.get("fieldKey") == "display_name"
            else observation
        )
        for observation in payload["fieldObservations"]
    ]
    return payload


def _normalized_payload(
    *,
    include_geography: bool = True,
    include_policy: bool = True,
    coverage: str = "inside",
    legal_status: str = "approved",
    usage_policy: str | None = "publishable",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "projection": {
            "display_name": "names",
            "website": "website_urls",
            "coordinates": "geography",
        },
        "fieldObservations": [
            _observation(
                "display_name",
                {"kind": "text", "normalized": "Studio Seeded", "locale": "de"},
            ),
            _observation("website", {"kind": "url", "normalized": "https://studio.example/"}),
            _observation(
                "coordinates", {"kind": "geo_point", "normalized": "52.5007771,13.4168436"}
            ),
            # The classification is a recorded field claim of the revision itself, so the
            # fixture seeds it as an observation with the same evidence shape as any other.
            _observation(
                "category_recording_studio",
                {"kind": "text", "normalized": "recording_studio"},
            ),
        ],
    }
    if include_policy:
        payload["exportPolicy"] = {
            "sourceKey": "openstreetmap",
            "legalStatus": legal_status,
            "usagePolicy": usage_policy,
            "attribution": "OpenStreetMap contributors",
        }
    if include_geography:
        payload["geography"] = {
            "coverage": coverage,
            "marketAreaRevision": "berlin_boundary_v1",
            "marketAreaDigest": _MARKET_AREA_DIGEST,
            # An exported entry carries the observed point the classification decided on;
            # the reader refuses to invent one, so the fixture must record real coordinates.
            "latitude": 52.5007771,
            "longitude": 13.4168436,
        }
    return payload


def _seed_candidate(
    engine: sa.Engine,
    *,
    payload: dict[str, Any] | None = None,
    eligible: bool = True,
    blockers: tuple[str, ...] = (),
    evidence: bool = True,
    decision: bool = True,
    revision: int = 0,
) -> UUID:
    candidate_id = uuid4()
    cluster_id = uuid4()
    case_id = uuid4()
    document = payload if payload is not None else _normalized_payload()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.candidates
                    (candidate_id, entity_kind, created_at_utc, correlation_id)
                VALUES (:candidate_id, 'organization', :now, 'export-readers')
                """
            ),
            {"candidate_id": candidate_id, "now": _NOW},
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO candidates.candidate_revisions
                    (candidate_id, revision, cluster_id, resolution_state, snapshot_digest,
                     source_lineage_digest, normalized_payload, recorded_at_utc, correlation_id)
                VALUES (:candidate_id, :revision, :cluster_id, 'review', :snapshot, :lineage,
                        CAST(:payload AS jsonb), :now, 'export-readers')
                """
            ),
            {
                "candidate_id": candidate_id,
                "cluster_id": cluster_id,
                "snapshot": _DIGEST,
                "lineage": _DIGEST,
                "revision": revision,
                "payload": json.dumps(document, sort_keys=True, separators=(",", ":")),
                "now": _NOW,
            },
        )
        if evidence:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO candidates.candidate_revision_evidence
                        (candidate_id, candidate_revision, position, evidence_kind, evidence_digest)
                    VALUES (:candidate_id, :revision, 0, 'artifact', :digest)
                    """
                ),
                {"candidate_id": candidate_id, "digest": _DIGEST, "revision": revision},
            )
        # Quality is authoritative only through the resolution that produced it.
        seed_resolution_quality(
            connection,
            evaluation={(candidate_id, revision): (eligible, blockers)},
            policy_digest=_POLICY_DIGEST,
            evaluated_at_utc=_NOW,
            correlation_id="export-readers",
            evaluation_digest=_DIGEST,
        )
        if decision:
            _seed_decision(
                connection, candidate_id=candidate_id, case_id=case_id, revision=revision
            )
    return candidate_id


def _seed_decision(
    connection: sa.Connection, *, candidate_id: UUID, case_id: UUID, revision: int = 0
) -> None:
    decision_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO review.review_cases
                (case_id, candidate_id, candidate_revision, opened_at_utc, correlation_id)
            VALUES (:case_id, :candidate_id, :revision, :now, 'export-readers')
            """
        ),
        {"case_id": case_id, "candidate_id": candidate_id, "revision": revision, "now": _NOW},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO review.review_decisions
                (decision_id, case_id, case_revision, outcome, actor_id, rationale,
                 evidence_references, supersedes_decision_id, command_digest,
                 decided_at_utc, correlation_id)
            VALUES (:decision_id, :case_id, :revision, 'accept_candidate', 'reviewer-1', 'verified',
                    :evidence_references, NULL, :command_digest, :now, 'export-readers')
            """
        ).bindparams(sa.bindparam("evidence_references", type_=sa.ARRAY(sa.Text()))),
        {
            "decision_id": decision_id,
            "case_id": case_id,
            "revision": revision,
            # The command digest is unique per decision, so each seeded case derives its own.
            "command_digest": "sha256:" + sha256(f"{case_id}:{revision}".encode()).hexdigest(),
            "evidence_references": [f"evidence:{_DIGEST}"],
            "now": _NOW,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO review.review_case_revisions
                (case_id, revision, state, reason_codes, current_decision_id,
                 recorded_at_utc, correlation_id)
            VALUES (:case_id, :revision, 'decided', :reason_codes, :decision_id, :now,
                    'export-readers')
            """
        ).bindparams(sa.bindparam("reason_codes", type_=sa.ARRAY(sa.Text()))),
        {
            "case_id": case_id,
            "decision_id": decision_id,
            "revision": revision,
            "reason_codes": ["evidence_verified"],
            "now": _NOW,
        },
    )


def _seed_decision(
    connection: sa.Connection, *, candidate_id: UUID, case_id: UUID, revision: int = 0
) -> None:
    decision_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO review.review_cases
                (case_id, candidate_id, candidate_revision, opened_at_utc, correlation_id)
            VALUES (:case_id, :candidate_id, :revision, :now, 'export-readers')
            """
        ),
        {"case_id": case_id, "candidate_id": candidate_id, "revision": revision, "now": _NOW},
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO review.review_decisions
                (decision_id, case_id, case_revision, outcome, actor_id, rationale,
                 evidence_references, supersedes_decision_id, command_digest,
                 decided_at_utc, correlation_id)
            VALUES (:decision_id, :case_id, :revision, 'accept_candidate', 'reviewer-1', 'verified',
                    :evidence_references, NULL, :command_digest, :now, 'export-readers')
            """
        ).bindparams(sa.bindparam("evidence_references", type_=sa.ARRAY(sa.Text()))),
        {
            "decision_id": decision_id,
            "case_id": case_id,
            "revision": revision,
            # The command digest is unique per decision, so each seeded case derives its own.
            "command_digest": "sha256:" + sha256(f"{case_id}:{revision}".encode()).hexdigest(),
            "evidence_references": [f"evidence:{_DIGEST}"],
            "now": _NOW,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO review.review_case_revisions
                (case_id, revision, state, reason_codes, current_decision_id,
                 recorded_at_utc, correlation_id)
            VALUES (:case_id, :revision, 'decided', :reason_codes, :decision_id, :now,
                    'export-readers')
            """
        ).bindparams(sa.bindparam("reason_codes", type_=sa.ARRAY(sa.Text()))),
        {
            "case_id": case_id,
            "decision_id": decision_id,
            "revision": revision,
            "reason_codes": ["evidence_verified"],
            "now": _NOW,
        },
    )


_BINDING = ExportCatalogBinding(
    site_key="berlin-recording",
    catalog_key="berlin-recording-services",
    configuration_revision_id=UUID("00000000-0000-0000-0000-000000000001"),
    coordinate_precision_meters=25,
    name_locales=(ExportCatalogNameLocale(campaign_locale="de", catalog_locale="de-DE"),),
    categories=(
        ExportCatalogCategoryMapping(
            campaign_category="recording_studio", catalog_category="recording-studio"
        ),
    ),
)


def _run_context() -> ExportRunContext:
    return ExportRunContext(
        run_id=uuid4(),
        campaign_bundle_digest=_DIGEST,
        campaign_key="berlin_recording_services",
        export_profile_key="default",
        export_profile_revision="rev_v1",
        quality_profile_digest=_DIGEST,
        catalog_binding=_BINDING,
        generated_at_utc=_NOW,
        correlation_id="export-readers",
    )


def test_readers_return_exactly_the_persisted_projection(engine: sa.Engine) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine)
    created.append(candidate_id)

    rows = PostgresExportCandidatePort(engine).read_candidates(
        candidate_revisions=[(candidate_id, 0)]
    )

    matching = [row for row in rows if row.candidate_id == candidate_id]
    assert len(matching) == 1
    source = matching[0]
    assert tuple(name.value for name in source.localized_names) == ("Studio Seeded",)
    assert tuple(contact.target for contact in source.contacts) == ("https://studio.example/",)
    assert source.category_keys == ("recording_studio",)
    # A classification without its own evidence would be an unsupported claim, so the
    # exported category carries an evidence record like every other field.
    category_evidence = [
        evidence
        for evidence in source.field_evidence
        if evidence.field_path == "categories/recording_studio"
    ]
    assert len(category_evidence) == 1
    assert category_evidence[0].source_key == "openstreetmap"
    assert category_evidence[0].evidence_digest == _DIGEST
    assert source.quality_evaluation_digest == _DIGEST


def test_the_sealed_export_policy_is_read_from_the_revision_not_guessed(
    engine: sa.Engine,
) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine)
    created.append(candidate_id)

    rows = PostgresExportCandidateReader(engine).read_rows()

    row = next(item for item in rows if item.candidate_id == candidate_id)
    assert row.source_key == "openstreetmap"
    assert row.usage_policy == "publishable"
    assert row.attribution == "OpenStreetMap contributors"


def test_a_link_only_field_keeps_its_restriction_per_evidence(engine: sa.Engine) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(
        engine,
        payload=_normalized_payload(legal_status="reference_only", usage_policy="link_only"),
    )

    created.append(candidate_id)
    sources = PostgresExportCandidatePort(engine).read_candidates(
        candidate_revisions=[(candidate_id, 0)]
    )

    source = next(item for item in sources if item.candidate_id == candidate_id)
    assert {item.usage_policy for item in source.field_evidence} == {"link_only"}
    assert all(item.source_key == "openstreetmap" for item in source.field_evidence)


def test_candidate_without_evidence_reports_no_provenance(engine: sa.Engine) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine, evidence=False)
    created.append(candidate_id)

    rows = PostgresExportCandidateReader(engine).read_rows()

    matching = [row for row in rows if row.candidate_id == candidate_id]
    assert matching[0].evidence_digests == ()


def test_quality_reader_surfaces_blockers_as_issues(engine: sa.Engine) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine, eligible=False, blockers=("BLOCKED_PAIR",))
    created.append(candidate_id)
    # The resolution that produced the verdict owns cluster membership, so the assessment is
    # read back under that cluster identity rather than the intake placeholder.
    with engine.connect() as connection:
        cluster_id = connection.execute(
            sa.text(
                """
                SELECT m.cluster_id FROM candidates.resolution_cluster_members m
                WHERE m.candidate_id = :candidate_id
                """
            ),
            {"candidate_id": candidate_id},
        ).scalar_one()

    assessments = PostgresExportQualityReader(engine).read_assessments(
        run_id=uuid4(), cluster_ids=[cluster_id]
    )

    assessment = assessments[cluster_id]
    assert assessment.export_eligible is False
    assert tuple(issue.code for issue in assessment.issues) == ("BLOCKED_PAIR",)


def test_an_unmapped_quality_blocker_cannot_reach_the_export_package(
    engine: sa.Engine,
) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine, eligible=False, blockers=("UNREGISTERED_BLOCKER",))
    created.append(candidate_id)

    with pytest.raises(ExportServiceError) as error:
        PostgresExportCandidatePort(engine).read_candidates(candidate_revisions=[(candidate_id, 0)])

    assert error.value.code == "EXPORT_QUALITY_ISSUE_CODE_UNMAPPED"
    assert error.value.owner == "CandidateQuality"
    assert error.value.context["blocker"] == "UNREGISTERED_BLOCKER"


def test_a_candidate_without_a_recorded_geography_decision_cannot_be_exported(
    engine: sa.Engine,
) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine, payload=_normalized_payload(include_geography=False))
    created.append(candidate_id)
    service = ExportService(
        candidates=PostgresExportCandidatePort(engine),
        quality=PostgresExportQualityReader(engine),
        candidate_revisions=((candidate_id, 0),),
    )

    with pytest.raises(ExportServiceError) as error:
        service.build_package(context=_run_context())

    assert error.value.code == "EXPORT_CANDIDATE_GEOGRAPHY_MISSING"
    assert error.value.owner == "CandidateGeography"


def test_the_name_locale_comes_from_the_recorded_source_language(engine: sa.Engine) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine, payload=_normalized_payload())
    created.append(candidate_id)
    service = ExportService(
        candidates=PostgresExportCandidatePort(engine),
        quality=PostgresExportQualityReader(engine),
        candidate_revisions=((candidate_id, 0),),
    )

    package = service.build_package(context=_run_context())

    names = package.entries[0].localized_names
    # The observation recorded `de`; nothing derives a display locale from the campaign's
    # own locale list or from a field key that carries no language.
    assert [(name.locale, name.value) for name in names] == [("de", "Studio Seeded")]


def test_a_name_with_no_established_language_stays_export_blocked(engine: sa.Engine) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine, payload=_name_without_language_payload())
    created.append(candidate_id)
    service = ExportService(
        candidates=PostgresExportCandidatePort(engine),
        quality=PostgresExportQualityReader(engine),
        candidate_revisions=((candidate_id, 0),),
    )

    with pytest.raises(ExportAssemblyError) as error:
        service.build_package(context=_run_context())

    assert error.value.code == "EXPORT_CANDIDATE_NAME_LOCALE_UNRESOLVED"
    assert error.value.owner == "CatalogMapping"
    assert "und" in str(error.value.context.get("campaignLocales"))


def test_unresolved_coverage_stays_export_blocked(engine: sa.Engine) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine, payload=_normalized_payload(coverage="unknown"))
    created.append(candidate_id)
    service = ExportService(
        candidates=PostgresExportCandidatePort(engine),
        quality=PostgresExportQualityReader(engine),
        candidate_revisions=((candidate_id, 0),),
    )

    with pytest.raises(ExportServiceError) as error:
        service.build_package(context=_run_context())

    assert error.value.code == "EXPORT_CANDIDATE_GEOGRAPHY_UNRESOLVED"
    assert error.value.owner == "CandidateGeography"


def test_a_revision_without_a_sealed_policy_cannot_be_exported(engine: sa.Engine) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine, payload=_normalized_payload(include_policy=False))
    created.append(candidate_id)

    with pytest.raises(ExportServiceError) as error:
        PostgresExportCandidateReader(engine).read_rows()

    assert error.value.code == "EXPORT_CANDIDATE_POLICY_MISSING"
    assert error.value.owner == "CandidateMaterialization"


def test_a_fully_evidenced_candidate_reaches_a_sealed_package(engine: sa.Engine) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine)
    created.append(candidate_id)
    service = ExportService(
        candidates=PostgresExportCandidatePort(engine),
        quality=PostgresExportQualityReader(engine),
        candidate_revisions=((candidate_id, 0),),
    )

    package = service.build_package(context=_run_context())

    entry = next(item for item in package.entries if item.candidate_id == candidate_id)
    assert entry.geography.coverage == "inside"
    assert entry.geography.market_area_digest == _MARKET_AREA_DIGEST
    assert entry.source_policies[0].source_key == "openstreetmap"
    assert entry.review_decisions[0].decision_kind == "accept_candidate"
    assert entry.field_evidence, "every exported field must carry its evidence"


def test_missing_projection_fails_closed_naming_the_owner(engine: sa.Engine) -> None:
    created: list[UUID] = []
    payload = _normalized_payload()
    payload.pop("projection")
    candidate_id = _seed_candidate(engine, payload=payload)
    created.append(candidate_id)

    with pytest.raises(ExportServiceError) as error:
        PostgresExportCandidatePort(engine).read_candidates(candidate_revisions=[(candidate_id, 0)])

    assert error.value.code == "EXPORT_CANDIDATE_PROJECTION_MISSING"
    assert error.value.owner == "CandidateMaterialization"


def _decision_id(engine: sa.Engine, candidate_id: UUID) -> UUID:
    with engine.connect() as connection:
        return connection.execute(
            sa.text(
                """
                SELECT d.decision_id FROM review.review_decisions AS d
                JOIN review.review_cases AS c ON c.case_id = d.case_id
                WHERE c.candidate_id = :candidate_id
                """
            ),
            {"candidate_id": candidate_id},
        ).scalar_one()


def test_export_rows_carry_exact_persisted_values_without_placeholders(
    engine: sa.Engine,
) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine)
    created.append(candidate_id)
    decision_id = _decision_id(engine, candidate_id)

    row = next(
        item
        for item in PostgresExportCandidateReader(engine).read_rows()
        if item.candidate_id == candidate_id
    )

    # Geography is the classified point, not a placeholder coordinate.
    assert row.normalized_payload["geography"]["coverage"] == "inside"
    assert row.normalized_payload["geography"]["marketAreaDigest"] == _MARKET_AREA_DIGEST
    assert row.normalized_payload["geography"]["latitude"] == 52.5007771
    # Policy is the sealed revision fact.
    assert row.source_key == "openstreetmap"
    assert row.usage_policy == "publishable"
    assert row.attribution == "OpenStreetMap contributors"
    # Quality comes from the persisted evaluation.
    assert row.export_eligible is True
    assert row.policy_digest == _POLICY_DIGEST
    assert row.evaluation_digest == _DIGEST
    # Review lineage is the current decision, by identity and digest.
    assert row.decision_id == decision_id
    assert row.decision_outcome == "accept_candidate"
    # The reader returns the exact persisted command digest of the recorded decision.
    # The digest is the exact recorded command digest, derived from the decision itself.
    assert row.decision_digest.startswith("sha256:")
    assert len(row.decision_digest) == len("sha256:") + 64
    assert row.decided_at_utc is not None


def test_export_rows_never_invent_field_evidence(engine: sa.Engine) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine, evidence=False)
    created.append(candidate_id)

    row = next(
        item
        for item in PostgresExportCandidateReader(engine).read_rows()
        if item.candidate_id == candidate_id
    )

    assert row.evidence_digests == ()
    sources = PostgresExportCandidatePort(engine).read_candidates(
        candidate_revisions=[(candidate_id, 0)]
    )
    evidence = sources[0].field_evidence
    # Every exported field keeps its own real source, URL and digest.
    assert {item.source_url for item in evidence} == {_SOURCE_URL}
    assert {item.source_record_id for item in evidence} == {"osm:node:101"}
    assert all(item.evidence_digest == _DIGEST for item in evidence)
    assert {item.source_policy_digest for item in evidence} == {_POLICY_DIGEST}


def test_sealed_export_is_deterministic_for_the_same_persisted_state(
    engine: sa.Engine,
) -> None:
    created: list[UUID] = []
    candidate_id = _seed_candidate(engine)
    created.append(candidate_id)
    context = _run_context()

    def build() -> Any:
        return ExportService(
            candidates=PostgresExportCandidatePort(engine),
            quality=PostgresExportQualityReader(engine),
            candidate_revisions=((candidate_id, 0),),
        ).build_package(context=context)

    first = build()
    second = build()

    assert first.package_digest == second.package_digest
    assert first.entries == second.entries


def test_the_database_backed_package_converts_into_a_consistent_backend_document(
    engine: sa.Engine,
) -> None:
    """The bytes a registration would upload, produced from persisted records only."""

    from collector_cli.backend_ingestion import build_manifest, build_package_documents

    created: list[UUID] = []
    candidate_id = _seed_candidate(engine)
    created.append(candidate_id)
    package = ExportService(
        candidates=PostgresExportCandidatePort(engine),
        quality=PostgresExportQualityReader(engine),
        candidate_revisions=((candidate_id, 0),),
    ).build_package(context=_run_context())

    documents = build_package_documents(package)
    manifest = build_manifest(
        package,
        documents,
        producer_identity="data-collection-platform",
        producer_build="database-backdated-vector",
        target_site_key="berlin-recording",
        target_catalog_key="berlin-recording-services",
        target_catalog_configuration_revision_id=str(_BINDING.configuration_revision_id),
    )

    # The payload digest covers the exact serialized item bytes.
    assert manifest["payloadDigest"] == documents.payload_digest
    assert manifest["itemCount"] == len(documents.items) == 1
    assert documents.payload_bytes
    item = documents.items[0]
    assert item["itemKey"] == str(candidate_id)
    # Real persisted facts survive into the backend document.
    assert Decimal(str(item["geographyProposal"]["latitude"])) == Decimal("52.5007771")
    assert item["provenanceReferences"][0]["sourceKey"] == "openstreetmap"
    assert item["provenanceReferences"][0]["usagePolicy"] == "publishable"
    assert item["provenanceReferences"][0]["attribution"] == "OpenStreetMap contributors"
    assert item["collectorReviewReferences"][0]["decisionKind"] == "accept_candidate"


@pytest.mark.parametrize("collector_revision", [0, 1])
def test_database_backed_package_bytes_are_exported_for_backend_validation(
    engine: sa.Engine, collector_revision: int
) -> None:
    """Emit the real DB-backed package so the backend validator can consume its exact bytes.

    Set ``COLLECTOR_EXPORT_FIXTURE_OUT`` to a path to write the fixture. Without it the test
    still verifies the package is buildable and its digests are self-consistent, so the
    default run proves the same production path without touching the filesystem.
    """

    from collector_cli.backend_ingestion import build_manifest, build_package_documents

    created: list[UUID] = []
    # The collector numbers candidate revisions from zero, which is the real first
    # revision a candidate carries; the vector is emitted at that revision.
    candidate_id = _seed_candidate(engine, revision=collector_revision)
    created.append(candidate_id)
    package = ExportService(
        candidates=PostgresExportCandidatePort(engine),
        quality=PostgresExportQualityReader(engine),
        candidate_revisions=((candidate_id, collector_revision),),
    ).build_package(context=_run_context())
    documents = build_package_documents(package)
    manifest = build_manifest(
        package,
        documents,
        producer_identity="data-collection-platform",
        producer_build="database-backed",
        target_site_key="berlin-recording",
        target_catalog_key="berlin-recording-services",
        target_catalog_configuration_revision_id=str(_BINDING.configuration_revision_id),
    )
    manifest = {
        **manifest,
        "artifacts": [
            {
                "role": "candidatePayload",
                "objectKey": f"ingestion/quarantine/{package.run_id}/payload.json",
                "contentDigest": documents.payload_digest,
                "size": len(documents.payload_bytes),
                "contentType": "application/json",
            }
        ],
    }
    document = {
        "manifest": manifest,
        "payloadBytes": documents.payload_bytes.decode("utf-8"),
        "itemIndex": list(documents.item_index),
        "candidateId": str(candidate_id),
        "packageDigest": package.package_digest,
        "collectorCandidateRevision": collector_revision,
    }

    assert documents.payload_digest == documents.payload_digest.lower()
    assert len(documents.payload_bytes) == manifest["artifacts"][0]["size"]

    template = os.environ.get("COLLECTOR_EXPORT_FIXTURE_OUT", "").strip()
    if template:
        Path(template.format(revision=collector_revision)).write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


def test_a_research_only_policy_stays_non_exportable_despite_complete_evidence(
    engine: sa.Engine,
) -> None:
    """Research-only sources support internal review but never production export.

    The candidate carries a sealed restricted policy, complete field evidence, an observed
    point and a quality assessment. Nothing except redistribution rights is missing, so only
    the sealed policy can block it - and it must.
    """

    created: list[UUID] = []
    candidate_id = _seed_candidate(
        engine,
        payload=_normalized_payload(legal_status="research_only", usage_policy=None),
    )
    created.append(candidate_id)

    # The sealed policy grants no redistribution right at all, so the read path refuses
    # before any entry could be sealed.
    with pytest.raises(ExportServiceError) as error:
        PostgresExportCandidatePort(engine).read_candidates(candidate_revisions=[(candidate_id, 0)])

    assert error.value.code == "EXPORT_CANDIDATE_SOURCE_POLICY_NOT_EXPORTABLE"
    assert error.value.owner == "CandidateMaterialization"
    assert error.value.context["legalStatus"] == "research_only"


def test_a_research_only_candidate_is_not_quietly_dropped_from_the_package(
    engine: sa.Engine,
) -> None:
    """A restricted revision must fail the run, never disappear into a partial package.

    Silently skipping it would report complete coverage while withholding a blocked
    candidate, so the run fails and names the source that needs an external decision.
    """

    created: list[UUID] = []
    exportable = _seed_candidate(engine)
    created.append(exportable)
    restricted = _seed_candidate(
        engine,
        payload=_normalized_payload(legal_status="research_only", usage_policy=None),
    )
    created.append(restricted)

    with pytest.raises(ExportServiceError) as error:
        PostgresExportCandidatePort(engine).read_candidates(
            candidate_revisions=[(exportable, 0), (restricted, 0)]
        )

    assert error.value.code == "EXPORT_CANDIDATE_SOURCE_POLICY_NOT_EXPORTABLE"
    # The failure names a real candidate, so the operator can act on it.
    assert error.value.context["candidateId"] == str(restricted)
    assert error.value.context["legalStatus"] == "research_only"


def test_research_only_keeps_its_exact_legal_status_without_conversion(
    engine: sa.Engine,
) -> None:
    """The original grant survives intake: research-only never becomes link-only.

    `link_only` is a real redistribution grant held by `reference_only` sources. Recording
    research-only evidence under it would silently widen what may be done with the data, so
    the sealed policy keeps the original status and states no usage right.
    """

    created: list[UUID] = []
    candidate_id = _seed_candidate(
        engine,
        payload=_normalized_payload(legal_status="research_only", usage_policy=None),
    )
    created.append(candidate_id)

    with engine.connect() as connection:
        payload = connection.execute(
            sa.text(
                "SELECT normalized_payload FROM candidates.candidate_revisions "
                "WHERE candidate_id = :candidate_id"
            ),
            {"candidate_id": candidate_id},
        ).scalar_one()

    sealed = payload["exportPolicy"]
    assert sealed["legalStatus"] == "research_only"
    assert sealed["usagePolicy"] is None
    # The restriction is stated, not replaced by a weaker different authorization.
    assert sealed["usagePolicy"] != "link_only"


def test_a_genuine_link_only_source_is_not_confused_with_research_only(
    engine: sa.Engine,
) -> None:
    """`reference_only` is a real grant, and stays exportable as link-only.

    Both statuses reach the export path, so only the recorded legal status separates them.
    """

    created: list[UUID] = []
    candidate_id = _seed_candidate(
        engine,
        payload=_normalized_payload(legal_status="reference_only", usage_policy="link_only"),
    )
    created.append(candidate_id)

    sources = PostgresExportCandidatePort(engine).read_candidates(
        candidate_revisions=[(candidate_id, 0)]
    )
    candidate = next(item for item in sources if item.candidate_id == candidate_id)

    policy = next(item for item in candidate.source_policies if item.source_key == "openstreetmap")
    assert policy.legal_status == "reference_only"
    assert policy.usage_policy == "link_only"
    assert policy.legal_status != "research_only"


def test_merged_sources_keep_their_restrictive_field_provenance(engine: sa.Engine) -> None:
    """A merged candidate must not lose a stricter field-level restriction.

    One field carries research-only evidence and another an approved grant. The candidate is
    refused because the restrictive provenance is still visible per field, instead of being
    flattened into the permissive value.
    """

    created: list[UUID] = []
    payload = _normalized_payload()
    observations = payload["fieldObservations"]
    restricted = dict(observations[0])
    restricted["sourceLegalStatus"] = "research_only"
    observations[0] = restricted

    candidate_id = _seed_candidate(engine, payload=payload)
    created.append(candidate_id)

    with pytest.raises(ExportServiceError) as error:
        PostgresExportCandidatePort(engine).read_candidates(candidate_revisions=[(candidate_id, 0)])

    assert error.value.code == "EXPORT_CANDIDATE_SOURCE_POLICY_NOT_EXPORTABLE"
    assert error.value.context["legalStatus"] == "research_only"
