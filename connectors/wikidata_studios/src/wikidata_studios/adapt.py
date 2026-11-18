"""Wikidata entity document -> source record the mapping owner already consumes.

The adapter only translates. It never fills a gap: a field the entity does not publish is
absent from the record, so the mapping owner downstream emits `NOT_OBSERVED` instead of an
invented value. Every emitted value keeps the statement it was read from, the entity
revision and the acquisition time, because the Catalog later publishes a claim per field
and a claim without that detail cannot be checked.
"""

from __future__ import annotations

import json
from collections import abc
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid5

from collection_contracts import owner_error
from collection_contracts.errors import OwnerContextError
from wikidata_studios.classification import WikidataClassificationMap
from wikidata_studios.request import WikidataEntityRequest

SOURCE_RECORD_CONTRACT = "wikidata-source-record@1"
SOURCE_KEY = "wikidata"
# A name or an address is published under a language. The adapter reads the entity's own
# language tags in this declared order and never translates, transliterates or assumes.
DEFAULT_LABEL_LANGUAGES = ("de", "en")

_STATEMENT_NAMESPACE = UUID("9a1c5d77-3f2e-5c48-8b10-6d2f4e8c1a70")
_LOCATION_CLAIM = "P625"
_WEBSITE_CLAIM = "P856"
_STREET_ADDRESS_CLAIM = "P6375"
_INSTANCE_OF_CLAIM = "P31"


@dataclass(frozen=True, slots=True)
class AdaptedWikidataRecord:
    """One sealed source record plus the evidence detail an operator may need to review."""

    document: dict[str, Any]
    content: bytes
    digest: str
    record_digest: str
    # The campaign category this entity was accepted as, with the statement that made the
    # classification applicable. The same claim is also the sealed record's
    # `category_<campaign category>` field; this summary keeps the operator-facing detail
    # (statement id, mapped class) in one place.
    campaign_category: str
    classification_evidence: dict[str, str]


def adapt_entity(
    payload: abc.Mapping[str, Any],
    *,
    entity_id: str,
    classification: WikidataClassificationMap,
    request: WikidataEntityRequest,
    label_languages: tuple[str, ...] = DEFAULT_LABEL_LANGUAGES,
) -> AdaptedWikidataRecord:
    """Adapt one entity document exactly as Wikidata published it."""

    entity = _entity(payload, entity_id)
    revision_id = entity.get("lastrevid")
    if not isinstance(revision_id, int) or revision_id <= 0:
        raise _adapt_error(
            "WIKIDATA_ENTITY_REVISION_MISSING",
            "The Wikidata entity document carries no revision identity.",
            {"entityId": entity_id},
        )
    claims = _claims(entity, entity_id=entity_id)
    evidenced: dict[str, list[dict[str, str]]] = {}

    name = _label(entity, entity_id=entity_id, languages=label_languages)
    if name is not None:
        evidenced["display_name"] = [name]
    point = _point(claims, entity_id=entity_id)
    if point is not None:
        evidenced["coordinates"] = [point]
    website = _website(claims, entity_id=entity_id)
    if website is not None:
        evidenced["website"] = [website]
    address = _street_address(claims, entity_id=entity_id, languages=label_languages)
    if address is not None:
        evidenced["address"] = [address]
    category = classification.campaign_category_for(_class_ids(claims))
    statement_id = _classification_statement_id(
        claims,
        entity_id=entity_id,
        studio_class_id=classification.studio_class_id,
    )
    # The classification is a claim about the entity, so it travels as one more keyed
    # claim whose evidence is the instance-of statement it was accepted from. The field
    # key is the campaign's valid-key convention `category_<campaign category>`: the
    # observation contract has no separate classification slot and its field keys never
    # contain a separator that could disguise configuration as an observed tag.
    evidenced[f"category_{category}"] = [
        {
            "value": category,
            "pointer": _statement_pointer(entity_id, statement_id),
        }
    ]

    fields = sorted(evidenced)
    document: dict[str, Any] = {
        "contract": SOURCE_RECORD_CONTRACT,
        "sourceRecordId": _source_record_id(entity_id),
        "entityRevisionId": revision_id,
        "observedAtUtc": _utc_text(request.retrieved_at_utc),
        "datasetRelease": f"wikidata-entity-revision:{revision_id}",
        "runId": str(request.run_id),
        "sourceUrl": request.entity_source_url(entity_id),
        # CC0 needs no attribution; the credit is kept because the product wants to show
        # where the record came from, not because the license demands it.
        "attribution": "Wikidata (CC0 1.0)",
        "fields": fields,
        "records": [{"sourceRecordId": _source_record_id(entity_id), **evidenced}],
    }
    digest = _seal(
        document,
        entity_id=entity_id,
        revision_id=revision_id,
        observed_at_utc=_utc_text(request.retrieved_at_utc),
    )
    content = json.dumps(
        document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return AdaptedWikidataRecord(
        document=document,
        content=content,
        digest=digest,
        record_digest=_digest(content),
        campaign_category=category,
        classification_evidence={
            "value": category,
            "pointer": f"/claims/{_INSTANCE_OF_CLAIM}/{statement_id}",
            "statementId": statement_id,
            "studioClassId": classification.studio_class_id,
        },
    )


def _seal(
    document: dict[str, Any],
    *,
    entity_id: str,
    revision_id: int,
    observed_at_utc: str,
) -> str:
    """Close the document with its artifact identity and digest.

    The digest covers the document without its own digest field, so the producer manifest
    stays verifiable and editing the record cannot satisfy it. The artifact identity covers
    the observation instant as well: the document records when the source was read, so two
    acquisitions of the same revision are two distinct immutable artifacts, while a replay of
    the same acquisition reproduces the same identity and the same bytes.
    """

    document["artifactRecordId"] = str(
        uuid5(_STATEMENT_NAMESPACE, f"{entity_id}|{revision_id}|{observed_at_utc}")
    )
    document["artifactDigest"] = _digest(_canonical_bytes(document))
    return str(document["artifactDigest"])


def _canonical_bytes(document: abc.Mapping[str, Any]) -> bytes:
    return json.dumps(
        {key: value for key, value in document.items() if key != "artifactDigest"},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _entity(payload: abc.Mapping[str, Any], entity_id: str) -> abc.Mapping[str, Any]:
    entities = payload.get("entities")
    if not isinstance(entities, abc.Mapping) or entity_id not in entities:
        raise _adapt_error(
            "WIKIDATA_ENTITY_PAYLOAD_INVALID",
            "The Wikidata response does not contain the requested entity.",
            {"entityId": entity_id},
        )
    entity = entities[entity_id]
    if not isinstance(entity, abc.Mapping) or entity.get("missing") is not None:
        raise _adapt_error(
            "WIKIDATA_ENTITY_MISSING",
            "The requested Wikidata entity does not exist.",
            {"entityId": entity_id},
        )
    return entity


def _claims(entity: abc.Mapping[str, Any], *, entity_id: str) -> abc.Mapping[str, Any]:
    claims = entity.get("claims", {})
    if not isinstance(claims, abc.Mapping):
        raise _adapt_error(
            "WIKIDATA_ENTITY_PAYLOAD_INVALID",
            "The Wikidata entity claims are not a statement map.",
            {"entityId": entity_id},
        )
    return claims


def _label(
    entity: abc.Mapping[str, Any], *, entity_id: str, languages: tuple[str, ...]
) -> dict[str, str] | None:
    """The published label in the first declared language that has one."""

    labels = entity.get("labels", {})
    if not isinstance(labels, abc.Mapping):
        raise _adapt_error(
            "WIKIDATA_ENTITY_PAYLOAD_INVALID",
            "The Wikidata entity labels are not a language map.",
            {"entityId": entity_id},
        )
    for language in languages:
        value = labels.get(language)
        if isinstance(value, abc.Mapping) and isinstance(value.get("value"), str):
            text = " ".join(str(value["value"]).split())
            if text:
                return {
                    "value": text,
                    "pointer": f"/entities/{entity_id}/labels/{language}",
                    "locale": language,
                }
    return None


def _statements(claims: abc.Mapping[str, Any], claim: str) -> tuple[abc.Mapping[str, Any], ...]:
    rows = claims.get(claim, ())
    if not isinstance(rows, abc.Sequence):
        return ()
    return tuple(item for item in rows if isinstance(item, abc.Mapping))


def _class_ids(claims: abc.Mapping[str, Any]) -> tuple[str, ...]:
    class_ids: list[str] = []
    for statement in _statements(claims, _INSTANCE_OF_CLAIM):
        value = _datavalue(statement)
        entity_id = value.get("id") if isinstance(value, abc.Mapping) else None
        if isinstance(entity_id, str):
            class_ids.append(entity_id)
    return tuple(class_ids)


def _point(claims: abc.Mapping[str, Any], *, entity_id: str) -> dict[str, str] | None:
    """The entity's own coordinate statements, refusing a contradictory pair.

    Two different published points are a source conflict, not something to average: a
    shifted coordinate silently moves a studio into another market area.
    """

    points: dict[tuple[str, str], dict[str, str]] = {}
    for statement in _statements(claims, _LOCATION_CLAIM):
        value = _datavalue(statement)
        if not isinstance(value, abc.Mapping):
            continue
        latitude = value.get("latitude")
        longitude = value.get("longitude")
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
            continue
        key = (_decimal_text(latitude), _decimal_text(longitude))
        points[key] = {
            "value": json.dumps(
                {"latitude": key[0], "longitude": key[1]},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "pointer": _claim_pointer(entity_id, _LOCATION_CLAIM, statement),
            "valueKind": "geo_point",
            "geometrySourceRecordId": _source_record_id(entity_id),
        }
    if not points:
        return None
    if len(points) > 1:
        raise _adapt_error(
            "WIKIDATA_COORDINATE_CONFLICT",
            "The Wikidata entity publishes more than one distinct coordinate.",
            {"entityId": entity_id, "distinctPoints": len(points)},
        )
    return next(iter(points.values()))


def _claim_pointer(entity_id: str, claim: str, statement: abc.Mapping[str, Any]) -> str:
    """The exact statement address a value was read from."""

    return f"/entities/{entity_id}/claims/{claim}/{statement.get('id', '')}"


def _website(claims: abc.Mapping[str, Any], *, entity_id: str) -> dict[str, str] | None:
    for statement in _statements(claims, _WEBSITE_CLAIM):
        value = _datavalue(statement)
        if isinstance(value, str) and value.strip():
            return {
                "value": value.strip(),
                "pointer": _claim_pointer(entity_id, _WEBSITE_CLAIM, statement),
                # P856 is typed as a URL by Wikidata, so the record declares the kind the
                # source property already asserts instead of leaving the mapper to guess.
                "valueKind": "url",
            }
    return None


def _street_address(
    claims: abc.Mapping[str, Any], *, entity_id: str, languages: tuple[str, ...]
) -> dict[str, str] | None:
    """The published street address in a declared language, kept exactly as written."""

    candidates: dict[str, dict[str, str]] = {}
    for statement in _statements(claims, _STREET_ADDRESS_CLAIM):
        value = _datavalue(statement)
        if not isinstance(value, abc.Mapping):
            continue
        text = value.get("text")
        language = value.get("language")
        if not isinstance(text, str) or not text.strip():
            continue
        if not isinstance(language, str) or language not in languages:
            continue
        candidates[language] = {
            "value": " ".join(text.split()),
            "pointer": _claim_pointer(entity_id, _STREET_ADDRESS_CLAIM, statement),
            "locale": language,
        }
    for language in languages:
        if language in candidates:
            return candidates[language]
    return None


def _classification_statement_id(
    claims: abc.Mapping[str, Any], *, entity_id: str, studio_class_id: str
) -> str:
    """The exact instance-of statement that carries the mapped studio class.

    The class set is unordered and an entity often declares several classes, so the
    accepted statement is the one whose value is the mapped class. A classification
    without that statement cannot be evidenced at all, and an entity that repeats the
    class in two statements is ambiguous rather than arbitrarily resolved.
    """

    statement_ids: list[str] = []
    for statement in _statements(claims, _INSTANCE_OF_CLAIM):
        value = _datavalue(statement)
        entity_class = value.get("id") if isinstance(value, abc.Mapping) else None
        if entity_class != studio_class_id:
            continue
        statement_id = statement.get("id")
        if not isinstance(statement_id, str) or not statement_id:
            raise _adapt_error(
                "WIKIDATA_CLASSIFICATION_STATEMENT_MISSING",
                "The instance-of statement carrying the studio class has no statement id.",
                {"entityId": entity_id, "studioClassId": studio_class_id},
            )
        statement_ids.append(statement_id)
    if not statement_ids:
        raise _adapt_error(
            "WIKIDATA_CLASSIFICATION_STATEMENT_MISSING",
            "No instance-of statement carries the mapped studio class.",
            {"entityId": entity_id, "studioClassId": studio_class_id},
        )
    if len(set(statement_ids)) > 1:
        raise _adapt_error(
            "WIKIDATA_CLASSIFICATION_STATEMENT_AMBIGUOUS",
            "The mapped studio class is repeated in more than one statement.",
            {"entityId": entity_id, "statementCount": len(set(statement_ids))},
        )
    return statement_ids[0]


def _statement_pointer(entity_id: str, statement_id: str) -> str:
    return f"/entities/{entity_id}/claims/{_INSTANCE_OF_CLAIM}/{statement_id}"


def _datavalue(statement: abc.Mapping[str, Any]) -> Any:
    snak = statement.get("mainsnak")
    if not isinstance(snak, abc.Mapping):
        return None
    value = snak.get("datavalue")
    if not isinstance(value, abc.Mapping):
        return None
    return value.get("value")


def _decimal_text(value: float) -> str:
    """Keep Wikidata's own decimal digits; the value is never rounded or rescaled."""

    return format(value, "f")


def _source_record_id(entity_id: str) -> str:
    return f"{SOURCE_KEY}:{entity_id}"


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _adapt_error(code: str, message: str, context: dict[str, object]) -> OwnerContextError:
    return owner_error(
        error_type="collection/wikidata-record-invalid",
        owner="WikidataStudios",
        code=code,
        message=message,
        context=context,
        required_action=(
            "Exclude this entity from the bounded acquisition or correct its evidence, then "
            "run the acquisition again."
        ),
        correlation_id=str(context.get("entityId") or context.get("runId") or "wikidata-adapt"),
    )
