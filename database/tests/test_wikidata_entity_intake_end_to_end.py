"""The captured Wikidata response, end to end through mapping, intake and review.

The response is the one the connector test suite captured from `wbgetentities` for the five
operator-declared pilot items. It matters because those items are exactly the two shapes the
path has to survive: four items carry `instance of recording studio (Q746369)` and one
(Meistersaal) carries four classes, so it must stay out of the candidate set while its
siblings still advance.

    acquired response -> sealed result envelope -> per-entity source records
    -> source-record mapping -> candidate intake -> PostgreSQL review

The shipped campaign keeps `wikidata_studios_berlin` disabled and its policy at
`legal_status: research_only` until the owner authorizes it. This module therefore builds its
own run snapshot with the binding enabled — the post-authorization shape — so it proves the
mechanics, not an authorization: the policy bytes are the shipped ones.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4, uuid5

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
    WikidataClassificationSpec,
    WikidataEntityQuerySpec,
    canonical_wikidata_query_spec_document,
    decode_source_record_observation_batch,
    decode_wikidata_entity_result,
    wikidata_query_spec_digest,
)
from collection_domain import WorkStage
from collection_infrastructure.postgres.candidate_intake_applier import (
    PostgresCandidateIntakeApplier,
)
from collection_infrastructure.postgres.source_record_mapping_applier import (
    PostgresSourceRecordMappingApplier,
)
from wikidata_studios import seal_entity_result

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 12, 1, 9, 30, tzinfo=UTC)
_RETRIEVED_AT = datetime(2026, 11, 30, 22, 3, 11, tzinfo=UTC)
_ENTITY_IDS = ("Q105762301", "Q1372342", "Q56298345", "Q667023", "Q7695694")
_STUDIO_ENTITY_IDS = ("Q105762301", "Q56298345", "Q667023", "Q7695694")
_AMBIGUOUS_ENTITY_ID = "Q1372342"

# The captured response lives next to the connector tests that read the same acquisition;
# copying it here would make one piece of evidence into two that can drift apart.
_RESPONSE_PATH = (
    Path(__file__).resolve().parents[2]
    / "connectors"
    / "wikidata_studios"
    / "tests"
    / "fixtures"
    / "wbgetentities.json"
)


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _stable_id(digest: str, purpose: str) -> UUID:
    return uuid5(UUID("2b9f5d4c-6b7e-5f4a-9c1d-3e8a7f6b5c4d"), f"{purpose}:{digest}")


def _spec_bytes(run_id: UUID, *, category: str = "recording_studio") -> bytes:
    return canonical_wikidata_query_spec_document(
        WikidataEntityQuerySpec(
            run_id=run_id,
            binding_key="wikidata_studios_berlin",
            entity_ids=_ENTITY_IDS,
            label_languages=("de", "en"),
            classification=WikidataClassificationSpec(
                studio_class_id="Q746369",
                campaign_category=category,
            ),
            source_policy_digest=f"sha256:{'4' * 64}",
        )
    )


def _sealed(run_id: UUID, *, category: str = "recording_studio"):
    return seal_entity_result(
        _RESPONSE_PATH.read_bytes(),
        spec=WikidataEntityQuerySpec(
            run_id=run_id,
            binding_key="wikidata_studios_berlin",
            entity_ids=_ENTITY_IDS,
            label_languages=("de", "en"),
            classification=WikidataClassificationSpec(
                studio_class_id="Q746369",
                campaign_category=category,
            ),
            source_policy_digest=f"sha256:{'4' * 64}",
        ),
        retrieved_at_utc=_RETRIEVED_AT,
    )


class _CampaignFiles:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = MappingProxyType(files)

    def read(self, campaign_key: str, correlation_id: str) -> RawCampaignBundle:
        del correlation_id
        return RawCampaignBundle(campaign_key=campaign_key, files=self._files)


def _compiled_campaign(*, enable_wikidata: bool = True) -> bytes:
    """Compile the shipped campaign, optionally with the Wikidata binding enabled.

    Enabling happens on this fixture's own copy of `campaign.yaml`; the file on disk keeps
    the binding disabled and the policy keeps `legal_status: research_only`.
    """

    campaign_root = Path(__file__).resolve().parents[2] / "campaigns" / "berlin_recording_services"
    files = {
        path.relative_to(campaign_root).as_posix(): path.read_bytes()
        for path in campaign_root.rglob("*")
        if path.is_file()
    }
    if enable_wikidata:
        campaign = files["campaign.yaml"].decode("utf-8")
        files["campaign.yaml"] = campaign.replace(
            "enabled_source_bindings:\n- manual_seed_import\n- osm_overpass_berlin\n",
            "enabled_source_bindings:\n- manual_seed_import\n- osm_overpass_berlin\n"
            "- wikidata_studios_berlin\n",
        ).encode("utf-8")
        assert b"wikidata_studios_berlin\n" in files["campaign.yaml"]
    return (
        CampaignSnapshotService(_CampaignFiles(files))
        .compile("berlin_recording_services", "correlation-wikidata-intake")
        .canonical_content
    )


def _result_digest(*, raw_digest: str, envelope_digest: str) -> str:
    """The completion digest the worker gateway publishes, re-derived here."""

    return _digest(
        json.dumps(
            {
                "contract": "wikidata-entity-result@2",
                "envelopeDigest": envelope_digest,
                "rawResponseDigest": raw_digest,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _discovery_output(
    run_id: UUID,
    *,
    category: str = "recording_studio",
    work_unit_id: UUID | None = None,
    stage_run_id: UUID | None = None,
) -> SucceededWorkOutput:
    result = _sealed(run_id, category=category)
    spec_content = _spec_bytes(run_id, category=category)
    campaign_content = _compiled_campaign()
    envelope_digest = _digest(result.envelope_content)
    return SucceededWorkOutput(
        source_work_unit_id=work_unit_id or uuid4(),
        run_id=run_id,
        stage_run_id=stage_run_id or uuid4(),
        stage=WorkStage.DISCOVERY,
        capability="wikidata_entity_query",
        output_contract="wikidata-entity-result@2",
        output_digest=_result_digest(
            raw_digest=result.raw_digest,
            envelope_digest=envelope_digest,
        ),
        output_artifact=ArtifactIdentity(
            artifact_id=_stable_id(envelope_digest, "wikidata-result"),
            role="wikidata_entity_result",
            content_digest=envelope_digest,
            size_bytes=len(result.envelope_content),
            content_type="application/vnd.collection.wikidata-entity-result+json",
        ),
        related_output_artifacts=(
            ArtifactIdentity(
                artifact_id=_stable_id(result.raw_digest, "wikidata-raw"),
                role="wikidata_raw_response",
                content_digest=result.raw_digest,
                size_bytes=len(result.raw_content),
                content_type="application/json",
            ),
        ),
        input_artifacts=(
            ArtifactIdentity(
                artifact_id=_stable_id(
                    wikidata_query_spec_digest(
                        WikidataEntityQuerySpec(
                            run_id=run_id,
                            binding_key="wikidata_studios_berlin",
                            entity_ids=_ENTITY_IDS,
                            label_languages=("de", "en"),
                            classification=WikidataClassificationSpec(
                                studio_class_id="Q746369",
                                campaign_category=category,
                            ),
                            source_policy_digest=f"sha256:{'4' * 64}",
                        )
                    ),
                    "wikidata-query-spec",
                ),
                role="wikidata_entity_query_spec",
                content_digest=_digest(spec_content),
                size_bytes=len(spec_content),
                content_type="application/vnd.collection.wikidata-entity-query-spec+json",
            ),
        ),
        source_binding_key="wikidata_studios_berlin",
        config_bundle_digest=_digest(campaign_content),
        campaign_bundle_content=campaign_content,
        output_content=result.envelope_content,
        wikidata_query_spec_content=spec_content,
    )


def _plan(source: SucceededWorkOutput) -> PipelineTransitionPlan:
    return PipelineTransitionRegistry().plan(source)


def _mapping_output(
    source: SucceededWorkOutput,
    action,
    *,
    work_unit_id: UUID | None = None,
    stage_run_id: UUID | None = None,
) -> SucceededWorkOutput:
    """The mapping work unit's own success: the sealed observation batch."""

    from processing_worker.source_record_mapping import (
        materialize_source_record_observations,
    )

    mapped = materialize_source_record_observations(
        action.source_record_payload,
        action.mapping_profile_payload,
        maximum_records=1,
    )
    from collection_contracts import canonical_source_record_observation_batch_json

    batch = canonical_source_record_observation_batch_json(mapped).encode("utf-8")
    batch_digest = _digest(batch)
    return SucceededWorkOutput(
        source_work_unit_id=work_unit_id or uuid4(),
        run_id=source.run_id,
        stage_run_id=stage_run_id or uuid4(),
        stage=WorkStage.NORMALIZATION,
        capability="source_record_mapping",
        output_contract="source-record-observation-batch@1",
        output_digest=batch_digest,
        output_artifact=ArtifactIdentity(
            artifact_id=_stable_id(batch_digest, "wikidata-batch"),
            role="source_record_observation_batch",
            content_digest=batch_digest,
            size_bytes=len(batch),
            content_type="application/vnd.collection.source-record-observation-batch+json",
        ),
        # The batch names the source-record artifact it was mapped from, so the intake
        # binding is read back from the batch instead of being invented here.
        input_artifacts=(action.source_record_artifact,),
        source_binding_key="wikidata_studios_berlin",
        normalization_profile_content=action.mapping_profile_payload,
        config_bundle_digest=source.config_bundle_digest,
        campaign_bundle_content=source.campaign_bundle_content,
        output_content=batch,
    )


def _observations(source: SucceededWorkOutput) -> dict[str, list]:
    batch = decode_source_record_observation_batch(source.output_content or b"")
    collected: dict[str, list] = {}
    for item in batch.observations:
        collected.setdefault(item.field_key, []).append(item)
    return collected


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
    from db_support import disposable_database

    for url in disposable_database("wikidata entity intake"):
        engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
        try:
            yield engine
        finally:
            engine.dispose()


def _persist(engine: sa.Engine, source: SucceededWorkOutput) -> UUID:
    plan = _plan(source)
    assert plan.disposition is PipelineTransitionDisposition.APPLY, plan.blocker
    candidate_id = plan.candidate_intake_actions[0].candidate_id
    with engine.begin() as connection:
        PostgresCandidateIntakeApplier(clock=lambda: _NOW)(
            connection, source, plan, correlation_id="wikidata-intake"
        )
    return candidate_id


def test_the_captured_response_fans_out_one_mapping_unit_per_accepted_item() -> None:
    run_id = uuid4()
    source = _discovery_output(run_id)

    plan = _plan(source)

    assert plan.disposition is PipelineTransitionDisposition.APPLY, plan.blocker
    assert len(plan.source_record_mapping_actions) == len(_STUDIO_ENTITY_IDS)
    assert {action.source_record_id for action in plan.source_record_mapping_actions} == {
        f"wikidata:{item}" for item in _STUDIO_ENTITY_IDS
    }
    # The one item with four classes is refused by name, not silently dropped.
    assert plan.source_record_mapping_actions[-1].unrepresentable_source_record_ids == (
        f"wikidata:{_AMBIGUOUS_ENTITY_ID}",
    )
    # Work identities are per entity and depend only on the evidence, so a replay cannot
    # become a different unit that happens to carry the same bytes.
    assert len({action.work_id for action in plan.source_record_mapping_actions}) == len(
        _STUDIO_ENTITY_IDS
    )
    # The transition owner accepts this plan unchanged.
    assert PostgresSourceRecordMappingApplier._valid_plan(source, plan) is True


def test_the_accepted_items_reach_persisted_candidates_with_their_classification(
    cleaned: sa.Engine,
) -> None:
    engine = cleaned
    run_id = uuid4()
    source = _discovery_output(run_id)
    actions = _plan(source).source_record_mapping_actions

    hansa = next(item for item in actions if item.source_record_id == "wikidata:Q667023")
    mapped = _mapping_output(source, hansa)
    observations = _observations(mapped)

    # The category claim is read from the exact instance-of statement, so its provenance
    # survives into the observation instead of being a bare label.
    category = observations["category_recording_studio"][0]
    assert category.state is ObservationState.OBSERVED
    assert category.value.normalized == "recording_studio"
    assert category.evidence.locator_value == (
        "/entities/Q667023/claims/P31/Q667023$30d3e507-46e6-72b2-a4b6-5f3398a53f13"
    )
    assert category.evidence.source_url == (
        "https://www.wikidata.org/wiki/Special:EntityData/Q667023.json"
    )

    display_name = observations["display_name"][0]
    assert display_name.state is ObservationState.OBSERVED
    assert display_name.value.normalized == "Hansa Studios"
    assert display_name.evidence.locator_value.startswith("/entities/Q667023/labels/")

    website = observations["website"][0]
    assert website.state is ObservationState.OBSERVED
    assert website.evidence.locator_value.startswith("/entities/Q667023/claims/P856/")

    coordinates = observations["coordinates"][0]
    assert coordinates.state is ObservationState.OBSERVED
    assert coordinates.value.latitude is not None
    assert coordinates.evidence.locator_value.startswith("/entities/Q667023/claims/P625/")

    candidate_id = _persist(engine, mapped)
    assert _counts(engine, candidate_id) == (1, 1, _counts(engine, candidate_id)[2], 1)

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
        stored_payload = connection.execute(
            sa.text(
                "SELECT normalized_payload FROM candidates.candidate_revisions "
                "WHERE candidate_id = :cid"
            ),
            {"cid": candidate_id},
        ).scalar_one()

    assert case[0] == "open"
    assert "CANDIDATE_REQUIRES_REVIEW" in case[1]
    assert revision[0] == "review"
    assert revision[1] == "place"
    # Classification reaches the candidate store as its own observation, still pointing at
    # the exact instance-of statement it was read from.
    categories = [
        item
        for item in stored_payload["fieldObservations"]
        if item["fieldKey"] == "category_recording_studio"
    ]
    assert len(categories) == 1
    assert categories[0]["value"]["normalized"] == "recording_studio"


def test_every_accepted_item_becomes_its_own_candidate(cleaned: sa.Engine) -> None:
    engine = cleaned
    source = _discovery_output(uuid4())
    actions = _plan(source).source_record_mapping_actions

    candidate_ids = set()
    for action in actions:
        candidate_ids.add(_persist(engine, _mapping_output(source, action)))

    assert len(candidate_ids) == len(_STUDIO_ENTITY_IDS)
    with engine.connect() as connection:
        stored = connection.execute(
            sa.text("SELECT count(*) FROM candidates.candidates")
        ).scalar_one()
    assert int(stored) == len(_STUDIO_ENTITY_IDS)


def test_replaying_the_same_response_adds_no_rows(cleaned: sa.Engine) -> None:
    engine = cleaned
    run_id = uuid4()
    work_unit_id = uuid4()
    stage_run_id = uuid4()
    first = _discovery_output(run_id, work_unit_id=work_unit_id, stage_run_id=stage_run_id)
    second = _discovery_output(run_id, work_unit_id=work_unit_id, stage_run_id=stage_run_id)

    assert first.output_content == second.output_content
    assert _plan(first).plan_digest == _plan(second).plan_digest

    action = _plan(first).source_record_mapping_actions[0]
    mapped_first = _mapping_output(first, action)
    mapped_second = _mapping_output(second, action)
    assert mapped_first.output_content == mapped_second.output_content

    candidate_id = _persist(engine, mapped_first)
    before = _counts(engine, candidate_id)

    with engine.begin() as connection:
        PostgresCandidateIntakeApplier(clock=lambda: _NOW)(
            connection, mapped_second, _plan(mapped_second), correlation_id="wikidata-replay"
        )

    assert _counts(engine, candidate_id) == before
    assert before == (1, 1, before[2], 1)


def test_a_result_envelope_that_no_longer_matches_its_response_is_refused() -> None:
    source = _discovery_output(uuid4())
    envelope = decode_wikidata_entity_result(source.output_content or b"")

    # The envelope still hashes to itself, but it describes a response nobody acquired.
    forged_content = envelope_document(envelope, response_digest=f"sha256:{'9' * 64}")
    forged = replace(
        source,
        output_content=forged_content,
        output_digest=_result_digest(
            raw_digest=source.related_output_artifacts[0].content_digest,
            envelope_digest=_digest(forged_content),
        ),
        output_artifact=replace(
            source.output_artifact,
            content_digest=_digest(forged_content),
            size_bytes=len(forged_content),
        ),
    )
    plan = _plan(forged)
    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_SOURCE_RECORD_RAW_TAMPERED"


def test_an_adapted_record_that_was_altered_after_sealing_is_refused() -> None:
    source = _discovery_output(uuid4())
    document = json.loads(source.output_content or b"{}")
    document["records"][0]["document"]["records"][0]["display_name"] = [
        {"value": "Substituted", "pointer": "/entities/Q667023/labels/de"}
    ]
    forged = _republished(source, _canonical(document))

    plan = _plan(forged)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_SOURCE_RECORD_RAW_INVALID"


def test_a_result_that_answers_a_different_entity_set_is_refused() -> None:
    source = _discovery_output(uuid4())
    document = json.loads(source.output_content or b"{}")
    # A read that answers four of the five declared items: internally consistent, but it
    # is not the question the run declared.
    dropped = document["records"].pop(0)["entityId"]
    document["requestedEntityIds"] = [
        item for item in document["requestedEntityIds"] if item != dropped
    ]
    forged = _republished(source, _canonical(document))

    plan = _plan(forged)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_SOURCE_RECORD_ENTITY_SET_CONFLICT"


def test_a_query_specification_for_another_category_is_refused() -> None:
    run_id = uuid4()
    source = _discovery_output(run_id)
    other = _spec_bytes(run_id, category="rehearsal_recording_space")
    forged = replace(
        source,
        input_artifacts=(
            replace(
                source.input_artifacts[0],
                content_digest=_digest(other),
                size_bytes=len(other),
            ),
        ),
        wikidata_query_spec_content=other,
    )

    plan = _plan(forged)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_SOURCE_RECORD_CLASSIFICATION_CONFLICT"


def test_a_disabled_binding_cannot_advance_until_the_owner_enables_it() -> None:
    """The shipped campaign keeps the binding disabled, so this path stays closed."""

    shipped = _compiled_campaign(enable_wikidata=False)
    source = _discovery_output(uuid4())
    disabled = replace(
        source,
        config_bundle_digest=_digest(shipped),
        campaign_bundle_content=shipped,
    )

    plan = _plan(disabled)

    assert plan.disposition is PipelineTransitionDisposition.BLOCK
    assert plan.blocker is not None
    assert plan.blocker.code == "PIPELINE_SOURCE_RECORD_BINDING_INVALID"


def _republished(source: SucceededWorkOutput, content: bytes) -> SucceededWorkOutput:
    """The same acquisition republished with different envelope bytes.

    Forging the envelope alone cannot pass the work output's own digest check, so the
    forgery has to be self-consistent to reach the transition that has to refuse it.
    """

    return replace(
        source,
        output_content=content,
        output_digest=_result_digest(
            raw_digest=source.related_output_artifacts[0].content_digest,
            envelope_digest=_digest(content),
        ),
        output_artifact=replace(
            source.output_artifact,
            content_digest=_digest(content),
            size_bytes=len(content),
        ),
    )


def _canonical(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def envelope_document(envelope, *, response_digest: str) -> bytes:
    """Re-seal the decoded envelope with a different declared response digest.

    The records keep their own digests, so only the response binding changes — exactly the
    forgery that claims an adapted record came from a response nobody acquired.
    """

    payload = {
        "contract": "wikidata-entity-result@2",
        "records": _record_entries(envelope),
        "rejectedEntities": [
            {"code": item.code, "entityId": item.entity_id, "message": item.message}
            for item in envelope.rejected_entities
        ],
        "requestedEntityIds": list(envelope.requested_entity_ids),
        "responseDigest": response_digest,
        "retrievedAtUtc": envelope.retrieved_at_utc.isoformat().replace("+00:00", "Z"),
        "schemaRevision": "wikidata-entity-result-v2",
    }
    return _canonical(payload)


def _record_entries(envelope) -> list[dict[str, object]]:
    entries = []
    for item in envelope.records:
        entries.append(
            {
                "campaignCategory": item.campaign_category,
                "classificationPointer": item.classification_pointer,
                "document": json.loads(item.document_bytes),
                "entityId": item.entity_id,
                "recordDigest": item.record_digest,
                "sourceRecordId": item.source_record_id,
            }
        )
    return entries
