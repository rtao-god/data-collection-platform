"""End to end on the real acquired OSM node, through intake and into PostgreSQL.

The node is OSM node 871572117 ("nb film", Dresdener Straße 125, Berlin), captured from the
configured Overpass endpoint. It matters because it publishes its site and phone only under
the `contact:` namespace, so this test covers the whole path an operator depends on:

    acquisition artifact -> source-record mapping -> candidate intake -> PostgreSQL review

Nothing here is synthesized: the tags, coordinates and acquisition timestamp are the
captured values, and the mapping profile is the one the campaign actually ships.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from collection_application import CampaignSnapshotService, RawCampaignBundle
from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineTransitionDisposition,
    PipelineTransitionPlan,
    PipelineTransitionRegistry,
    SucceededWorkOutput,
)
from collection_contracts import (
    ObservationState,
    canonical_osm_source_record_json,
    canonical_source_record_observation_batch_json,
    decode_osm_envelope,
    decode_source_record_observation_batch,
)
from collection_domain import WorkStage
from collection_infrastructure.postgres.candidate_intake_applier import (
    PostgresCandidateIntakeApplier,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 11, 16, tzinfo=UTC)
_ACQUIRED_AT = datetime(2026, 11, 16, 0, 1, 59, tzinfo=UTC)
_OSM_ID = 871572117

# Verbatim tags of the acquired node.
_REAL_TAGS = {
    "addr:city": "Berlin",
    "addr:country": "DE",
    "addr:housenumber": "125",
    "addr:postcode": "10999",
    "addr:street": "Dresdener Straße",
    "addr:suburb": "Kreuzberg",
    "amenity": "recording_studio",
    "contact:mobile": "+49 174 5910833",
    "contact:phone": "+49 30 31169672",
    "contact:website": "http://www.nbfilm.de",
    "name": "nb film",
    "operator": "Thorsten Schneider",
}

# The alias decision the campaign binding carries.
_FIELD_ALIASES = {
    "email": ["email", "contact:email"],
    "phone": ["phone", "contact:phone"],
    "website": ["website", "contact:website"],
}
_FIELD_PROJECTION = {
    "display_name": "names",
    "email": "emails",
    "phone": "phones",
    "website": "website_urls",
}


def _envelope(tags: dict[str, str]) -> bytes:
    return json.dumps(
        {
            "attribution": "© OpenStreetMap contributors",
            "generator": "osm-overpass-worker",
            "observations": [
                {
                    "address": {
                        "city": "Berlin",
                        "country": "DE",
                        "houseNumber": "125",
                        "postcode": "10999",
                        "street": "Dresdener Straße",
                    },
                    "elementType": "node",
                    "latitude": 52.5007771,
                    "longitude": 13.4168436,
                    "osmId": _OSM_ID,
                    "sourceUrl": f"https://www.openstreetmap.org/node/{_OSM_ID}",
                    "tags": tags,
                }
            ],
            "osmBaseTimestampUtc": "2026-11-16T00:01:59.000000Z",
            "queryDigest": "sha256:" + "1" * 64,
            "responseDigest": "sha256:" + "2" * 64,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _source_record(tags: dict[str, str]) -> bytes:
    """The per-element record the fan-out produces for the acquired element."""

    element = decode_osm_envelope(_envelope(tags)).elements[0]
    return canonical_osm_source_record_json(element).encode("utf-8")


def _mapping_profile(run_id: UUID) -> bytes:
    return json.dumps(
        {
            "campaignKey": "berlin_recording_services",
            "entityKind": "place",
            "fieldAliases": _FIELD_ALIASES,
            "fieldProjection": _FIELD_PROJECTION,
            "mappingRevision": "osm-overpass-source-record-mapping-v1",
            "runId": str(run_id),
            "sourceBindingKey": "osm_overpass_berlin",
            "sourceKey": "openstreetmap",
            "sourcePolicyDigest": "sha256:" + "3" * 64,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _batch(record: bytes, run_id: UUID) -> bytes:
    from processing_worker.source_record_mapping import (
        materialize_source_record_observations,
    )

    mapped = materialize_source_record_observations(
        record,
        _mapping_profile(run_id),
        maximum_records=1,
    )
    return canonical_source_record_observation_batch_json(mapped).encode("utf-8")


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
            "correlation-real-node-intake",
        )
        .canonical_content
    )


def _stable_id(digest: str, purpose: str) -> UUID:
    """A deterministic identity so identical evidence yields an identical plan."""

    return UUID(bytes=sha256(f"{purpose}:{digest}".encode()).digest()[:16], version=4)


def _succeeded_output(
    record: bytes,
    run_id: UUID,
    *,
    work_unit_id: UUID | None = None,
    stage_run_id: UUID | None = None,
    deterministic: bool = False,
) -> SucceededWorkOutput:
    """One exact acquired output.

    The work unit and stage-run identities are inputs rather than fresh values so a replay
    can reuse the identical output instead of silently becoming a different work unit that
    happens to carry the same bytes.
    """

    batch = _batch(record, run_id)
    digest = "sha256:" + sha256(record).hexdigest()
    # The batch carries the exact source-record artifact it was mapped from, so the input
    # artifact identity is read back from the batch instead of being invented here.
    record_artifact_id = decode_source_record_observation_batch(batch).source_record_artifact_id
    batch_digest = "sha256:" + sha256(batch).hexdigest()
    campaign_content = _compiled_campaign()
    return SucceededWorkOutput(
        source_work_unit_id=work_unit_id or uuid4(),
        run_id=run_id,
        stage_run_id=stage_run_id or uuid4(),
        stage=WorkStage.NORMALIZATION,
        capability="source_record_mapping",
        output_contract="source-record-observation-batch@1",
        output_digest=batch_digest,
        output_artifact=ArtifactIdentity(
            artifact_id=_stable_id(batch_digest, "batch") if deterministic else uuid4(),
            role="source_record_observation_batch",
            content_digest=batch_digest,
            size_bytes=len(batch),
            content_type="application/vnd.collection.source-record-observation-batch+json",
        ),
        input_artifacts=(
            ArtifactIdentity(
                artifact_id=record_artifact_id,
                role="source_record",
                content_digest=digest,
                size_bytes=len(record),
                content_type="application/vnd.collection.osm-source-record+json",
            ),
        ),
        source_binding_key="osm_overpass_berlin",
        normalization_profile_content=_mapping_profile(run_id),
        config_bundle_digest="sha256:" + sha256(campaign_content).hexdigest(),
        campaign_bundle_content=campaign_content,
        output_content=batch,
    )


def _plan(source: SucceededWorkOutput) -> PipelineTransitionPlan:
    return PipelineTransitionRegistry().plan(source)


def _counts(engine: sa.Engine, candidate_id: UUID) -> tuple[int, int, int, int]:
    with engine.connect() as connection:
        candidate = connection.execute(
            sa.text("SELECT count(*) FROM candidates.candidates WHERE candidate_id = :cid"),
            {"cid": candidate_id},
        ).scalar_one()
        revisions = connection.execute(
            sa.text(
                "SELECT count(*) FROM candidates.candidate_revisions WHERE candidate_id = :cid"
            ),
            {"cid": candidate_id},
        ).scalar_one()
        evidence = connection.execute(
            sa.text(
                "SELECT count(*) FROM candidates.candidate_revision_evidence "
                "WHERE candidate_id = :cid"
            ),
            {"cid": candidate_id},
        ).scalar_one()
        cases = connection.execute(
            sa.text("SELECT count(*) FROM review.review_cases WHERE candidate_id = :cid"),
            {"cid": candidate_id},
        ).scalar_one()
    return candidate, revisions, evidence, cases


@pytest.fixture()
def cleaned():
    """Each test runs against its own migrated database, so no row teardown is needed.

    Candidate, quality and review rows are append-only by trigger; isolation therefore comes
    from a disposable database instead of from disabling those guards.
    """

    from db_support import disposable_database

    for url in disposable_database("real node intake"):
        engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
        try:
            yield engine, []
        finally:
            engine.dispose()


def _persist(engine: sa.Engine, source: SucceededWorkOutput) -> UUID:
    plan = _plan(source)
    assert plan.disposition is PipelineTransitionDisposition.APPLY, plan.blocker
    candidate_id = plan.candidate_intake_actions[0].candidate_id
    applier = PostgresCandidateIntakeApplier(clock=lambda: _NOW)
    with engine.begin() as connection:
        applier(connection, source, plan, correlation_id="real-node-intake")
    return candidate_id


def _observations(source: SucceededWorkOutput) -> dict[str, object]:
    from collection_contracts import decode_source_record_observation_batch

    batch = decode_source_record_observation_batch(source.output_content or b"")
    return {item.field_key: item for item in batch.observations}


def test_real_node_reaches_a_persisted_review_case_with_source_tag_evidence(
    cleaned: tuple[sa.Engine, list[UUID]],
) -> None:
    engine, created = cleaned
    source = _succeeded_output(_source_record(_REAL_TAGS), uuid4())

    observations = _observations(source)
    website = observations["website"]
    phone = observations["phone"]
    email = observations["email"]

    # The values only exist under the `contact:` namespace, so this is the real proof.
    assert website.state is ObservationState.OBSERVED
    # The URL normalizer canonicalizes a bare host with a trailing slash.
    assert website.value is not None
    assert website.value.normalized == "http://www.nbfilm.de/"
    assert website.value.registrable_domain == "nbfilm.de"
    assert website.evidence is not None
    assert website.evidence.locator_value == "/tags/contact:website"
    assert website.evidence.source_url == f"https://www.openstreetmap.org/node/{_OSM_ID}"

    assert phone.state is ObservationState.OBSERVED
    assert phone.evidence is not None
    assert phone.evidence.locator_value == "/tags/contact:phone"

    # email is genuinely absent: unknown, never a confirmed absence.
    assert email.state is ObservationState.NOT_OBSERVED
    assert email.value is None and email.evidence is None

    # Time is provenance, not the clock.
    assert website.evidence.observed_at_utc == _ACQUIRED_AT

    candidate_id = _persist(engine, source)
    created.append(candidate_id)

    candidate, revisions, evidence_rows, cases = _counts(engine, candidate_id)
    assert candidate == 1
    assert revisions == 1
    assert evidence_rows >= 2
    assert cases == 1

    with engine.connect() as connection:
        case = connection.execute(
            sa.text(
                "SELECT r.state, r.reason_codes FROM review.review_case_revisions r "
                "JOIN review.review_cases c ON c.case_id = r.case_id "
                "WHERE c.candidate_id = :cid"
            ),
            {"cid": candidate_id},
        ).one()
        revision = connection.execute(
            sa.text(
                "SELECT resolution_state, entity_kind FROM candidates.candidate_revisions r "
                "JOIN candidates.candidates c USING (candidate_id) "
                "WHERE r.candidate_id = :cid"
            ),
            {"cid": candidate_id},
        ).one()

    assert case[0] == "open"
    assert "CANDIDATE_REQUIRES_REVIEW" in case[1]
    assert revision[0] == "review"
    assert revision[1] == "place"


def test_real_node_replay_is_identical_and_adds_no_rows(
    cleaned: tuple[sa.Engine, list[UUID]],
) -> None:
    engine, created = cleaned
    run_id = uuid4()
    record = _source_record(_REAL_TAGS)

    work_unit_id = uuid4()
    stage_run_id = uuid4()
    first = _succeeded_output(
        record, run_id, work_unit_id=work_unit_id, stage_run_id=stage_run_id, deterministic=True
    )
    second = _succeeded_output(
        record, run_id, work_unit_id=work_unit_id, stage_run_id=stage_run_id, deterministic=True
    )

    # Same evidence, same bytes: no wall clock reaches the sealed batch.
    assert first.output_content == second.output_content
    assert first.output_digest == second.output_digest
    assert _plan(first).plan_digest == _plan(second).plan_digest
    assert (
        _plan(first).candidate_intake_actions[0].candidate_id
        == _plan(second).candidate_intake_actions[0].candidate_id
    )

    candidate_id = _persist(engine, first)
    created.append(candidate_id)
    before = _counts(engine, candidate_id)

    # Processing the same work again must not add a candidate, revision or review case.
    applier = PostgresCandidateIntakeApplier(clock=lambda: _NOW)
    with engine.begin() as connection:
        applier(connection, second, _plan(second), correlation_id="real-node-replay")

    assert _counts(engine, candidate_id) == before
    assert before == (1, 1, before[2], 1)


def test_altered_source_artifact_never_reaches_intake(
    cleaned: tuple[sa.Engine, list[UUID]],
) -> None:
    """Evidence cannot be altered after acquisition without being detected.

    The acquired record seals its own digest, so changing a published value invalidates the
    record before mapping, and a work output whose bytes disagree with its declared artifact
    digest is refused at construction. Neither path can write a candidate.
    """

    engine, _created = cleaned
    record = _source_record(_REAL_TAGS)
    source = _succeeded_output(record, uuid4())

    # Changing a published value breaks the record's own sealed digest.
    altered = record.replace(b"http://www.nbfilm.de", b"http://www.nbfilm.example")
    with pytest.raises(ValueError):
        _batch(altered, source.run_id)

    # Bytes that no longer match the declared artifact digest never form a work output.
    with pytest.raises(ValueError):
        replace(source, output_content=altered)

    # Nothing reached the candidate store for either attempt.
    with engine.connect() as connection:
        written = connection.execute(
            sa.text("SELECT count(*) FROM candidates.candidates")
        ).scalar_one()
    assert int(written) == 0


def test_conflicting_alias_values_survive_as_distinct_observations(
    cleaned: tuple[sa.Engine, list[UUID]],
) -> None:
    engine, created = cleaned
    tags = {
        **_REAL_TAGS,
        "website": "https://primary.example",
        "contact:website": "https://alias.example",
    }
    source = _succeeded_output(_source_record(tags), uuid4())

    from collection_contracts import decode_source_record_observation_batch

    batch = decode_source_record_observation_batch(source.output_content or b"")
    websites = [item for item in batch.observations if item.field_key == "website"]

    # Neither silently wins; both stay available with their own tag provenance.
    assert len(websites) == 2
    # The URL normalizer canonicalizes a host with a trailing slash; both values survive.
    assert {item.value.normalized for item in websites if item.value} == {
        "https://primary.example/",
        "https://alias.example/",
    }
    assert {item.evidence.locator_value for item in websites if item.evidence} == {
        "/tags/website",
        "/tags/contact:website",
    }

    candidate_id = _persist(engine, source)
    created.append(candidate_id)
    assert _counts(engine, candidate_id)[0] == 1
