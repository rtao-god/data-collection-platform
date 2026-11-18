"""Wire contract of one bounded Wikidata entity acquisition result.

The acquisition worker publishes two immutable artifacts: the exact response bytes and this
envelope. The envelope repeats the response digest, the entity set that was asked for, and
one already-adapted source record per accepted entity with its own digest — so a consumer can
prove that every record it maps came from the response this work acquired, for the set it
asked for, and that nothing was added, dropped or substituted on the way.

Deliberately free of any connector import: the adaptation semantics belong to the connector,
the wire shape and its digests belong here. `collection_application` and
`collection_infrastructure` read this module only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

RESULT_CONTRACT = "wikidata-entity-result@2"
RESULT_SCHEMA_REVISION = "wikidata-entity-result-v2"
MAX_RESULT_ENTITIES = 50

_RESULT_KEYS = frozenset(
    {
        "contract",
        "records",
        "rejectedEntities",
        "requestedEntityIds",
        "responseDigest",
        "retrievedAtUtc",
        "schemaRevision",
    }
)
_RECORD_KEYS = frozenset(
    {
        "campaignCategory",
        "classificationPointer",
        "document",
        "entityId",
        "recordDigest",
        "sourceRecordId",
    }
)
_REJECTION_KEYS = frozenset({"code", "entityId", "message"})
_CATEGORY_FIELD_PREFIX = "category_"


@dataclass(frozen=True, slots=True)
class WikidataAdaptedRecordEntry:
    """One accepted entity, exactly as it will be published as a source record."""

    entity_id: str
    source_record_id: str
    artifact_record_id: UUID
    record_digest: str
    campaign_category: str
    classification_pointer: str
    document_bytes: bytes
    source_url: str
    dataset_release: str
    observed_at_utc: datetime


@dataclass(frozen=True, slots=True)
class WikidataRejectedEntityEntry:
    entity_id: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class WikidataEntityResultEnvelope:
    """The decoded result: what was answered, what was accepted and what was refused."""

    requested_entity_ids: tuple[str, ...]
    response_digest: str
    retrieved_at_utc: datetime
    records: tuple[WikidataAdaptedRecordEntry, ...]
    rejected_entities: tuple[WikidataRejectedEntityEntry, ...]
    content_digest: str

    @property
    def accepted_entity_ids(self) -> tuple[str, ...]:
        return tuple(item.entity_id for item in self.records)

    @property
    def rejected_entity_ids(self) -> tuple[str, ...]:
        return tuple(item.entity_id for item in self.rejected_entities)


def canonical_wikidata_source_record_document(document: Mapping[str, object]) -> bytes:
    """The exact bytes of one adapted source record, as the connector sealed it."""

    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def wikidata_record_digest(document_bytes: bytes) -> str:
    return "sha256:" + sha256(document_bytes).hexdigest()


def sealed_wikidata_record_digest(document: Mapping[str, object]) -> str:
    """The digest a source record seals over itself: its bytes without the digest field.

    The artifact's published content digest covers the sealed bytes; this one covers the
    record content and is what the mapping owner re-derives. Both are required, so editing
    either the bytes or the record's own claim is detected.
    """

    unsealed = {key: value for key, value in document.items() if key != "artifactDigest"}
    declared = document.get("artifactDigest")
    digest = wikidata_record_digest(canonical_wikidata_source_record_document(unsealed))
    if declared != digest:
        raise ValueError("the adapted Wikidata source record is not self-verifying")
    return digest


def build_wikidata_entity_result(
    *,
    requested_entity_ids: Sequence[str],
    retrieved_at_utc: datetime,
    response_digest: str,
    records: Sequence[Mapping[str, object]],
    rejected: Sequence[tuple[str, str, str]],
) -> bytes:
    """Render the canonical envelope for one acquired response.

    Every record is described by the digest of its own bytes and by the classification
    statement it was accepted from, so a later consumer needs neither the connector nor the
    original response to decide whether a record is the one the acquisition produced.
    """

    requested = tuple(sorted(set(requested_entity_ids)))
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for document in records:
        entity_id = _required_text(document, "sourceRecordId")
        evidence = _classification_evidence(document)
        entry: dict[str, object] = {
            "campaignCategory": evidence[0],
            "classificationPointer": evidence[1],
            "document": document,
            "entityId": evidence[2],
            "recordDigest": wikidata_record_digest(
                canonical_wikidata_source_record_document(document)
            ),
            "sourceRecordId": entity_id,
        }
        if entry["entityId"] in seen:
            raise ValueError("the Wikidata result repeats one entity")
        seen.add(str(entry["entityId"]))
        entries.append(entry)
    rejections = [
        {"code": code, "entityId": entity_id, "message": message}
        for entity_id, code, message in rejected
    ]
    for rejection in rejections:
        if rejection["entityId"] in seen:
            raise ValueError("the Wikidata result both accepts and refuses one entity")
        seen.add(str(rejection["entityId"]))
    if seen != set(requested) or len(seen) != len(requested):
        raise ValueError("the Wikidata result does not describe exactly the requested entities")
    envelope = {
        "contract": RESULT_CONTRACT,
        "records": sorted(entries, key=lambda item: str(item["entityId"])),
        "rejectedEntities": sorted(rejections, key=lambda item: str(item["entityId"])),
        "requestedEntityIds": list(requested),
        "responseDigest": response_digest,
        "retrievedAtUtc": _utc_text(retrieved_at_utc),
        "schemaRevision": RESULT_SCHEMA_REVISION,
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_wikidata_entity_result(content: bytes) -> WikidataEntityResultEnvelope:
    """Read back exactly the bytes the acquisition published, re-verifying every record."""

    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Wikidata result envelope is not valid JSON") from exc
    if not isinstance(document, dict) or set(document) != _RESULT_KEYS:
        raise ValueError("Wikidata result envelope has an unexpected shape")
    if document["contract"] != RESULT_CONTRACT:
        raise ValueError("Wikidata result envelope declares an unsupported contract")
    if document["schemaRevision"] != RESULT_SCHEMA_REVISION:
        raise ValueError("Wikidata result envelope declares an unsupported schema revision")
    requested = _entity_id_list(document["requestedEntityIds"])
    if not 1 <= len(requested) <= MAX_RESULT_ENTITIES:
        raise ValueError("Wikidata result envelope exceeds the bounded entity limit")
    response_digest = document["responseDigest"]
    if not isinstance(response_digest, str) or not response_digest.startswith("sha256:"):
        raise ValueError("Wikidata result envelope carries no response digest")
    retrieved_at_utc = _parse_utc(document["retrievedAtUtc"])

    raw_records = document["records"]
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("Wikidata result envelope carries no adapted source record")
    records: list[WikidataAdaptedRecordEntry] = []
    for item in raw_records:
        records.append(_decode_record_entry(item, requested=requested))
    raw_rejections = document["rejectedEntities"]
    if not isinstance(raw_rejections, list):
        raise ValueError("Wikidata result envelope carries an invalid rejection list")
    rejections: list[WikidataRejectedEntityEntry] = []
    for item in raw_rejections:
        if not isinstance(item, dict) or set(item) != _REJECTION_KEYS:
            raise ValueError("Wikidata result envelope carries an invalid rejection")
        rejections.append(
            WikidataRejectedEntityEntry(
                entity_id=_entity_id(item["entityId"]),
                code=_required_text(item, "code"),
                message=_required_text(item, "message"),
            )
        )

    covered = [entry.entity_id for entry in records] + [entry.entity_id for entry in rejections]
    if sorted(covered) != list(requested) or len(set(covered)) != len(covered):
        raise ValueError("Wikidata result envelope does not account for every requested entity")
    return WikidataEntityResultEnvelope(
        requested_entity_ids=requested,
        response_digest=response_digest,
        retrieved_at_utc=retrieved_at_utc,
        records=tuple(records),
        rejected_entities=tuple(rejections),
        content_digest="sha256:" + sha256(content).hexdigest(),
    )


def _decode_record_entry(item: object, *, requested: tuple[str, ...]) -> WikidataAdaptedRecordEntry:
    if not isinstance(item, dict) or set(item) != _RECORD_KEYS:
        raise ValueError("Wikidata result envelope carries an invalid record entry")
    entity_id = _entity_id(item["entityId"])
    if entity_id not in requested:
        raise ValueError("Wikidata result envelope carries a record for an unrequested entity")
    document = item["document"]
    if not isinstance(document, dict):
        raise ValueError("Wikidata result envelope carries no source record document")
    document_bytes = canonical_wikidata_source_record_document(document)
    declared_digest = _required_text(item, "recordDigest")
    if declared_digest != wikidata_record_digest(document_bytes):
        raise ValueError("the adapted Wikidata source record differs from its declared digest")
    # The record also seals its own content digest; both must hold, so a producer cannot
    # change one and keep the other.
    sealed_wikidata_record_digest(document)
    source_record_id = _required_text(item, "sourceRecordId")
    if document.get("sourceRecordId") != source_record_id:
        raise ValueError("the adapted Wikidata source record contradicts its entry identity")
    category = _required_text(item, "campaignCategory")
    pointer = _required_text(item, "classificationPointer")
    document_category, document_pointer, document_entity = _classification_evidence(document)
    if (category, pointer, document_entity) != (
        document_category,
        document_pointer,
        entity_id,
    ):
        raise ValueError("the adapted Wikidata source record contradicts its classification")
    return WikidataAdaptedRecordEntry(
        entity_id=entity_id,
        source_record_id=source_record_id,
        artifact_record_id=_artifact_record_id(document),
        record_digest=declared_digest,
        campaign_category=category,
        classification_pointer=pointer,
        document_bytes=document_bytes,
        source_url=_required_text(document, "sourceUrl"),
        dataset_release=_required_text(document, "datasetRelease"),
        observed_at_utc=_parse_utc(document.get("observedAtUtc")),
    )


def _classification_evidence(document: Mapping[str, object]) -> tuple[str, str, str]:
    """The accepted category, its P31 statement pointer, and the entity it belongs to.

    The adapted record carries exactly one `category_<campaign category>` claim whose value
    repeats the category and whose pointer is the instance-of statement it was read from, so
    the envelope cannot describe a classification the record does not make.
    """

    record_id = _required_text(document, "sourceRecordId")
    if not record_id.startswith("wikidata:"):
        raise ValueError("the adapted Wikidata source record has no Wikidata identity")
    record_entity = _entity_id(record_id.split(":", 1)[1])
    records = document.get("records")
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("the adapted Wikidata source record must describe exactly one record")
    entry = records[0]
    if not isinstance(entry, dict):
        raise ValueError("the adapted Wikidata source record entry is not an object")
    categories = [
        key for key in entry if isinstance(key, str) and key.startswith(_CATEGORY_FIELD_PREFIX)
    ]
    if len(categories) != 1:
        raise ValueError("the adapted Wikidata source record carries no single category claim")
    field_key = categories[0]
    claims = entry[field_key]
    if not isinstance(claims, list) or len(claims) != 1 or not isinstance(claims[0], dict):
        raise ValueError("the adapted Wikidata source record category claim is not an object")
    claim = claims[0]
    category = _required_text(claim, "value")
    if field_key[len(_CATEGORY_FIELD_PREFIX) :] != category:
        raise ValueError("the adapted Wikidata source record category disagrees with its key")
    pointer = _required_text(claim, "pointer")
    expected_prefix = f"/entities/{record_entity}/claims/P31/"
    if not pointer.startswith(expected_prefix) or len(pointer) == len(expected_prefix):
        raise ValueError("the adapted Wikidata source record has no instance-of statement")
    statement_id = pointer[len(expected_prefix) :]
    # A statement id names its own subject, so a pointer carrying another entity's statement
    # is not evidence about this one even if the path prefix looks right.
    if not statement_id.startswith(f"{record_entity}$"):
        raise ValueError("the adapted Wikidata source record cites a foreign instance-of statement")
    return category, pointer, record_entity


def _artifact_record_id(document: Mapping[str, object]) -> UUID:
    value = document.get("artifactRecordId")
    if not isinstance(value, str):
        raise ValueError("the adapted Wikidata source record declares no artifact identity")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("the adapted Wikidata source record artifact identity is invalid") from exc
    if parsed.version != 5:
        raise ValueError("the adapted Wikidata source record artifact identity is not derived")
    return parsed


def _entity_id_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Wikidata result envelope requests an invalid entity list")
    entity_ids = tuple(_entity_id(item) for item in value)
    if tuple(sorted(set(entity_ids))) != entity_ids:
        raise ValueError("Wikidata result envelope entity ids are not unique and sorted")
    return entity_ids


def _entity_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("the Wikidata entity id is not a string")
    digits = value[1:] if value.startswith("Q") else ""
    if not digits or not digits.isdigit() or digits.startswith("0"):
        raise ValueError("the Wikidata entity id is not an item id")
    return value


def _required_text(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"the Wikidata result carries no usable {key}")
    return value


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("the Wikidata result carries no timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("the Wikidata result timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise ValueError("the Wikidata result timestamp is not an instant in UTC")
    return parsed


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
