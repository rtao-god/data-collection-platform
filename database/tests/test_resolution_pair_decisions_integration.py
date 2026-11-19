"""Operator pair decisions are durable, bounded and never silently reused.

The decision owner is exercised against real PostgreSQL: identity, history, idempotency,
supersession and the export fence that refuses a verdict derived from replaced decisions.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from db_support import database_url, digest, seed_collection_run
from sqlalchemy.pool import NullPool

from collection_infrastructure.postgres.export_readers import PostgresExportQualityReader
from collection_infrastructure.postgres.resolution_pair_decisions import (
    PairDecisionConflict,
    PairDecisionRequest,
    PostgresResolutionPairDecisionRepository,
)
from collection_infrastructure.resolution import ResolutionCandidateSource
from collection_infrastructure.resolution.decisions import (
    current_decision_basis,
    manual_decisions_for_revisions,
    recorded_decision_basis,
)
from resolution_contracts import candidate_pair_id

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_MARKET_AREA_DIGEST = digest("pair-decision-market-area")
_POLICY_DIGEST = digest("pair-decision-policy")
# sha256_json([]): the canonical identity of "this resolution reflected no decisions".
_EMPTY_DECISION_BASIS = "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


class RecordingScheduler:
    """Records the recomputation the owner asks for instead of running the pipeline."""

    def __init__(self) -> None:
        self.runs: list[tuple[UUID, str]] = []

    def schedule_after_pair_decision(
        self,
        connection: sa.Connection,
        *,
        run_id: UUID,
        correlation_id: str,
    ) -> UUID | None:
        del connection
        self.runs.append((run_id, correlation_id))
        return None


_SOURCE_URL = "https://www.wikidata.org/wiki/Special:EntityData/Q1.json"
_RAW_ARTIFACT_ID = "00000000-0000-0000-0000-00000000f001"


def _observation(field_key: str, value: dict[str, Any], *, name: str) -> dict[str, Any]:
    return {
        "observationId": digest("pair-decision-observation", field_key, name),
        "fieldKey": field_key,
        "state": "observed",
        "value": value,
        "sourceRecordId": "wikidata:Q1",
        "sourcePolicyDigest": _POLICY_DIGEST,
        "evidence": {
            "sourceUrl": _SOURCE_URL,
            "observedAtUtc": "2026-09-01T11:00:00+00:00",
            "evidenceDigest": digest("pair-decision-evidence", field_key, name),
            "rawArtifactId": _RAW_ARTIFACT_ID,
        },
    }


def _payload(*, name: str, website: str) -> dict[str, Any]:
    return {
        "projection": {"display_name": "names", "coordinates": "geography"},
        "fieldObservations": [
            _observation(
                "display_name",
                {"kind": "text", "normalized": name, "locale": "de"},
                name=name,
            ),
            _observation("website", {"kind": "url", "normalized": website}, name=name),
            _observation(
                "coordinates",
                {"kind": "geo_point", "normalized": "52.5007771,13.4168436"},
                name=name,
            ),
        ],
        "exportPolicy": {
            "sourceKey": "wikidata",
            "legalStatus": "approved",
            "usagePolicy": "publishable",
            "attribution": "Wikidata, CC0",
        },
        "geography": {
            "coverage": "inside",
            "marketAreaRevision": "berlin_boundary_v1",
            "marketAreaDigest": _MARKET_AREA_DIGEST,
            "latitude": 52.5007771,
            "longitude": 13.4168436,
        },
    }


class _Fixture:
    def __init__(self, engine: sa.Engine) -> None:
        self.run_id = uuid4()
        self.left_id = uuid4()
        self.right_id = uuid4()
        self.left_revision = 0
        self.right_revision = 0
        self.resolution_id = uuid4()
        self.pair_id = candidate_pair_id(self.left_id, self.right_id)

    def sources(self, engine: sa.Engine) -> tuple[ResolutionCandidateSource, ...]:
        return _sources(
            engine, (self.left_id, self.left_revision), (self.right_id, self.right_revision)
        )


def _seed(engine: sa.Engine, *, pairs: bool = True) -> _Fixture:
    fixture = _Fixture(engine)
    with engine.begin() as connection:
        seed_collection_run(
            connection,
            run_id=fixture.run_id,
            bundle_digest=digest("pair-decision-bundle", str(fixture.run_id)),
            recorded_at_utc=_NOW,
        )
        for candidate_id, name in (
            (fixture.left_id, "Studio Alpha"),
            (fixture.right_id, "Studio Alpha Berlin"),
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO candidates.candidates"
                    " (candidate_id, entity_kind, created_at_utc, correlation_id)"
                    " VALUES (:candidate_id, 'organization', :now, 'pair-decisions')"
                ),
                {"candidate_id": candidate_id, "now": _NOW},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO candidates.candidate_revisions"
                    " (candidate_id, revision, cluster_id, resolution_state, snapshot_digest,"
                    " source_lineage_digest, normalized_payload, recorded_at_utc, correlation_id)"
                    " VALUES (:candidate_id, 0, :cluster_id, 'review', :snapshot, :lineage,"
                    " CAST(:payload AS jsonb), :now, 'pair-decisions')"
                ),
                {
                    "candidate_id": candidate_id,
                    "cluster_id": uuid4(),
                    "snapshot": digest("pair-decision-snapshot", str(candidate_id)),
                    "lineage": digest("pair-decision-lineage", str(candidate_id)),
                    "payload": json.dumps(
                        _payload(
                            name=name,
                            website=f"https://studio-{candidate_id.hex[:6]}.example/",
                        ),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "now": _NOW,
                },
            )
        _insert_resolution(
            connection,
            run_id=fixture.run_id,
            resolution_id=fixture.resolution_id,
            supersedes=None,
            decision_basis_digest=None,
            candidates=(
                (fixture.left_id, fixture.left_revision),
                (fixture.right_id, fixture.right_revision),
            ),
        )
        if pairs:
            connection.execute(
                sa.text(
                    "INSERT INTO candidates.resolution_pairs"
                    " (resolution_id, pair_id, left_candidate_id, right_candidate_id,"
                    " disposition, reason_codes, evidence_strength)"
                    " VALUES (:resolution_id, :pair_id, :left_id, :right_id, 'review_required',"
                    " ARRAY['NAME_OVERLAP']::text[], 1)"
                ),
                {
                    "resolution_id": fixture.resolution_id,
                    "pair_id": fixture.pair_id,
                    "left_id": fixture.left_id,
                    "right_id": fixture.right_id,
                },
            )
    return fixture


def _insert_resolution(
    connection: sa.Connection,
    *,
    run_id: UUID,
    resolution_id: UUID,
    supersedes: UUID | None,
    decision_basis_digest: str | None,
    candidates: Sequence[tuple[UUID, int]],
) -> None:
    work_id = uuid4()
    # A run has one entity-resolution stage run; a superseding resolution belongs to it.
    stage_run_id = connection.execute(
        sa.text(
            "SELECT stage_run_id FROM runs.stage_runs"
            " WHERE run_id = :run_id AND stage = 'entity_resolution'"
        ),
        {"run_id": run_id},
    ).scalar_one_or_none()
    if stage_run_id is None:
        stage_run_id = uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO runs.stage_runs (stage_run_id, run_id, stage, state, revision,"
                " created_at_utc, updated_at_utc, correlation_id)"
                " VALUES (:stage_run_id, :run_id, 'entity_resolution', 'running', 0, :now, :now,"
                " 'pair-decisions')"
            ),
            {"stage_run_id": stage_run_id, "run_id": run_id, "now": _NOW},
        )
    connection.execute(
        sa.text(
            "INSERT INTO work.work_units (work_id, run_id, stage_run_id, stage, capability,"
            " semantic_key, input_digest, expected_output_contract, priority, state,"
            " attempt_count, max_attempts, retry_initial_delay_seconds, retry_multiplier,"
            " retry_max_delay_seconds, available_at_utc, output_contract, output_digest,"
            " completed_at_utc, revision, created_at_utc, updated_at_utc, correlation_id,"
            " failure_count)"
            " VALUES (:work_id, :run_id, :stage_run_id, 'entity_resolution', 'entity_resolution',"
            " :semantic_key, :input_digest, 'entity-resolution-snapshot@1', 0, 'succeeded', 1, 3,"
            " 30, 2, 900, :now, 'entity-resolution-snapshot@1', :output_digest, :now, 0, :now,"
            " :now, 'pair-decisions', 0)"
        ),
        {
            "work_id": work_id,
            "run_id": run_id,
            "stage_run_id": stage_run_id,
            "semantic_key": digest("pair-decisions-work", str(resolution_id)),
            "input_digest": digest("pair-decisions-batch", str(resolution_id)),
            "output_digest": digest("pair-decisions-snapshot", str(resolution_id)),
            "now": _NOW,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO candidates.resolution_runs (resolution_id, run_id, work_id, batch_id,"
            " batch_digest, snapshot_digest, market_area_revision, market_area_digest,"
            " quality_policy_digest, supersedes_resolution_id, applied_at_utc, correlation_id,"
            " decision_basis_digest)"
            " VALUES (:resolution_id, :run_id, :work_id, :batch_id, :batch_digest,"
            " :snapshot_digest, 'berlin_boundary_v1', :market_area_digest, :policy_digest,"
            " :supersedes, :now, 'pair-decisions',"
            " COALESCE(:decision_basis_digest, :empty_basis))"
        ),
        {
            "resolution_id": resolution_id,
            "run_id": run_id,
            "work_id": work_id,
            "batch_id": uuid4(),
            "batch_digest": digest("pair-decisions-batch", str(resolution_id)),
            "snapshot_digest": digest("pair-decisions-snapshot", str(resolution_id)),
            "market_area_digest": _MARKET_AREA_DIGEST,
            "policy_digest": _POLICY_DIGEST,
            "supersedes": supersedes,
            "now": _NOW,
            "decision_basis_digest": decision_basis_digest,
            "empty_basis": _EMPTY_DECISION_BASIS,
        },
    )
    for candidate_id, revision in candidates:
        cluster_id = uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO candidates.resolution_clusters (resolution_id, cluster_id,"
                " lineage_kind, parent_cluster_ids, parent_membership_digest,"
                " blocked_match_edge_ids)"
                " VALUES (:resolution_id, :cluster_id, 'new', ARRAY[]::uuid[], :membership,"
                " ARRAY[]::text[])"
            ),
            {
                "resolution_id": resolution_id,
                "cluster_id": cluster_id,
                "membership": digest("pair-decisions-membership", str(cluster_id)),
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO candidates.resolution_cluster_members"
                " (resolution_id, cluster_id, candidate_id)"
                " VALUES (:resolution_id, :cluster_id, :candidate_id)"
            ),
            {
                "resolution_id": resolution_id,
                "cluster_id": cluster_id,
                "candidate_id": candidate_id,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO quality.quality_evaluations (evaluation_id, candidate_id,"
                " candidate_revision, policy_digest, export_eligible, blockers,"
                " evaluation_digest, evaluated_at_utc, correlation_id, resolution_id)"
                " VALUES (:evaluation_id, :candidate_id, :revision, :policy_digest, true,"
                " ARRAY[]::text[], :evaluation_digest, :now, 'pair-decisions', :resolution_id)"
            ),
            {
                "evaluation_id": uuid4(),
                "candidate_id": candidate_id,
                "revision": revision,
                "policy_digest": _POLICY_DIGEST,
                "evaluation_digest": digest(
                    "pair-decisions-evaluation", str(candidate_id), str(revision)
                ),
                "now": _NOW,
                "resolution_id": resolution_id,
            },
        )


def _sources(
    engine: sa.Engine,
    *coordinates: tuple[UUID, int],
) -> tuple[ResolutionCandidateSource, ...]:
    sources: list[ResolutionCandidateSource] = []
    with engine.connect() as connection:
        for candidate_id, revision in coordinates:
            row = (
                connection.execute(
                    sa.text(
                        "SELECT r.normalized_payload, c.entity_kind"
                        " FROM candidates.candidate_revisions AS r"
                        " JOIN candidates.candidates AS c USING (candidate_id)"
                        " WHERE r.candidate_id = :candidate_id AND r.revision = :revision"
                    ),
                    {"candidate_id": candidate_id, "revision": revision},
                )
                .mappings()
                .one()
            )
            sources.append(
                ResolutionCandidateSource(
                    candidate_id=candidate_id,
                    revision=revision,
                    entity_kind=str(row["entity_kind"]),
                    normalized_payload=row["normalized_payload"],
                )
            )
    return tuple(sources)


def _request(fixture: _Fixture, *, action: str = "match", revision: int = 0) -> PairDecisionRequest:
    return PairDecisionRequest(
        pair_id=fixture.pair_id,
        left_candidate_id=fixture.left_id,
        left_candidate_revision=fixture.left_revision,
        right_candidate_id=fixture.right_id,
        right_candidate_revision=fixture.right_revision,
        action=action,
        reason_code="SAME_STUDIO",
        rationale="Both records name the same studio and share one website.",
        evidence_references=("sha256:" + "1" * 64,),
        expected_decision_revision=revision,
    )


def _key(prefix: str) -> str:
    """One fresh command key per call; keys are globally unique across the database."""

    return f"{prefix}-{uuid4().hex}"


def _repository(engine: sa.Engine, scheduler: RecordingScheduler):
    return PostgresResolutionPairDecisionRepository(
        engine,
        scheduler=scheduler,
        clock=lambda: _NOW,
    )


def test_open_pairs_are_listed_without_any_decision() -> None:
    engine = sa.create_engine(database_url("pair decisions"), poolclass=NullPool)
    fixture = _seed(engine)
    scheduler = RecordingScheduler()

    pairs = _repository(engine, scheduler).list_pending_pairs(fixture.run_id)

    assert [(item.pair_id, item.disposition) for item in pairs] == [
        (fixture.pair_id, "review_required")
    ]
    assert pairs[0].decision is None
    assert scheduler.runs == []


def test_a_pair_decision_is_recorded_once_and_its_history_is_immutable() -> None:
    engine = sa.create_engine(database_url("pair decisions"), poolclass=NullPool)
    fixture = _seed(engine)
    scheduler = RecordingScheduler()
    repository = _repository(engine, scheduler)

    records = repository.submit_decisions(
        run_id=fixture.run_id,
        actor_id="operator-1",
        idempotency_key=_key("decision-1"),
        requests=(_request(fixture),),
        correlation_id="pair-decision-1",
    )

    assert len(records) == 1
    record = records[0]
    assert record.pair_id == fixture.pair_id
    assert record.action == "match"
    assert record.revision == 0
    assert record.supersedes_decision_id is None
    # The recorded decision is what a later resolution reuses.
    decisions = manual_decisions_for_revisions(engine.connect(), fixture.sources(engine))
    assert [item.decision_id for item in decisions] == [record.decision_id]
    assert scheduler.runs == [(fixture.run_id, "pair-decision-1")]

    with engine.begin() as connection, pytest.raises(sa.exc.OperationalError) as rejected:
        connection.execute(
            sa.text(
                "UPDATE candidates.resolution_pair_decisions SET action = 'separate'"
                " WHERE decision_id = :decision_id"
            ),
            {"decision_id": record.decision_id},
        )
    assert "immutable" in str(rejected.value)


def test_a_replaced_decision_appends_a_revision_and_supersedes_the_old_one() -> None:
    engine = sa.create_engine(database_url("pair decisions"), poolclass=NullPool)
    fixture = _seed(engine)
    repository = _repository(engine, RecordingScheduler())
    first = repository.submit_decisions(
        run_id=fixture.run_id,
        actor_id="operator-1",
        idempotency_key=_key("decision-1"),
        requests=(_request(fixture),),
        correlation_id="pair-decision-1",
    )[0]

    second = repository.submit_decisions(
        run_id=fixture.run_id,
        actor_id="operator-2",
        idempotency_key=_key("decision-2"),
        requests=(_request(fixture, action="separate", revision=1),),
        correlation_id="pair-decision-2",
    )[0]

    assert second.revision == 1
    assert second.action == "separate"
    assert second.supersedes_decision_id == first.decision_id
    # Only the newest revision counts, so one pair never carries two active judgements.
    decisions = manual_decisions_for_revisions(engine.connect(), fixture.sources(engine))
    assert [(item.decision_id, item.action.value) for item in decisions] == [
        (second.decision_id, "separate")
    ]


def test_a_replayed_command_is_idempotent_and_an_altered_one_is_rejected() -> None:
    engine = sa.create_engine(database_url("pair decisions"), poolclass=NullPool)
    fixture = _seed(engine)
    repository = _repository(engine, RecordingScheduler())
    key = _key("decision-1")
    first = repository.submit_decisions(
        run_id=fixture.run_id,
        actor_id="operator-1",
        idempotency_key=key,
        requests=(_request(fixture),),
        correlation_id="pair-decision-1",
    )

    replay = repository.submit_decisions(
        run_id=fixture.run_id,
        actor_id="operator-1",
        idempotency_key=key,
        requests=(_request(fixture),),
        correlation_id="pair-decision-1-retry",
    )

    assert replay == first
    with pytest.raises(PairDecisionConflict) as conflict:
        repository.submit_decisions(
            run_id=fixture.run_id,
            actor_id="operator-1",
            idempotency_key=key,
            requests=(_request(fixture, action="separate"),),
            correlation_id="pair-decision-1-altered",
        )
    assert conflict.value.code == "PAIR_DECISION_IDEMPOTENCY_CONFLICT"
    assert conflict.value.status_code == 409


def test_a_stale_or_foreign_decision_is_rejected() -> None:
    engine = sa.create_engine(database_url("pair decisions"), poolclass=NullPool)
    fixture = _seed(engine)
    repository = _repository(engine, RecordingScheduler())
    repository.submit_decisions(
        run_id=fixture.run_id,
        actor_id="operator-1",
        idempotency_key=_key("decision-1"),
        requests=(_request(fixture),),
        correlation_id="pair-decision-1",
    )

    with pytest.raises(PairDecisionConflict) as stale:
        repository.submit_decisions(
            run_id=fixture.run_id,
            actor_id="operator-2",
            idempotency_key=_key("decision-2"),
            requests=(_request(fixture, action="separate"),),
            correlation_id="pair-decision-2",
        )
    assert stale.value.code == "PAIR_DECISION_REVISION_CONFLICT"
    assert stale.value.context["currentDecisionRevision"] == 1

    # A resolution that reported no pair cannot be decided at all.
    unpaired = _seed(engine, pairs=False)
    with pytest.raises(PairDecisionConflict) as unknown:
        repository.submit_decisions(
            run_id=unpaired.run_id,
            actor_id="operator-1",
            idempotency_key=_key("decision-3"),
            requests=(_request(unpaired),),
            correlation_id="pair-decision-3",
        )
    assert unknown.value.code == "PAIR_DECISION_PAIR_UNKNOWN"


def test_a_verdict_derived_from_replaced_decisions_cannot_be_exported() -> None:
    """The mandatory negative: a stale resolution must never authorize an export."""

    engine = sa.create_engine(database_url("pair decisions"), poolclass=NullPool)
    fixture = _seed(engine)
    repository = _repository(engine, RecordingScheduler())

    assert recorded_decision_basis(engine.connect(), fixture.run_id) == current_decision_basis(
        engine.connect(), fixture.run_id
    )
    cluster_ids = _cluster_ids(engine, fixture.resolution_id)
    assert PostgresExportQualityReader(engine).read_assessments(
        run_id=fixture.run_id, cluster_ids=cluster_ids
    )

    repository.submit_decisions(
        run_id=fixture.run_id,
        actor_id="operator-1",
        idempotency_key=_key("decision-1"),
        requests=(_request(fixture),),
        correlation_id="pair-decision-1",
    )

    assert recorded_decision_basis(engine.connect(), fixture.run_id) != current_decision_basis(
        engine.connect(), fixture.run_id
    )
    with pytest.raises(Exception) as refusal:
        PostgresExportQualityReader(engine).read_assessments(
            run_id=fixture.run_id, cluster_ids=cluster_ids
        )
    assert getattr(refusal.value, "code", "") == "EXPORT_QUALITY_DECISION_BASIS_STALE"

    # A resolution derived from the new decisions restores export authority.
    with engine.begin() as connection:
        _insert_resolution(
            connection,
            run_id=fixture.run_id,
            resolution_id=uuid4(),
            supersedes=fixture.resolution_id,
            decision_basis_digest=current_decision_basis(engine.connect(), fixture.run_id),
            candidates=(
                (fixture.left_id, fixture.left_revision),
                (fixture.right_id, fixture.right_revision),
            ),
        )
    refreshed = _cluster_ids(engine, _current_resolution_id(engine, fixture.run_id))
    assessments = PostgresExportQualityReader(engine).read_assessments(
        run_id=fixture.run_id, cluster_ids=refreshed
    )
    assert sorted(assessments) == sorted(refreshed)


def _current_resolution_id(engine: sa.Engine, run_id: UUID) -> UUID:
    with engine.connect() as connection:
        return UUID(
            str(
                connection.execute(
                    sa.text(
                        "SELECT resolution_id FROM candidates.resolution_runs AS r"
                        " WHERE r.run_id = :run_id AND NOT EXISTS ("
                        "  SELECT 1 FROM candidates.resolution_runs AS s"
                        "  WHERE s.supersedes_resolution_id = r.resolution_id)"
                    ),
                    {"run_id": run_id},
                ).scalar_one()
            )
        )


def _cluster_ids(engine: sa.Engine, resolution_id: UUID) -> tuple[UUID, ...]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT DISTINCT cluster_id FROM candidates.resolution_cluster_members"
                " WHERE resolution_id = :resolution_id ORDER BY cluster_id"
            ),
            {"resolution_id": resolution_id},
        ).all()
    return tuple(UUID(str(row[0])) for row in rows)
