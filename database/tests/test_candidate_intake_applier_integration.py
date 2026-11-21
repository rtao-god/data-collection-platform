from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from db_support import database_url
from sqlalchemy.pool import NullPool

from collection_application import CampaignSnapshotService, RawCampaignBundle
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    CandidateIntakeAction,
    ManualRecordIntakeProvenance,
    PipelineAdvancementConflict,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    PipelineTransitionRegistry,
    SucceededWorkOutput,
)
from collection_contracts import (
    EvidenceReference,
    FieldObservationPayload,
    ManualRecordLineage,
    NormalizationFieldRule,
    NormalizationProfile,
    ObservationBatchPayload,
    ObservationState,
    TextObservationValue,
    canonical_manual_record_lineage_bytes,
    canonical_observation_batch_json,
    decode_observation_batch,
    seal_field_observation,
    seal_observation_batch,
)
from collection_domain import WorkStage
from collection_infrastructure.postgres.candidate_intake_applier import (
    PostgresCandidateIntakeApplier,
)
from resolution_contracts import EntityKind

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _sha(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _artifact(artifact_id: UUID, role: str, digest: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_id=artifact_id,
        role=role,
        content_digest=digest,
        size_bytes=64,
        content_type="application/json",
    )


def _seed_acquired_document(connection: sa.Connection, digest: str) -> None:
    """Record the acquired document an extracted observation proves itself against.

    A page-derived observation seals the content digest of the document it was read from, and
    the candidate revision must carry the matching artifact identity, so the intake needs the
    artifact the extraction actually consumed.
    """

    object_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO sources.artifact_objects
                (object_id, artifact_kind, content_digest, size_bytes, storage_reference,
                 verified_at_utc, recorded_at_utc, correlation_id)
            VALUES (:object_id, 'raw_artifact', :digest, 1, :storage_reference,
                    :now, :now, 'candidate-intake-applier')
            ON CONFLICT (artifact_kind, content_digest) DO NOTHING
            """
        ),
        {
            "object_id": object_id,
            "digest": digest,
            "storage_reference": f"artifacts/{object_id}.html",
            "now": _NOW,
        },
    )


def _raw_digest(source: SucceededWorkOutput) -> str:
    assert source.output_content is not None
    return decode_observation_batch(source.output_content).raw_artifact_digest


def _batch_bytes(record_digest: str) -> bytes:
    observation = seal_field_observation(
        FieldObservationPayload(
            sourceRecordId=record_digest,
            fieldKey="names",
            state=ObservationState.OBSERVED,
            value=TextObservationValue(original="Studio", normalized="Studio"),
            evidence=EvidenceReference(
                rawArtifactDigest="sha256:" + "1" * 64,
                sourceUrl="https://example.test/studio",
                locatorKind="json_pointer",
                locatorValue="/records/1",
                evidenceDigest="sha256:" + "2" * 64,
                observedAtUtc=_NOW,
                extractorRevision="manual-v1",
            ),
            sourcePolicyDigest="sha256:" + "3" * 64,
            normalizerRevision="manual-v1",
            confidence=1,
        )
    )
    return canonical_observation_batch_json(
        seal_observation_batch(
            ObservationBatchPayload(
                sourceRecordId=record_digest,
                extractedRecordDigest="sha256:" + "4" * 64,
                rawArtifactDigest="sha256:" + "1" * 64,
                sourcePolicyDigest="sha256:" + "3" * 64,
                normalizerRevision="manual-v1",
                observations=(observation,),
                issues=(),
            )
        )
    ).encode("utf-8")


class _CampaignFiles:
    """Read the shipped campaign definition from disk for the exact run snapshot."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = MappingProxyType(files)

    def read(self, campaign_key: str, correlation_id: str) -> RawCampaignBundle:
        del correlation_id
        return RawCampaignBundle(campaign_key=campaign_key, files=self._files)


def _compiled_campaign() -> bytes:
    """Compile the shipped campaign so candidate geography has a declared market area.

    Candidate intake fails closed without the exact run snapshot, because the export gate may
    not receive an unbacked market area.
    """

    campaign_root = Path(__file__).resolve().parents[2] / "campaigns" / "berlin_recording_services"
    files = {
        path.relative_to(campaign_root).as_posix(): path.read_bytes()
        for path in campaign_root.rglob("*")
        if path.is_file()
    }
    return (
        CampaignSnapshotService(_CampaignFiles(files))
        .compile(
            "berlin_recording_services",
            "correlation-candidate-intake-applier",
        )
        .canonical_content
    )


def _normalization_profile_bytes() -> bytes:
    """The exact normalization profile this fixture's batch was produced under."""

    return (
        NormalizationProfile(
            normalizerRevision="manual-v1",
            fieldRules=(
                NormalizationFieldRule(sourceField="names", targetField="names", valueKind="text"),
            ),
            booleanPatternRules=(),
        )
        .model_dump_json(by_alias=True)
        .encode("utf-8")
    )


def _sources() -> tuple[SucceededWorkOutput, bytes, ManualRecordLineage]:
    record_digest = "sha256:" + uuid4().hex.ljust(64, "0")
    batch_bytes = _batch_bytes(record_digest)
    lineage = ManualRecordLineage(
        runId=uuid4(),
        manualRecordWorkId=uuid4(),
        manualRecordArtifactId=uuid4(),
        manualRecordArtifactDigest="sha256:" + "5" * 64,
        recordDigest="sha256:" + "6" * 64,
        expectedEntityKind="organization",
        manualRecordContentDigest=record_digest,
        sourceArtifactId=uuid4(),
        sourceArtifactDigest="sha256:" + "7" * 64,
    )
    lineage_bytes = canonical_manual_record_lineage_bytes(lineage)
    campaign_content = _compiled_campaign()
    profile_bytes = _normalization_profile_bytes()
    source = SucceededWorkOutput(
        source_work_unit_id=uuid4(),
        run_id=lineage.run_id,
        stage_run_id=uuid4(),
        stage=WorkStage.NORMALIZATION,
        capability="normalization",
        output_contract="field-observation-batch@1",
        output_digest=decode_observation_batch(batch_bytes).content_digest,
        output_artifact=_artifact(uuid4(), "field_observation_batch", _sha(batch_bytes)),
        input_artifacts=(
            _artifact(uuid4(), "extracted_record", "sha256:" + "8" * 64),
            _artifact(uuid4(), "normalization_profile", _sha(profile_bytes)),
            _artifact(uuid4(), "manual_record_lineage", _sha(lineage_bytes)),
        ),
        source_binding_key="manual_seed_import",
        config_bundle_digest="sha256:" + hashlib.sha256(campaign_content).hexdigest(),
        campaign_bundle_content=campaign_content,
        output_content=batch_bytes,
        normalization_profile_content=profile_bytes,
        manual_record_lineage_content=lineage_bytes,
    )
    return source, batch_bytes, lineage


def _plan(source: SucceededWorkOutput, lineage: ManualRecordLineage) -> PipelineTransitionPlan:
    action = CandidateIntakeAction(
        candidate_id=uuid4(),
        run_id=source.run_id,
        source_work_unit_id=source.source_work_unit_id,
        observation_batch_digest=source.output_digest,
        provenance=ManualRecordIntakeProvenance(
            manual_record_artifact_id=lineage.manual_record_artifact_id,
            manual_record_lineage_artifact=source.input_artifacts[2],
        ),
        source_artifact_ids=tuple(item.artifact_id for item in source.input_artifacts),
    )
    return PipelineTransitionPlan(
        source_work_unit_id=source.source_work_unit_id,
        transition_key="field-observation-batch-candidate-intake-v1",
        disposition=PipelineTransitionDisposition.APPLY,
        plan_digest=PipelineTransitionRegistry().plan(source).plan_digest,
        blocker=None,
        candidate_intake_actions=(action,),
    )


@pytest.fixture()
def engine() -> sa.Engine:
    return sa.create_engine(database_url("candidate intake applier"), poolclass=NullPool)


@pytest.fixture()
def isolated_engine(engine: sa.Engine):
    """Remove this module's candidate rows so shared review queues stay unaffected."""
    created: list[UUID] = []
    yield engine, created
    if not created:
        return
    with engine.begin() as connection:
        for table in (
            ("candidates", "candidate_revisions"),
            ("candidates", "candidates"),
            ("quality", "quality_evaluations"),
            ("review", "review_case_revisions"),
            ("review", "review_cases"),
        ):
            connection.execute(sa.text(f"ALTER TABLE {table[0]}.{table[1]} DISABLE TRIGGER ALL"))
        case_ids = [
            row[0]
            for row in connection.execute(
                sa.text("SELECT case_id FROM review.review_cases WHERE candidate_id = ANY(:ids)"),
                {"ids": created},
            )
        ]
        connection.execute(
            sa.text("DELETE FROM review.review_case_revisions WHERE case_id = ANY(:case_ids)"),
            {"case_ids": case_ids},
        )
        connection.execute(
            sa.text("DELETE FROM quality.quality_evaluations WHERE candidate_id = ANY(:ids)"),
            {"ids": created},
        )
        connection.execute(
            sa.text("DELETE FROM review.review_cases WHERE candidate_id = ANY(:ids)"),
            {"ids": created},
        )
        connection.execute(
            sa.text(
                "DELETE FROM candidates.candidate_revision_evidence WHERE candidate_id = ANY(:ids)"
            ),
            {"ids": created},
        )
        connection.execute(
            sa.text("DELETE FROM candidates.candidate_revisions WHERE candidate_id = ANY(:ids)"),
            {"ids": created},
        )
        connection.execute(
            sa.text("DELETE FROM candidates.candidates WHERE candidate_id = ANY(:ids)"),
            {"ids": created},
        )
        for table in (
            ("candidates", "candidate_revisions"),
            ("candidates", "candidates"),
            ("quality", "quality_evaluations"),
            ("review", "review_case_revisions"),
            ("review", "review_cases"),
        ):
            connection.execute(sa.text(f"ALTER TABLE {table[0]}.{table[1]} ENABLE TRIGGER ALL"))


def _candidate_rows(engine: sa.Engine, candidate_id: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text("SELECT count(*) FROM candidates.candidates WHERE candidate_id = :cid"),
                {"cid": candidate_id},
            ).scalar_one()
        )


def test_candidate_intake_persists_candidate_revision_and_review(engine: sa.Engine) -> None:
    source, _, lineage = _sources()
    plan = _plan(source, lineage)
    applier = PostgresCandidateIntakeApplier(clock=lambda: _NOW)
    candidate_id = plan.candidate_intake_actions[0].candidate_id

    with engine.begin() as connection:
        _seed_acquired_document(connection, _raw_digest(source))
        result_digest = applier(connection, source, plan, correlation_id="intake-test")

    assert result_digest.startswith("sha256:")
    with engine.connect() as connection:
        candidate = connection.execute(
            sa.text("SELECT entity_kind FROM candidates.candidates WHERE candidate_id = :cid"),
            {"cid": candidate_id},
        ).one()
        revision = connection.execute(
            sa.text(
                "SELECT revision, resolution_state, snapshot_digest "
                "FROM candidates.candidate_revisions WHERE candidate_id = :cid"
            ),
            {"cid": candidate_id},
        ).one()
        evidence_count = connection.execute(
            sa.text(
                "SELECT count(*) FROM candidates.candidate_revision_evidence "
                "WHERE candidate_id = :cid"
            ),
            {"cid": candidate_id},
        ).scalar_one()
        quality_rows = connection.execute(
            sa.text("SELECT count(*) FROM quality.quality_evaluations WHERE candidate_id = :cid"),
            {"cid": candidate_id},
        ).scalar_one()
        case = connection.execute(
            sa.text("SELECT case_id FROM review.review_cases WHERE candidate_id = :cid"),
            {"cid": candidate_id},
        ).one()
        case_revision = connection.execute(
            sa.text(
                "SELECT state, reason_codes FROM review.review_case_revisions "
                "WHERE case_id = :case_id"
            ),
            {"case_id": case[0]},
        ).one()

    assert candidate[0] == "organization"
    assert revision[0] == 0
    assert revision[1] == "review"
    assert evidence_count >= 1
    # Intake records the candidate and admits it to review; only entity resolution may
    # declare a quality verdict, so intake must not fabricate one.
    assert quality_rows == 0
    assert case_revision[0] == "open"
    assert "CANDIDATE_REQUIRES_REVIEW" in case_revision[1]


def test_exact_replay_does_not_duplicate_or_alter_candidate(engine: sa.Engine) -> None:
    source, _, lineage = _sources()
    plan = _plan(source, lineage)
    applier = PostgresCandidateIntakeApplier(clock=lambda: _NOW)
    candidate_id = plan.candidate_intake_actions[0].candidate_id

    with engine.begin() as connection:
        _seed_acquired_document(connection, _raw_digest(source))
        first = applier(connection, source, plan, correlation_id="replay-1")
    with engine.begin() as connection:
        second = applier(connection, source, plan, correlation_id="replay-2")

    assert first == second
    with engine.connect() as connection:
        revisions = connection.execute(
            sa.text(
                "SELECT count(*) FROM candidates.candidate_revisions WHERE candidate_id = :cid"
            ),
            {"cid": candidate_id},
        ).scalar_one()
        cases = connection.execute(
            sa.text("SELECT count(*) FROM review.review_cases WHERE candidate_id = :cid"),
            {"cid": candidate_id},
        ).scalar_one()
        qualities = connection.execute(
            sa.text("SELECT count(*) FROM quality.quality_evaluations WHERE candidate_id = :cid"),
            {"cid": candidate_id},
        ).scalar_one()

    assert revisions == 1
    assert cases == 1
    assert qualities == 0


def test_writer_failure_leaves_no_partial_candidate(engine: sa.Engine) -> None:
    source, _, lineage = _sources()
    plan = _plan(source, lineage)
    candidate_id = plan.candidate_intake_actions[0].candidate_id
    applier = PostgresCandidateIntakeApplier(clock=lambda: _NOW)

    with pytest.raises(RuntimeError), engine.begin() as connection:
        _seed_acquired_document(connection, _raw_digest(source))
        applier(connection, source, plan, correlation_id="rollback-test")
        raise RuntimeError("abort after intake writes")

    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM candidates.candidates WHERE candidate_id = :cid"),
                {"cid": candidate_id},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM review.review_cases WHERE candidate_id = :cid"),
                {"cid": candidate_id},
            ).scalar_one()
            == 0
        )


def test_identity_conflict_is_typed_and_writes_nothing_new(engine: sa.Engine) -> None:
    """A stored candidate keeps its immutable identity kind.

    The conflict is provoked through the applier's own candidate insert, because the intake
    plan digest already rejects a lineage that disagrees with the run, and candidate rows
    cannot be mutated once written.
    """

    source, _, lineage = _sources()
    plan = _plan(source, lineage)
    action = plan.candidate_intake_actions[0]
    candidate_id = action.candidate_id
    applier = PostgresCandidateIntakeApplier(clock=lambda: _NOW)

    with engine.begin() as connection:
        _seed_acquired_document(connection, _raw_digest(source))
        applier(connection, source, plan, correlation_id="conflict-seed")

    assert _candidate_rows(engine, candidate_id) == 1

    with pytest.raises(PipelineAdvancementConflict) as caught, engine.begin() as connection:
        applier._insert_candidate(
            connection,
            entity_kind=EntityKind.PLACE,
            now_utc=_NOW,
            correlation_id="conflict-retry",
            action=action,
        )

    assert caught.value.code == "PIPELINE_CANDIDATE_IDENTITY_CONFLICT"
    # The rejected attempt added no second candidate row.
    assert _candidate_rows(engine, candidate_id) == 1
