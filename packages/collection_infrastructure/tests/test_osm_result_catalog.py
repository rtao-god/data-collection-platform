from __future__ import annotations

import json
from hashlib import sha256
from uuid import UUID

import pytest

from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    PipelineAdvancementConflict,
)
from collection_infrastructure.postgres.succeeded_work_catalog import (
    PostgresSucceededWorkCatalog,
    _BoundArtifact,
    _osm_result_digest,
    decode_osm_observation_batch,
)

_WORK_ID = UUID("00000000-0000-0000-0000-000000000901")
_RAW_ID = UUID("00000000-0000-0000-0000-000000000902")
_OBS_ID = UUID("00000000-0000-0000-0000-000000000903")
_QUERY_ID = UUID("00000000-0000-0000-0000-000000000904")

_RAW_CONTENT = b'{"version":0.6,"elements":[]}'
_RAW_DIGEST = f"sha256:{sha256(_RAW_CONTENT).hexdigest()}"
_QUERY_DIGEST = f"sha256:{'3' * 64}"


def _observations_document(
    *,
    attribution: str = "\u00a9 OpenStreetMap contributors",
    observation_entries: list[dict[str, object]] | None = None,
    response_digest: str = _RAW_DIGEST,
) -> bytes:
    document = {
        "attribution": attribution,
        "generator": "Overpass API",
        "observations": observation_entries
        if observation_entries is not None
        else [
            {
                "address": {
                    "city": "Berlin",
                    "country": "DE",
                    "houseNumber": "1",
                    "postcode": "10115",
                    "street": "Beispielweg",
                },
                "elementType": "node",
                "latitude": 52.5320,
                "longitude": 13.3849,
                "osmId": 123456789,
                "sourceUrl": "https://www.openstreetmap.org/node/123456789",
                "tags": {"amenity": "studio", "name": "Beispiel Studio"},
            }
        ],
        "osmBaseTimestampUtc": "2026-11-15T00:00:00Z",
        "queryDigest": _QUERY_DIGEST,
        "responseDigest": response_digest,
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _artifact(artifact_id: UUID, role: str, digest: str, content_type: str) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_id=artifact_id,
        role=role,
        content_digest=digest,
        size_bytes=1,
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


def _bindings(*, observations_content: bytes | None = None) -> tuple[list[_BoundArtifact], bytes]:
    observations = (
        observations_content if observations_content is not None else _observations_document()
    )
    outputs = [
        _BoundArtifact(
            0,
            _artifact(_RAW_ID, "osm_raw_response", _RAW_DIGEST, "application/json"),
        ),
        _BoundArtifact(
            1,
            _artifact(
                _OBS_ID,
                "osm_observations",
                f"sha256:{sha256(observations).hexdigest()}",
                "application/vnd.collection.osm-observations+json",
            ),
        ),
    ]
    return outputs, observations


def _inputs() -> list[_BoundArtifact]:
    return [
        _BoundArtifact(
            0,
            _artifact(
                _QUERY_ID,
                "overpass_query_spec",
                f"sha256:{'4' * 64}",
                "application/vnd.collection.overpass-query-spec+json",
            ),
        )
    ]


def test_osm_result_digest_matches_the_producer_payload_shape() -> None:
    payload = json.dumps(
        {
            "contract": "osm-overpass-result@1",
            "observationsDigest": f"sha256:{'b' * 64}",
            "rawResponseDigest": f"sha256:{'a' * 64}",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert (
        _osm_result_digest(
            output_contract="osm-overpass-result@1",
            raw_digest=f"sha256:{'a' * 64}",
            observation_digest=f"sha256:{'b' * 64}",
        )
        == f"sha256:{sha256(payload).hexdigest()}"
    )


def test_two_output_osm_result_advances_and_returns_both_artifacts() -> None:
    outputs, observations = _bindings()
    reader = _Reader({"osm_raw_response": _RAW_CONTENT, "osm_observations": observations})
    digest = _osm_result_digest(
        output_contract="osm-overpass-result@1",
        raw_digest=_RAW_DIGEST,
        observation_digest=outputs[1].artifact.content_digest,
    )

    raw, obs, content = reader._read_osm_result(
        object(),
        _WORK_ID,
        outputs=tuple(outputs),
        inputs=tuple(_inputs()),
        output_digest=digest,
    )

    assert raw.artifact_id == _RAW_ID
    assert obs.artifact_id == _OBS_ID
    assert content == observations


def test_osm_observations_envelope_reports_sorted_element_identities() -> None:
    envelope = decode_osm_observation_batch(_observations_document())

    assert envelope.osm_ids == (("node", 123456789),)
    assert envelope.element_count == 1
    assert envelope.response_digest == _RAW_DIGEST
    assert envelope.attribution == "\u00a9 OpenStreetMap contributors"


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("single_output", "PIPELINE_OSM_OUTPUT_BINDINGS_INVALID"),
        ("three_outputs", "PIPELINE_OSM_OUTPUT_BINDINGS_INVALID"),
        ("wrong_role", "PIPELINE_OSM_OUTPUT_BINDINGS_INVALID"),
        ("wrong_content_type", "PIPELINE_OSM_OUTPUT_CONTRACT_INVALID"),
        ("missing_query", "PIPELINE_OSM_SOURCE_IDENTITY_MISSING"),
        ("digest_mismatch", "PIPELINE_OSM_RESULT_DIGEST_MISMATCH"),
        ("raw_pairing_mismatch", "PIPELINE_OSM_RAW_DIGEST_MISMATCH"),
    ],
)
def test_osm_result_rejects_invalid_outputs_deterministically(case: str, code: str) -> None:
    outputs, observations = _bindings()
    inputs = _inputs()
    if case == "single_output":
        outputs = outputs[:1]
    elif case == "three_outputs":
        outputs = [
            *outputs,
            _BoundArtifact(
                2,
                _artifact(
                    UUID("00000000-0000-0000-0000-000000000905"),
                    "osm_extras",
                    f"sha256:{'9' * 64}",
                    "application/json",
                ),
            ),
        ]
    elif case == "wrong_role":
        outputs[1] = _BoundArtifact(
            1,
            _artifact(
                _OBS_ID,
                "osm_observation",
                outputs[1].artifact.content_digest,
                "application/vnd.collection.osm-observations+json",
            ),
        )
    elif case == "wrong_content_type":
        outputs[0] = _BoundArtifact(
            0,
            _artifact(_RAW_ID, "osm_raw_response", _RAW_DIGEST, "text/plain"),
        )
    elif case == "missing_query":
        inputs = []
    elif case == "raw_pairing_mismatch":
        observations = _observations_document(response_digest=f"sha256:{'7' * 64}")
        outputs[1] = _BoundArtifact(
            1,
            _artifact(
                _OBS_ID,
                "osm_observations",
                f"sha256:{sha256(observations).hexdigest()}",
                "application/vnd.collection.osm-observations+json",
            ),
        )

    reader = _Reader({"osm_raw_response": _RAW_CONTENT, "osm_observations": observations})
    supplied_digest = (
        f"sha256:{'0' * 64}"
        if case == "digest_mismatch"
        else _osm_result_digest(
            output_contract="osm-overpass-result@1",
            raw_digest=_RAW_DIGEST,
            observation_digest=outputs[-1].artifact.content_digest,
        )
    )

    with pytest.raises(PipelineAdvancementConflict) as caught:
        reader._read_osm_result(
            object(),
            _WORK_ID,
            outputs=tuple(outputs),
            inputs=tuple(inputs),
            output_digest=supplied_digest,
        )

    assert caught.value.code == code
    assert caught.value.required_action


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(attribution="© Someone Else"),
        lambda d: d.update(responseDigest="not-a-digest"),
        lambda d: d.update(observations=[]),
        lambda d: d.update(observations=[]),
    ],
)
def test_osm_observations_envelope_rejects_invalid_documents(mutate: object) -> None:
    document = json.loads(_observations_document().decode("utf-8"))
    mutate(document)  # type: ignore[operator]

    if document["observations"] == []:
        # An explicitly empty complete scan is legal and must stay representable.
        envelope = decode_osm_observation_batch(json.dumps(document).encode("utf-8"))
        assert envelope.element_count == 0
        return

    with pytest.raises(ValueError):
        decode_osm_observation_batch(json.dumps(document).encode("utf-8"))


@pytest.mark.parametrize(
    "document",
    [
        {"attribution": "\u00a9 OpenStreetMap contributors"},
        "not-an-object",
        {
            "attribution": "x",
            "generator": None,
            "observations": [],
            "osmBaseTimestampUtc": None,
            "queryDigest": "sha256:" + "1" * 64,
            "responseDigest": "sha256:" + "2" * 64,
            "extra": True,
        },
    ],
)
def test_osm_observations_envelope_rejects_wrong_shape(document: object) -> None:
    with pytest.raises(ValueError):
        decode_osm_observation_batch(json.dumps(document).encode("utf-8"))


def test_osm_observations_envelope_rejects_unsorted_or_duplicate_elements() -> None:
    def entry(element_type: str, osm_id: int) -> dict[str, object]:
        return {
            "address": {
                "city": "Berlin",
                "country": "DE",
                "houseNumber": None,
                "postcode": None,
                "street": None,
            },
            "elementType": element_type,
            "latitude": 52.5,
            "longitude": 13.4,
            "osmId": osm_id,
            "sourceUrl": f"https://www.openstreetmap.org/{element_type}/{osm_id}",
            "tags": {"amenity": "studio"},
        }

    unsorted = _observations_document(observation_entries=[entry("node", 2), entry("node", 1)])
    duplicate = _observations_document(observation_entries=[entry("node", 1), entry("node", 1)])

    with pytest.raises(ValueError):
        decode_osm_observation_batch(unsorted)
    with pytest.raises(ValueError):
        decode_osm_observation_batch(duplicate)


def test_osm_observations_envelope_rejects_identity_url_mismatch() -> None:
    entry = {
        "address": {
            "city": "Berlin",
            "country": "DE",
            "houseNumber": None,
            "postcode": None,
            "street": None,
        },
        "elementType": "node",
        "latitude": 52.5,
        "longitude": 13.4,
        "osmId": 7,
        "sourceUrl": "https://www.openstreetmap.org/node/8",
        "tags": {"amenity": "studio"},
    }

    with pytest.raises(ValueError):
        decode_osm_observation_batch(_observations_document(observation_entries=[entry]))
