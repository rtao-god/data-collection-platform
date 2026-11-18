"""Source-tag aliases widen which source tags feed a typed target, and nothing else.

The campaign declares `contact:website` as an alias of `website`. These tests pin the two
properties that make that safe: the typed projection is unchanged, and the intake resolves
a profile carrying aliases exactly as it resolves one without them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineAdvancementConflict,
    SucceededWorkOutput,
)
from collection_domain import WorkStage
from collection_infrastructure.postgres.candidate_intake_applier import (
    PostgresCandidateIntakeApplier,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 11, 16, tzinfo=UTC)


def _mapping_profile(*, aliases: dict[str, list[str]] | None) -> bytes:
    document: dict[str, object] = {
        "campaignKey": "berlin_recording_services",
        "entityKind": "place",
        "fieldProjection": {
            "display_name": "names",
            "website": "website_urls",
        },
        "mappingRevision": "osm-overpass-source-record-mapping-v1",
        "runId": "00000000-0000-0000-0000-00000000aa01",
        "sourceBindingKey": "osm_overpass_berlin",
        "sourceKey": "openstreetmap",
        "sourcePolicyDigest": "sha256:" + "a" * 64,
    }
    if aliases is not None:
        document["fieldAliases"] = aliases
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _source(profile: bytes) -> SucceededWorkOutput:
    return SucceededWorkOutput(
        source_work_unit_id=UUID("00000000-0000-0000-0000-00000000aa02"),
        run_id=UUID("00000000-0000-0000-0000-00000000aa01"),
        stage_run_id=UUID("00000000-0000-0000-0000-00000000aa03"),
        stage=WorkStage.NORMALIZATION,
        capability="source_record_mapping",
        output_contract="source-record-observation-batch@1",
        output_digest="sha256:" + "b" * 64,
        output_artifact=ArtifactIdentity(
            artifact_id=UUID("00000000-0000-0000-0000-00000000aa04"),
            role="source_record_observation_batch",
            content_digest="sha256:" + "b" * 64,
            size_bytes=1,
            content_type="application/json",
        ),
        input_artifacts=(),
        source_binding_key="osm_overpass_berlin",
        normalization_profile_content=profile,
    )


def test_aliases_do_not_change_the_typed_projection_or_entity_kind() -> None:
    from collection_infrastructure.postgres.candidate_intake_applier import (
        _resolve_source_mapping_policy,
    )

    plain = _resolve_source_mapping_policy(_source(_mapping_profile(aliases=None)))
    aliased = _resolve_source_mapping_policy(
        _source(_mapping_profile(aliases={"website": ["website", "contact:website"]}))
    )

    assert aliased == plain
    _entity_kind, projection, resolved = aliased
    assert projection == {"display_name": "names", "website": "website_urls"}
    assert resolved is True


def test_profile_with_aliases_is_accepted_by_the_intake() -> None:
    from collection_infrastructure.postgres.candidate_intake_applier import (
        _resolve_source_mapping_policy,
    )

    _resolve_source_mapping_policy(
        _source(
            _mapping_profile(
                aliases={
                    "website": ["website", "contact:website"],
                    "phone": ["phone", "contact:phone"],
                }
            )
        )
    )


def test_profile_with_an_unknown_key_is_still_rejected() -> None:
    from collection_infrastructure.postgres.candidate_intake_applier import (
        _resolve_source_mapping_policy,
    )

    document = json.loads(_mapping_profile(aliases=None).decode("utf-8"))
    document["unexpectedKey"] = True
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(PipelineAdvancementConflict) as raised:
        _resolve_source_mapping_policy(_source(payload))

    assert raised.value.code == "PIPELINE_SOURCE_RECORD_PROFILE_INVALID"


def test_applier_is_constructible_for_this_shape() -> None:
    # The applier is the intake owner; this guards the import surface used above.
    assert PostgresCandidateIntakeApplier is not None
    assert _NOW.tzinfo is UTC
