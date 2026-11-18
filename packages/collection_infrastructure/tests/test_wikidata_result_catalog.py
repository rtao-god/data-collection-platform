"""The collector's verdict on a persisted Wikidata acquisition.

Before the planner is allowed to schedule mapping work, the succeeded-work catalog re-reads
the three artifacts the acquisition published — the untouched response, the result envelope
and the declared query specification — and proves they belong together. These tests hold that
check to what it claims, because a consumer that trusts a mismatched trio would map records
from a response nobody acquired.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import pytest

from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineAdvancementConflict,
)
from collection_contracts import (
    WikidataClassificationSpec,
    WikidataEntityQuerySpec,
    build_wikidata_entity_result,
    canonical_wikidata_query_spec_document,
    canonical_wikidata_source_record_document,
    decode_wikidata_query_spec,
    wikidata_query_spec_digest,
)
from collection_infrastructure.postgres.succeeded_work_catalog import (
    PostgresSucceededWorkCatalog,
    _BoundArtifact,
)

_WORK_ID = UUID("00000000-0000-0000-0000-000000000a01")
_RAW_ID = UUID("00000000-0000-0000-0000-000000000a02")
_ENVELOPE_ID = UUID("00000000-0000-0000-0000-000000000a03")
_SPEC_ID = UUID("00000000-0000-0000-0000-000000000a04")
_RUN_ID = UUID("77777777-7777-5777-8777-777777777777")
_RETRIEVED_AT = datetime(2026, 11, 30, 22, 3, 11, tzinfo=UTC)
_STATEMENT_ID = "Q667023$30d3e507-46e6-72b2-a4b6-5f3398a53f13"

_RAW = json.dumps(
    {
        "entities": {
            "Q667023": {
                "type": "item",
                "id": "Q667023",
                "lastrevid": 2465661990,
                "labels": {"de": {"language": "de", "value": "Hansa Studios"}},
            }
        },
        "success": 1,
    },
    sort_keys=True,
).encode("utf-8")


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _spec(*, category: str = "recording_studio") -> bytes:
    return canonical_wikidata_query_spec_document(
        WikidataEntityQuerySpec(
            run_id=_RUN_ID,
            binding_key="wikidata_studios_berlin",
            entity_ids=("Q667023",),
            label_languages=("de", "en"),
            classification=WikidataClassificationSpec(
                studio_class_id="Q746369",
                campaign_category=category,
            ),
            source_policy_digest=f"sha256:{'4' * 64}",
        )
    )


def _record_document() -> dict:
    document: dict = {
        "artifactRecordId": str(UUID(bytes=sha256(b"Q667023").digest()[:16], version=5)),
        "contract": "wikidata-source-record@1",
        "sourceRecordId": "wikidata:Q667023",
        "entityRevisionId": 2465661990,
        "observedAtUtc": _RETRIEVED_AT.isoformat().replace("+00:00", "Z"),
        "datasetRelease": "wikidata-entity-revision:2465661990",
        "runId": str(_RUN_ID),
        "sourceUrl": "https://www.wikidata.org/wiki/Special:EntityData/Q667023.json",
        "attribution": "Wikidata (CC0 1.0)",
        "fields": ["display_name", "category_recording_studio"],
        "records": [
            {
                "sourceRecordId": "wikidata:Q667023",
                "display_name": [
                    {"value": "Hansa Studios", "pointer": "/entities/Q667023/labels/de"}
                ],
                "category_recording_studio": [
                    {
                        "value": "recording_studio",
                        "pointer": f"/entities/Q667023/claims/P31/{_STATEMENT_ID}",
                    }
                ],
            }
        ],
    }
    document["artifactDigest"] = _digest(canonical_wikidata_source_record_document(document))
    return document


def _envelope(*, response_digest: str | None = None, records: list[dict] | None = None) -> bytes:
    return build_wikidata_entity_result(
        requested_entity_ids=("Q667023",),
        retrieved_at_utc=_RETRIEVED_AT,
        response_digest=response_digest or _digest(_RAW),
        records=records if records is not None else [_record_document()],
        rejected=[],
    )


def _result_digest(*, envelope: bytes) -> str:
    return _digest(
        json.dumps(
            {
                "contract": "wikidata-entity-result@2",
                "envelopeDigest": _digest(envelope),
                "rawResponseDigest": _digest(_RAW),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _artifact(artifact_id: UUID, role: str, content: bytes, content_type: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_id=artifact_id,
        role=role,
        content_digest=_digest(content),
        size_bytes=len(content),
        content_type=content_type,
    )


class _Reader(PostgresSucceededWorkCatalog):
    def __init__(self, contents: dict[str, bytes]) -> None:
        super().__init__(object())  # type: ignore[arg-type]
        self._contents = contents

    def _read_artifact_content(
        self,
        _connection: object,
        identity: ArtifactIdentity,
        _source_work_unit_id: UUID,
        *,
        maximum_bytes: int,
    ) -> bytes:
        assert maximum_bytes > 0
        return self._contents[identity.role]


def _outputs(envelope: bytes, *, raw: bytes = _RAW) -> list[_BoundArtifact]:
    return [
        _BoundArtifact(0, _artifact(_RAW_ID, "wikidata_raw_response", raw, "application/json")),
        _BoundArtifact(
            1,
            _artifact(
                _ENVELOPE_ID,
                "wikidata_entity_result",
                envelope,
                "application/vnd.collection.wikidata-entity-result+json",
            ),
        ),
    ]


def _inputs(spec: bytes) -> list[_BoundArtifact]:
    return [
        _BoundArtifact(
            0,
            _artifact(
                _SPEC_ID,
                "wikidata_entity_query_spec",
                spec,
                "application/vnd.collection.wikidata-entity-query-spec+json",
            ),
        )
    ]


def _read(
    *,
    envelope: bytes | None = None,
    spec: bytes | None = None,
    outputs: list[_BoundArtifact] | None = None,
    inputs: list[_BoundArtifact] | None = None,
    output_digest: str | None = None,
    contents: dict[str, bytes] | None = None,
):
    envelope = envelope if envelope is not None else _envelope()
    spec = spec if spec is not None else _spec()
    reader = _Reader(
        contents
        if contents is not None
        else {
            "wikidata_raw_response": _RAW,
            "wikidata_entity_result": envelope,
            "wikidata_entity_query_spec": spec,
        }
    )
    return reader._read_wikidata_result(
        object(),  # type: ignore[arg-type]
        _WORK_ID,
        outputs=outputs if outputs is not None else _outputs(envelope),
        inputs=inputs if inputs is not None else _inputs(spec),
        output_digest=output_digest
        if output_digest is not None
        else _result_digest(envelope=envelope),
    )


def test_the_exact_acquisition_trio_is_accepted() -> None:
    raw_artifact, envelope_artifact, envelope_content, spec_content = _read()

    assert raw_artifact.artifact_id == _RAW_ID
    assert envelope_artifact.artifact_id == _ENVELOPE_ID
    assert envelope_content == _envelope()
    assert decode_wikidata_query_spec(spec_content).entity_ids == ("Q667023",)


def test_an_envelope_that_describes_another_response_is_refused() -> None:
    other_response = _RAW + b" "
    envelope = _envelope(response_digest=_digest(other_response))

    with pytest.raises(PipelineAdvancementConflict) as error:
        _read(envelope=envelope)

    assert error.value.code == "PIPELINE_WIKIDATA_RAW_DIGEST_MISMATCH"


def test_a_completion_digest_from_another_acquisition_is_refused() -> None:
    with pytest.raises(PipelineAdvancementConflict) as error:
        _read(output_digest=f"sha256:{'7' * 64}")

    assert error.value.code == "PIPELINE_WIKIDATA_RESULT_DIGEST_MISMATCH"


def test_a_query_specification_for_another_entity_set_is_refused() -> None:
    spec = canonical_wikidata_query_spec_document(
        WikidataEntityQuerySpec(
            run_id=_RUN_ID,
            binding_key="wikidata_studios_berlin",
            entity_ids=("Q667023", "Q7695694"),
            label_languages=("de", "en"),
            classification=WikidataClassificationSpec(
                studio_class_id="Q746369",
                campaign_category="recording_studio",
            ),
            source_policy_digest=f"sha256:{'4' * 64}",
        )
    )

    with pytest.raises(PipelineAdvancementConflict) as error:
        _read(spec=spec)

    assert error.value.code == "PIPELINE_WIKIDATA_ENTITY_SET_CONFLICT"


def test_a_query_specification_that_is_not_the_published_bytes_is_refused() -> None:
    spec = _spec() + b"\n"

    with pytest.raises(PipelineAdvancementConflict) as error:
        _read(spec=spec)

    assert error.value.code == "PIPELINE_WIKIDATA_QUERY_SPEC_INVALID"


def test_an_acquisition_without_its_declared_query_is_refused() -> None:
    with pytest.raises(PipelineAdvancementConflict) as error:
        _read(inputs=[])

    assert error.value.code == "PIPELINE_WIKIDATA_QUERY_BINDINGS_INVALID"


def test_a_success_that_binds_an_extra_envelope_is_refused() -> None:
    envelope = _envelope()
    duplicated = _outputs(envelope) + _outputs(envelope)

    with pytest.raises(PipelineAdvancementConflict) as error:
        _read(outputs=duplicated)

    assert error.value.code == "PIPELINE_WIKIDATA_OUTPUT_BINDINGS_INVALID"


def test_an_envelope_whose_record_no_longer_matches_its_digest_is_refused() -> None:
    document = json.loads(_envelope())
    document["records"][0]["document"]["records"][0]["display_name"][0]["value"] = "Substituted"
    tampered = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    with pytest.raises(PipelineAdvancementConflict) as error:
        _read(envelope=tampered)

    assert error.value.code == "PIPELINE_WIKIDATA_RESULT_INVALID"


def test_the_accepted_records_keep_the_classification_pointer_they_were_read_from() -> None:
    _raw, _envelope_artifact, envelope_content, _spec = _read()

    document = json.loads(envelope_content)
    record = document["records"][0]
    assert record["sourceRecordId"] == "wikidata:Q667023"
    assert record["classificationPointer"] == f"/entities/Q667023/claims/P31/{_STATEMENT_ID}"
    assert record["campaignCategory"] == "recording_studio"


def test_the_query_specification_digest_is_the_one_the_work_declared() -> None:
    spec = decode_wikidata_query_spec(_spec())

    assert wikidata_query_spec_digest(spec) == _digest(_spec())
