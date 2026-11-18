from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest

from collection_contracts import (
    ObservationState,
    SourceRecordObservationBatch,
    canonical_source_record_observation_batch_json,
    decode_source_record_observation_batch,
)
from processing_worker import ProcessingWorker, ProcessingWorkerSettings
from processing_worker.source_record_mapping import (
    SourceRecordMappingError,
    materialize_source_record_observations,
)
from source_connector_sdk import LeaseArtifact, WorkerLease

_NOW = datetime(2026, 11, 15, tzinfo=UTC)
_RUN_ID = "77777777-7777-5777-8777-777777777777"
_ARTIFACT_ID = "88888888-8888-5888-8888-888888888888"

_PROFILE = json.dumps(
    {
        "campaignKey": "berlin_recording_services",
        "mappingRevision": "osm-overpass-source-record-mapping-v1",
        "sourceBindingKey": "osm_overpass_berlin",
        "sourceKey": "openstreetmap",
        "sourcePolicyDigest": f"sha256:{'a' * 64}",
    },
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")


def _record_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "runId": _RUN_ID,
        "artifactRecordId": _ARTIFACT_ID,
        "sourceRecordId": "osm:batch-1",
        "datasetRelease": "osm-overpass-live-v1",
        "sourceUrl": "https://www.openstreetmap.org/node/101",
        "osmBaseTimestampUtc": "2026-11-16T00:01:59Z",
        "fields": ["display_name", "website"],
        "records": [
            {
                "sourceRecordId": "osm:batch-1",
                "display_name": {
                    "value": "Beispiel  Studio",
                    "pointer": "/observations/0/tags/name",
                },
            },
        ],
    }
    document.update(overrides)
    return document


def _seal(document: dict[str, object]) -> bytes:
    """Serialize with the producer's digest rule: over the document minus its digest."""
    body = json.dumps(
        {key: value for key, value in document.items() if key != "artifactDigest"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sealed = {**document, "artifactDigest": f"sha256:{sha256(body).hexdigest()}"}
    return json.dumps(sealed, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _record_bytes(**overrides: object) -> bytes:
    return _seal(_record_document(**overrides))


# The real tags of OSM node 871572117 ("nb film"), acquired from the configured endpoint.
_REAL_NODE_TAGS = {
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


def _field_aliases() -> dict[str, list[str]]:
    return {
        "email": ["email", "contact:email"],
        "phone": ["phone", "contact:phone"],
        "website": ["website", "contact:website"],
    }


def _alias_profile() -> bytes:
    document = json.loads(_PROFILE.decode("utf-8"))
    document.setdefault("runId", _RUN_ID)
    document.setdefault("entityKind", "place")
    document["fieldProjection"] = {
        "display_name": "names",
        "email": "emails",
        "phone": "phones",
        "website": "website_urls",
    }
    document["fieldAliases"] = _field_aliases()
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _osm_record_bytes(tags: dict[str, str], *, osm_id: int = 871572117) -> bytes:
    """A real per-element OSM source record built through the envelope contract."""

    from collection_contracts import (
        canonical_osm_source_record_json,
        decode_osm_envelope,
    )

    envelope = json.dumps(
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
                    "osmId": osm_id,
                    "sourceUrl": f"https://www.openstreetmap.org/node/{osm_id}",
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
    element = decode_osm_envelope(envelope).elements[0]
    return canonical_osm_source_record_json(element).encode("utf-8")


def _materialize_alias(content: bytes) -> SourceRecordObservationBatch:
    return materialize_source_record_observations(
        content,
        _alias_profile(),
        maximum_records=1,
    )


def _materialize(content: bytes, *, maximum_records: int = 1_000) -> SourceRecordObservationBatch:
    return materialize_source_record_observations(
        content,
        _PROFILE,
        maximum_records=maximum_records,
    )


def test_real_node_contact_website_produces_observed_website() -> None:
    """OSM node 871572117 publishes its site only as `contact:website`."""

    batch = _materialize_alias(_osm_record_bytes(_REAL_NODE_TAGS))
    website = next(item for item in batch.observations if item.field_key == "website")

    assert website.state is ObservationState.OBSERVED
    assert website.value is not None
    assert website.value.normalized == "http://www.nbfilm.de/"
    assert website.evidence is not None
    assert website.evidence.locator_value == "/tags/contact:website"
    assert website.evidence.source_url == "https://www.openstreetmap.org/node/871572117"
    assert website.evidence.raw_artifact_digest == batch.source_record_artifact_digest


def test_real_node_contact_phone_produces_observed_phone() -> None:
    batch = _materialize_alias(_osm_record_bytes(_REAL_NODE_TAGS))
    phone = next(item for item in batch.observations if item.field_key == "phone")

    assert phone.state is ObservationState.OBSERVED
    assert phone.evidence is not None
    assert phone.evidence.locator_value == "/tags/contact:phone"


def test_absent_alias_and_primary_still_reports_not_observed() -> None:
    """No contact tag for email: unknown, never a confirmed absence."""

    batch = _materialize_alias(_osm_record_bytes(_REAL_NODE_TAGS))
    email = next(item for item in batch.observations if item.field_key == "email")

    assert email.state is ObservationState.NOT_OBSERVED
    assert email.value is None
    assert email.evidence is None
    assert all(item.state is not ObservationState.ABSENT_IN_SOURCE for item in batch.observations)


def test_primary_tag_still_wins_when_alias_is_absent() -> None:
    tags = {key: value for key, value in _REAL_NODE_TAGS.items() if key != "contact:website"}
    tags["website"] = "https://plain.example"
    batch = _materialize_alias(_osm_record_bytes(tags, osm_id=871572118))
    websites = [item for item in batch.observations if item.field_key == "website"]

    assert len(websites) == 1
    assert websites[0].evidence is not None
    assert websites[0].evidence.locator_value == "/tags/website"
    assert websites[0].value is not None
    assert websites[0].value.normalized == "https://plain.example/"


def test_identical_normalized_values_are_deduplicated() -> None:
    tags = {
        **_REAL_NODE_TAGS,
        "website": "http://www.nbfilm.de",
        "contact:website": "http://www.nbfilm.de",
    }
    batch = _materialize_alias(_osm_record_bytes(tags, osm_id=871572119))
    websites = [item for item in batch.observations if item.field_key == "website"]

    # Same meaning: one observation, not two.
    assert len(websites) == 1
    assert websites[0].value is not None
    assert websites[0].value.normalized == "http://www.nbfilm.de/"


def test_conflicting_values_are_all_kept_and_never_overwritten() -> None:
    tags = {
        **_REAL_NODE_TAGS,
        "website": "https://primary.example",
        "contact:website": "https://alias.example",
    }
    batch = _materialize_alias(_osm_record_bytes(tags, osm_id=871572120))
    websites = [item for item in batch.observations if item.field_key == "website"]

    # Both survive so a reviewer can resolve the conflict; neither silently wins.
    assert len(websites) == 2
    assert {item.value.normalized for item in websites if item.value} == {
        "https://primary.example/",
        "https://alias.example/",
    }
    assert {item.evidence.locator_value for item in websites if item.evidence} == {
        "/tags/website",
        "/tags/contact:website",
    }


def test_alias_mapping_is_deterministic_across_replays() -> None:
    content = _osm_record_bytes(_REAL_NODE_TAGS)
    first = _materialize_alias(content)
    second = _materialize_alias(content)

    assert first.content_digest == second.content_digest
    assert canonical_source_record_observation_batch_json(first) == (
        canonical_source_record_observation_batch_json(second)
    )


def test_profile_without_aliases_keeps_exact_original_behaviour() -> None:
    """A campaign that declares no aliases must not start reading contact:* tags."""

    batch = _materialize_alias(_osm_record_bytes(_REAL_NODE_TAGS))
    assert next(o for o in batch.observations if o.field_key == "website").state is (
        ObservationState.OBSERVED
    )

    document = json.loads(_alias_profile().decode("utf-8"))
    document.pop("fieldAliases")
    plain_profile = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

    from processing_worker.source_record_mapping import materialize_source_record_observations

    plain = materialize_source_record_observations(
        _osm_record_bytes(_REAL_NODE_TAGS),
        plain_profile,
        maximum_records=1,
    )
    plain_website = next(item for item in plain.observations if item.field_key == "website")
    assert plain_website.state is ObservationState.NOT_OBSERVED
    assert plain_website.value is None


def test_mapping_emits_source_native_batch_without_html_or_manual_lineage() -> None:
    batch = _materialize(_record_bytes())

    assert batch.source_key == "openstreetmap"
    assert batch.source_binding_key == "osm_overpass_berlin"
    assert batch.dataset_release == "osm-overpass-live-v1"
    assert batch.source_policy_digest == f"sha256:{'a' * 64}"
    dump = batch.model_dump(mode="json", by_alias=True)
    assert "extractedRecordDigest" not in dump
    assert "extractedRecordArtifactId" not in dump


def test_mapping_round_trips_through_canonical_contract() -> None:
    batch = _materialize(_record_bytes())
    canonical = canonical_source_record_observation_batch_json(batch)

    assert decode_source_record_observation_batch(canonical.encode("utf-8")) == batch


def test_absent_field_is_not_observed_never_confirmed_absent() -> None:
    batch = _materialize(_record_bytes())
    by_field = {item.field_key: item for item in batch.observations}

    website = by_field["website"]
    assert website.state is ObservationState.NOT_OBSERVED
    assert website.value is None
    assert website.evidence is None

    assert all(item.state is not ObservationState.ABSENT_IN_SOURCE for item in batch.observations)


def test_observed_field_keeps_pointer_locator_and_record_artifact() -> None:
    batch = _materialize(_record_bytes())
    observed = next(
        item
        for item in batch.observations
        if item.state is ObservationState.OBSERVED and item.field_key == "display_name"
    )

    assert observed.evidence is not None
    assert observed.evidence.locator_value == "/observations/0/tags/name"
    assert observed.evidence.raw_artifact_id == UUID(_ARTIFACT_ID)
    assert observed.evidence.raw_artifact_digest == batch.source_record_artifact_digest
    assert observed.value is not None


def test_malformed_single_field_is_quarantined_while_neighbours_continue() -> None:
    """A semantically invalid field value is typed INVALID; the rest still map."""

    document = _record_document()
    document["records"] = [
        {
            "sourceRecordId": "osm:batch-1",
            "display_name": {"value": "Ok Studio", "pointer": "/a"},
            "website": "https://studio.example/",
        },
    ]
    batch = _materialize(_seal(document))

    states = {item.field_key: item.state for item in batch.observations}
    assert states["display_name"] is ObservationState.OBSERVED
    assert states["website"] is ObservationState.INVALID


def test_multiple_records_in_one_artifact_are_rejected() -> None:
    document = _record_document()
    document["records"] = [
        {"sourceRecordId": "osm:batch-1", "display_name": {"value": "A", "pointer": "/a"}},
        {"sourceRecordId": "osm:batch-1", "display_name": {"value": "B", "pointer": "/b"}},
    ]

    with pytest.raises(SourceRecordMappingError) as caught:
        _materialize(_seal(document))

    assert caught.value.code == "SOURCE_RECORD_ENTRY_COUNT_INVALID"


def test_record_identity_conflict_is_rejected() -> None:
    document = _record_document()
    document["records"] = [
        {"sourceRecordId": "osm:other", "display_name": {"value": "A", "pointer": "/a"}},
    ]

    with pytest.raises(SourceRecordMappingError) as caught:
        _materialize(_seal(document))

    assert caught.value.code == "SOURCE_RECORD_IDENTITY_CONFLICT"


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("tampered", "SOURCE_RECORD_ARTIFACT_TAMPERED"),
        ("no_records", "SOURCE_RECORD_ENTRIES_MISSING"),
        ("no_fields", "SOURCE_RECORD_FIELDS_MISSING"),
        ("bad_profile", "SOURCE_MAPPING_PROFILE_INVALID"),
        ("no_entries", "SOURCE_RECORD_ENTRIES_MISSING"),
    ],
)
def test_corrupted_provenance_fails_typed(case: str, code: str) -> None:
    if case == "tampered":
        content = _record_bytes()
        content = content.replace(b'"osm:batch-1"', b'"osm:batch-9"')
        profile = _PROFILE
    elif case == "no_records":
        content = _record_bytes(records=[])
        profile = _PROFILE
    elif case == "no_fields":
        content = _record_bytes(fields=[])
        profile = _PROFILE
    elif case == "no_entries":
        content = _record_bytes(records=[])
        profile = _PROFILE
    else:
        content = _record_bytes()
        profile = b'{"mappingRevision":"x"}'

    with pytest.raises(SourceRecordMappingError) as caught:
        materialize_source_record_observations(content, profile, maximum_records=100)

    assert caught.value.code == code


def test_record_limit_is_bounded() -> None:
    document = _record_document()
    document["records"] = [
        {"sourceRecordId": "osm:batch-1", "display_name": {"value": "A", "pointer": "/a"}},
        {"sourceRecordId": "osm:batch-1", "display_name": {"value": "B", "pointer": "/b"}},
    ]
    content = _seal(document)

    with pytest.raises(SourceRecordMappingError) as caught:
        materialize_source_record_observations(content, _PROFILE, maximum_records=1)

    assert caught.value.code == "SOURCE_RECORD_ENTRIES_EXCEEDED"


def test_mapping_is_deterministic_for_replay() -> None:
    content = _record_bytes()

    first = _materialize(content)
    second = _materialize(content)

    assert first.content_digest == second.content_digest
    assert canonical_source_record_observation_batch_json(first) == (
        canonical_source_record_observation_batch_json(second)
    )


class _Gateway:
    def __init__(self, lease: WorkerLease, content_by_role: dict[str, bytes]) -> None:
        self.lease = lease
        self.content_by_role = content_by_role
        self.published: tuple[bytes, str, str, str] | None = None
        self.failures: list[tuple[str, str]] = []

    def register(self, settings: ProcessingWorkerSettings) -> None:
        assert settings.output_contract == "source-record-observation-batch@1"

    def acquire(self, settings: ProcessingWorkerSettings) -> WorkerLease | None:
        assert settings.capability == "source_record_mapping"
        return self.lease

    def heartbeat(self, lease: WorkerLease, settings: ProcessingWorkerSettings) -> WorkerLease:
        del settings
        return lease

    def read_input(self, lease: WorkerLease, *, role: str, maximum_bytes: int) -> bytes:
        lease.artifact(role)
        content = self.content_by_role[role]
        assert len(content) <= maximum_bytes
        return content

    def publish_and_complete(
        self,
        lease: WorkerLease,
        *,
        content: bytes,
        content_type: str,
        output_role: str,
        output_digest: str,
    ) -> None:
        del lease
        self.published = (content, content_type, output_role, output_digest)

    def fail(
        self,
        lease: WorkerLease,
        *,
        failure_kind: str,
        code: str,
        message: str,
        required_action: str,
    ) -> None:
        del lease, message, required_action
        self.failures.append((failure_kind, code))


def _lease(roles: tuple[str, ...]) -> WorkerLease:
    return WorkerLease(
        lease_id=UUID(int=1),
        work_id=UUID(int=2),
        lease_token=UUID(int=3),
        worker_id="worker-source-record-mapping-tests",
        stage="normalization",
        capability="source_record_mapping",
        input_digest=f"sha256:{'4' * 64}",
        expected_output_contract="source-record-observation-batch@1",
        issued_at_utc=_NOW,
        expires_at_utc=_NOW + timedelta(minutes=5),
        heartbeat_deadline_utc=_NOW + timedelta(minutes=1),
        source_permit=None,
        input_artifacts=tuple(
            LeaseArtifact(artifact_id=UUID(int=index + 10), role=role)
            for index, role in enumerate(roles)
        ),
        correlation_id="correlation-mapping-tests",
    )


def _settings() -> ProcessingWorkerSettings:
    return ProcessingWorkerSettings(
        gateway_url="http://worker-gateway.test",
        gateway_token="token",
        build_identity="processing-worker@tests",
        capability="source_record_mapping",
        heartbeat_interval_seconds=60,
    )


def test_worker_executes_source_record_mapping_end_to_end() -> None:
    content = _record_bytes()
    gateway = _Gateway(
        _lease(("source_record", "source_mapping_profile")),
        {"source_record": content, "source_mapping_profile": _PROFILE},
    )
    worker = ProcessingWorker(gateway, _settings())  # type: ignore[arg-type]

    result = worker.run_once()

    assert result.acquired is True
    assert gateway.published is not None
    payload, content_type, role, output_digest = gateway.published
    assert content_type == "application/vnd.collection.source-record-observation-batch+json"
    assert role == "source_record_observation_batch"
    # The published output digest covers the exact bytes the gateway now stores; the batch
    # keeps its own sealed identity inside them.
    assert output_digest == f"sha256:{sha256(payload).hexdigest()}"
    assert decode_source_record_observation_batch(payload).content_digest != output_digest
    assert (
        canonical_source_record_observation_batch_json(
            decode_source_record_observation_batch(payload)
        ).encode("utf-8")
        == payload
    )


def test_worker_rejects_lease_with_manual_lineage_role() -> None:
    content = _record_bytes()
    gateway = _Gateway(
        _lease(("source_record", "source_mapping_profile", "manual_record_lineage")),
        {"source_record": content, "source_mapping_profile": _PROFILE},
    )
    worker = ProcessingWorker(gateway, _settings())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="roles are incomplete"):
        worker.run_once()

    assert gateway.failures
    assert gateway.failures[0][0] == "contract_invalid"


def test_worker_reports_tampered_source_record_as_contract_failure() -> None:
    content = _record_bytes().replace(b'"osm:batch-1"', b'"osm:batch-9"')
    gateway = _Gateway(
        _lease(("source_record", "source_mapping_profile")),
        {"source_record": content, "source_mapping_profile": _PROFILE},
    )
    worker = ProcessingWorker(gateway, _settings())  # type: ignore[arg-type]

    with pytest.raises(SourceRecordMappingError):
        worker.run_once()

    assert gateway.failures == [("contract_invalid", "SOURCE_RECORD_ARTIFACT_TAMPERED")]
    assert gateway.published is None


def test_missing_acquisition_time_is_rejected_not_stamped_with_now() -> None:
    document = _record_document()
    document.pop("osmBaseTimestampUtc")

    with pytest.raises(SourceRecordMappingError) as caught:
        _materialize(_seal(document))

    assert caught.value.code == "SOURCE_RECORD_TIMESTAMP_MISSING"


def test_conflicting_acquisition_times_are_rejected() -> None:
    document = _record_document(
        osmBaseTimestampUtc="2026-11-16T00:01:59Z",
        acquiredAtUtc="2026-11-16T03:00:00Z",
    )

    with pytest.raises(SourceRecordMappingError) as caught:
        _materialize(_seal(document))

    assert caught.value.code == "SOURCE_RECORD_TIMESTAMP_CONFLICT"


def test_agreeing_acquisition_times_are_accepted() -> None:
    document = _record_document(
        osmBaseTimestampUtc="2026-11-16T00:01:59Z",
        observedAtUtc="2026-11-16T00:01:59Z",
    )

    batch = _materialize(_seal(document))
    assert batch.observations


@pytest.mark.parametrize(
    "value",
    ["not-a-time", "", "2026-11-16T00:01:59", "16/11/2026 00:01:59"],
)
def test_invalid_acquisition_time_is_rejected(value: object) -> None:
    with pytest.raises(SourceRecordMappingError) as caught:
        _materialize(_seal(_record_document(osmBaseTimestampUtc=value)))

    assert caught.value.code == "SOURCE_RECORD_TIMESTAMP_INVALID"


def test_acquisition_time_is_derived_from_the_record_not_the_clock() -> None:
    first = _materialize(_seal(_record_document()))
    second = _materialize(_seal(_record_document()))

    first_evidence = first.observations[0].evidence
    second_evidence = second.observations[0].evidence
    assert first_evidence is not None and second_evidence is not None
    assert first_evidence.observed_at_utc == second_evidence.observed_at_utc
    assert first_evidence.observed_at_utc == datetime(2026, 11, 16, 0, 1, 59, tzinfo=UTC)


def test_element_modification_time_is_kept_apart_from_observed_time() -> None:
    """An OSM edit time describes the object, so it must not become the observation time."""

    document = _record_document(
        osmBaseTimestampUtc="2026-11-16T00:01:59Z",
        elementTimestampUtc="2024-03-01T10:00:00Z",
    )
    batch = _materialize(_seal(document))

    observed = {
        item.evidence.observed_at_utc for item in batch.observations if item.evidence is not None
    }
    assert observed == {datetime(2026, 11, 16, 0, 1, 59, tzinfo=UTC)}


def test_altered_source_artifact_is_rejected_before_mapping() -> None:
    sealed = _seal(_record_document())
    tampered = sealed.replace(b"Beispiel  Studio", b"Beispiel  Studoi")

    with pytest.raises(SourceRecordMappingError) as caught:
        _materialize(tampered)

    assert caught.value.code == "SOURCE_RECORD_ARTIFACT_TAMPERED"


def test_a_name_keeps_the_language_the_source_published_it_in() -> None:
    document = _record_document(
        fields=["display_name"],
        records=[
            {
                "sourceRecordId": "osm:batch-1",
                "display_name": {
                    "value": "Hansa Studios",
                    "pointer": "/entities/Q667023/labels/de",
                    "locale": "de",
                },
            }
        ],
    )
    batch = _materialize(_seal(document))

    observed = {item.field_key: item for item in batch.observations}
    assert observed["display_name"].value.locale == "de"


def test_a_name_without_a_declared_language_keeps_no_locale() -> None:
    batch = _materialize(_record_bytes())

    observed = {item.field_key: item for item in batch.observations}
    assert observed["display_name"].value.locale is None


def test_a_malformed_language_tag_is_invalid_evidence_not_a_dropped_one() -> None:
    document = _record_document(
        fields=["display_name"],
        records=[
            {
                "sourceRecordId": "osm:batch-1",
                "display_name": {
                    "value": "Hansa Studios",
                    "pointer": "/entities/Q667023/labels/??",
                    "locale": "not a tag",
                },
            }
        ],
    )
    batch = _materialize(_seal(document))

    observed = {item.field_key: item for item in batch.observations}
    assert observed["display_name"].state is ObservationState.INVALID
    assert observed["display_name"].value.reason_code == "SOURCE_RECORD_LOCALE_INVALID"


def test_a_declared_point_becomes_typed_geometry_not_text() -> None:
    document = _record_document(
        fields=["coordinates"],
        records=[
            {
                "sourceRecordId": "osm:batch-1",
                "coordinates": {
                    "value": '{"latitude":"52.5063","longitude":"13.3772"}',
                    "pointer": "/entities/Q667023/claims/P625",
                    "valueKind": "geo_point",
                    "geometrySourceRecordId": "osm:batch-1",
                },
            }
        ],
    )
    batch = _materialize(_seal(document))

    observed = {item.field_key: item for item in batch.observations}
    assert observed["coordinates"].value.kind == "geo_point"
    assert str(observed["coordinates"].value.latitude) == "52.5063"


def test_an_unsupported_declared_value_kind_is_refused() -> None:
    document = _record_document(
        fields=["display_name"],
        records=[
            {
                "sourceRecordId": "osm:batch-1",
                "display_name": {
                    "value": "Hansa Studios",
                    "pointer": "/labels/de",
                    "valueKind": "phone",
                },
            }
        ],
    )
    batch = _materialize(_seal(document))

    observed = {item.field_key: item for item in batch.observations}
    assert observed["display_name"].state is ObservationState.INVALID
    assert observed["display_name"].value.reason_code == "SOURCE_RECORD_VALUE_KIND_UNSUPPORTED"


def _wikidata_profile() -> bytes:
    document = json.loads(_PROFILE.decode("utf-8"))
    document["runId"] = _RUN_ID
    document["entityKind"] = "place"
    document["mappingRevision"] = "wikidata-entity-source-record-mapping-v1"
    document["sourceBindingKey"] = "wikidata_studios_berlin"
    document["sourceKey"] = "wikidata"
    document["fieldProjection"] = {
        "display_name": "names",
        "website": "website_urls",
        "address": "addresses",
        "coordinates": "coordinates",
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _wikidata_seal(document: dict[str, object]) -> bytes:
    """Seal exactly as the Wikidata adapter does: UTF-8, not escaped, digest over the rest."""

    unsealed = {key: value for key, value in document.items() if key != "artifactDigest"}
    canonical = json.dumps(
        unsealed,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    document["artifactDigest"] = f"sha256:{sha256(canonical).hexdigest()}"
    return json.dumps(
        document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _wikidata_record_bytes(**overrides: object) -> bytes:
    """One entity as the Wikidata adapter publishes it, including its class claim."""

    document: dict[str, object] = {
        "contract": "wikidata-source-record@1",
        "sourceRecordId": "wikidata:Q667023",
        "entityRevisionId": 2465661990,
        "observedAtUtc": "2026-02-22T15:00:00Z",
        "datasetRelease": "wikidata-entity-revision:2465661990",
        "runId": _RUN_ID,
        "sourceUrl": "https://www.wikidata.org/wiki/Special:EntityData/Q667023.json",
        "attribution": "Wikidata (CC0 1.0)",
        "artifactRecordId": _ARTIFACT_ID,
        "fields": [
            "address",
            "category_recording_studio",
            "coordinates",
            "display_name",
            "website",
        ],
        "records": [
            {
                "sourceRecordId": "wikidata:Q667023",
                "display_name": {
                    "value": "Hansa Studios",
                    "pointer": "/entities/Q667023/labels/de",
                    "locale": "de",
                },
                "address": {
                    "value": "Köpenicker Straße 1, 10997 Berlin",
                    "pointer": "/entities/Q667023/claims/P6375/statement",
                    "locale": "de",
                },
                "website": {
                    "value": "https://www.hansastudios.de/",
                    "pointer": "/entities/Q667023/claims/P856/statement",
                    "valueKind": "url",
                },
                "coordinates": {
                    "value": '{"latitude":"52.5063","longitude":"13.3772"}',
                    "pointer": "/entities/Q667023/claims/P625/statement",
                    "valueKind": "geo_point",
                    "geometrySourceRecordId": "wikidata:Q667023",
                },
                "category_recording_studio": {
                    "value": "recording_studio",
                    "pointer": "/entities/Q667023/claims/P31/Q667023$statement",
                },
            }
        ],
    }
    document.update(overrides)
    return _wikidata_seal(document)


def test_wikidata_entity_record_maps_every_published_field_to_its_own_value_kind() -> None:
    """The six normalized meanings of one entity stay six typed observations."""

    batch = materialize_source_record_observations(
        _wikidata_record_bytes(),
        _wikidata_profile(),
        maximum_records=1,
    )

    observed = {item.field_key: item for item in batch.observations}
    assert set(observed) == {
        "address",
        "category_recording_studio",
        "coordinates",
        "display_name",
        "website",
    }
    assert batch.source_key == "wikidata"
    assert batch.source_record_id == "wikidata:Q667023"
    assert observed["display_name"].state is ObservationState.OBSERVED
    assert observed["display_name"].value.locale == "de"
    assert observed["address"].value.locale == "de"
    assert observed["website"].value.kind == "url"
    assert observed["website"].value.normalized == "https://www.hansastudios.de/"
    assert observed["coordinates"].value.kind == "geo_point"
    assert str(observed["coordinates"].value.latitude) == "52.5063"
    # The classification claim keeps the statement it was read from.
    assert observed["category_recording_studio"].value.normalized == "recording_studio"
    assert observed["category_recording_studio"].evidence.locator_value.endswith("$statement")


def test_a_wikidata_category_claim_cannot_describe_a_different_category() -> None:
    document = _wikidata_record_bytes()
    mismatched = document.replace(
        b'"category_recording_studio":{"pointer":"/entities/Q667023/claims/P31/Q667023$statement",'
        b'"value":"recording_studio"}',
        b'"category_rehearsal_room":{"pointer":"/entities/Q667023/claims/P31/Q667023$statement",'
        b'"value":"recording_studio"}',
    ).replace(b'"category_recording_studio"', b'"category_rehearsal_room"', 1)
    document = json.loads(mismatched.decode("utf-8"))
    document["fields"] = [
        "category_rehearsal_room" if item == "category_recording_studio" else item
        for item in document["fields"]
    ]
    batch = materialize_source_record_observations(
        _wikidata_seal({key: value for key, value in document.items() if key != "artifactDigest"}),
        _wikidata_profile(),
        maximum_records=1,
    )

    rejected = next(
        item for item in batch.observations if item.field_key == "category_rehearsal_room"
    )
    assert rejected.state is ObservationState.INVALID
    assert rejected.value.reason_code == "SOURCE_RECORD_CATEGORY_INVALID"


def test_a_wikidata_entity_without_a_website_still_maps() -> None:
    document = _wikidata_record_bytes()
    parsed = json.loads(document.decode("utf-8"))
    del parsed["records"][0]["website"]
    parsed["fields"] = [item for item in parsed["fields"] if item != "website"]
    batch = materialize_source_record_observations(
        _wikidata_seal({key: value for key, value in parsed.items() if key != "artifactDigest"}),
        _wikidata_profile(),
        maximum_records=1,
    )

    observed = {item.field_key for item in batch.observations}
    assert "website" not in observed
    assert {"display_name", "coordinates", "category_recording_studio"} <= observed
