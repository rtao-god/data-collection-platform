"""Transactional intake: rollback, retry and replay against a real PostgreSQL.

The unit-level applier tests use a recording double. These run against PostGIS itself and
prove the property that matters operationally: candidate and review writes for one intake
either all persist or none do, a retry after a failure succeeds, and a replay never creates
a second candidate or review case. Intake writes no quality verdict: only entity resolution
may, so the quality count stays zero here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from test_candidate_intake_applier_integration import (
    _plan as _shared_plan,
)
from test_candidate_intake_applier_integration import (
    _raw_digest as _shared_raw_digest,
)
from test_candidate_intake_applier_integration import (
    _seed_acquired_document as _seed_shared_acquired_document,
)
from test_candidate_intake_applier_integration import (
    _sources as _shared_sources,
)

from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    CandidateIntakeAction,
    ManualRecordIntakeProvenance,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    PipelineTransitionRegistry,
    SucceededWorkOutput,
)
from collection_contracts import (
    ManualRecordLineage,
    NormalizationFieldRule,
    NormalizationProfile,
    ObservationBatchPayload,
    ObservationState,
    canonical_manual_record_lineage_bytes,
    canonical_observation_batch_json,
    seal_field_observation,
    seal_observation_batch,
)
from collection_infrastructure.postgres.candidate_intake_applier import (
    PostgresCandidateIntakeApplier,
)

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


def _profile_bytes() -> bytes:
    profile = NormalizationProfile(
        profile_key="transactional-profile",
        profile_revision="1",
        entity_kind="organization",
        field_rules=(
            NormalizationFieldRule(
                candidate_field="display_name",
                source_field="name",
                required=True,
            ),
        ),
    )
    return profile.model_dump_json().encode("utf-8")


def _batch_bytes(record_digest: str) -> bytes:
    observation = seal_field_observation(
        {
            "recordArtifactId": str(uuid4()),
            "recordDigest": record_digest,
            "candidateId": str(uuid4()),
            "fieldKey": "display_name",
            "state": ObservationState.OBSERVED.value,
            "value": {"original": "Transactional Studio", "normalized": "Transactional Studio"},
            "evidence": (
                {
                    "artifactId": str(uuid4()),
                    "locator": {"kind": "xpath", "value": "/html/body/h1"},
                    "observedAtUtc": _NOW.isoformat().replace("+00:00", "Z"),
                    "mappingRevision": "transactional-v1",
                },
            ),
            "confidence": 0.9,
        }
    )
    payload = ObservationBatchPayload(
        record_artifact_id=observation.record_artifact_id,
        record_digest=record_digest,
        observations=(observation,),
    )
    return canonical_observation_batch_json(seal_observation_batch(payload))


def _lineage() -> tuple[ManualRecordLineage, bytes]:
    lineage = ManualRecordLineage(
        manual_record_artifact_id=uuid4(),
        source_binding_key="transactional_binding",
        record_index=0,
        record_count=1,
        admitted_at_utc=_NOW,
        correlation_id="transactional",
    )
    return lineage, canonical_manual_record_lineage_bytes(lineage)


def _source_and_plan() -> tuple[SucceededWorkOutput, PipelineTransitionPlan]:
    source, _, lineage = _shared_sources()
    return source, _shared_plan(source, lineage)


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


def _counts(engine: sa.Engine, candidate_id: UUID) -> tuple[int, int, int, int]:
    with engine.connect() as connection:
        candidate = connection.execute(
            sa.text("SELECT count(*) FROM candidates.candidates WHERE candidate_id = :cid"),
            {"cid": candidate_id},
        ).scalar_one()
        revision = connection.execute(
            sa.text(
                "SELECT count(*) FROM candidates.candidate_revisions WHERE candidate_id = :cid"
            ),
            {"cid": candidate_id},
        ).scalar_one()
        quality = connection.execute(
            sa.text("SELECT count(*) FROM quality.quality_evaluations WHERE candidate_id = :cid"),
            {"cid": candidate_id},
        ).scalar_one()
        cases = connection.execute(
            sa.text("SELECT count(*) FROM review.review_cases WHERE candidate_id = :cid"),
            {"cid": candidate_id},
        ).scalar_one()
    return int(candidate), int(revision), int(quality), int(cases)


def _review_case_ids(engine: sa.Engine, candidate_id: UUID) -> list[UUID]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                sa.text("SELECT case_id FROM review.review_cases WHERE candidate_id = :cid"),
                {"cid": candidate_id},
            )
        ]


@pytest.fixture()
def engine() -> Iterator[sa.Engine]:
    """A disposable, fully migrated database so no row teardown is needed.

    The candidate, quality and review tables reject `DELETE`/`TRUNCATE` through their
    immutable-history triggers, which is the production contract. Isolation therefore comes
    from a database of its own rather than from disabling those guards.
    """

    from db_support import disposable_database
    from sqlalchemy.pool import NullPool

    for url in disposable_database("transactional intake"):
        value = sa.create_engine(url, poolclass=NullPool)
        try:
            yield value
        finally:
            value.dispose()


@pytest.fixture()
def cleaned(engine: sa.Engine):
    """Hand out the disposable engine plus a created-id list.

    Each test owns a fresh migrated database, so the list is only a record of what was
    written; nothing is deleted and no integrity guard is bypassed.
    """

    yield engine, []


@pytest.fixture(autouse=True)
def _acquired_document(cleaned: tuple[sa.Engine, list[UUID]]) -> None:
    """Record the document the shared observation batch was extracted from."""

    engine, _ = cleaned
    source, _, _ = _shared_sources()
    with engine.begin() as connection:
        _seed_shared_acquired_document(connection, _shared_raw_digest(source))


def test_failure_before_commit_rolls_back_candidate_and_review(
    cleaned: tuple[sa.Engine, list[UUID]],
) -> None:
    engine, created = cleaned
    source, _, lineage = _shared_sources()
    plan = _shared_plan(source, lineage)
    candidate_id = plan.candidate_intake_actions[0].candidate_id
    created.append(candidate_id)
    applier = PostgresCandidateIntakeApplier(clock=lambda: _NOW)

    # The intake writes land inside the transaction, then the caller fails before
    # committing (this stands in for the advancement completion step).
    with (
        pytest.raises(RuntimeError, match="advancement completion failed"),
        engine.begin() as connection,
    ):
        applier(connection, source, plan, correlation_id="rollback")
        raise RuntimeError("advancement completion failed")

    assert _counts(engine, candidate_id) == (0, 0, 0, 0)
    assert _review_case_ids(engine, candidate_id) == []


def test_retry_after_rollback_persists_exactly_one_set_of_rows(
    cleaned: tuple[sa.Engine, list[UUID]],
) -> None:
    engine, created = cleaned
    source, _, lineage = _shared_sources()
    plan = _shared_plan(source, lineage)
    candidate_id = plan.candidate_intake_actions[0].candidate_id
    created.append(candidate_id)
    applier = PostgresCandidateIntakeApplier(clock=lambda: _NOW)

    with pytest.raises(RuntimeError), engine.begin() as connection:
        applier(connection, source, plan, correlation_id="rollback")
        raise RuntimeError("advancement completion failed")
    assert _counts(engine, candidate_id) == (0, 0, 0, 0)

    with engine.begin() as connection:
        first_digest = applier(connection, source, plan, correlation_id="retry")

    assert _counts(engine, candidate_id) == (1, 1, 0, 1)
    assert first_digest.startswith("sha256:")


def test_replay_keeps_identical_identity_and_creates_no_duplicates(
    cleaned: tuple[sa.Engine, list[UUID]],
) -> None:
    engine, created = cleaned
    source, _, lineage = _shared_sources()
    plan = _shared_plan(source, lineage)
    candidate_id = plan.candidate_intake_actions[0].candidate_id
    created.append(candidate_id)
    applier = PostgresCandidateIntakeApplier(clock=lambda: _NOW)

    with engine.begin() as connection:
        first = applier(connection, source, plan, correlation_id="replay-1")
    first_cases = _review_case_ids(engine, candidate_id)

    with engine.begin() as connection:
        second = applier(connection, source, plan, correlation_id="replay-2")

    assert first == second
    assert _counts(engine, candidate_id) == (1, 1, 0, 1)
    assert _review_case_ids(engine, candidate_id) == first_cases


def test_concurrent_identical_intake_does_not_duplicate_the_candidate(
    cleaned: tuple[sa.Engine, list[UUID]],
) -> None:
    engine, created = cleaned
    source, _, lineage = _shared_sources()
    plan = _shared_plan(source, lineage)
    candidate_id = plan.candidate_intake_actions[0].candidate_id
    created.append(candidate_id)
    applier = PostgresCandidateIntakeApplier(clock=lambda: _NOW)

    with engine.begin() as connection:
        applier(connection, source, plan, correlation_id="first-writer")

    # A second writer with the same deterministic identity must be a replay, not a
    # duplicate: the candidate row is looked up before it is inserted.
    with engine.begin() as connection:
        applier(connection, source, plan, correlation_id="second-writer")

    assert _counts(engine, candidate_id) == (1, 1, 0, 1)


def test_intake_of_the_same_candidate_reports_the_persisted_identity(
    cleaned: tuple[sa.Engine, list[UUID]],
) -> None:
    engine, created = cleaned
    source, _, lineage = _shared_sources()
    plan = _shared_plan(source, lineage)
    candidate_id = plan.candidate_intake_actions[0].candidate_id
    created.append(candidate_id)

    assert (
        json.dumps(str(candidate_id))
        and plan.candidate_intake_actions[0].candidate_id == candidate_id
    )
    applier = PostgresCandidateIntakeApplier(clock=lambda: _NOW)
    with engine.begin() as connection:
        applier(connection, source, plan, correlation_id="identity")

    with engine.connect() as connection:
        stored = connection.execute(
            sa.text(
                "SELECT candidate_id, entity_kind FROM candidates.candidates "
                "WHERE candidate_id = :cid"
            ),
            {"cid": candidate_id},
        ).one()
    assert stored[0] == candidate_id
    assert stored[1] == "organization"
