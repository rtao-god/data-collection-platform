"""Exact Wikidata response -> the immutable result envelope the planner re-verifies.

The worker publishes two artifacts for one acquisition: the untouched response bytes and
this envelope. The envelope repeats the response digest and the exact entity set that was
asked for, so the planner can prove that every source record it maps comes from the response
it claims and that no entity was added, dropped or substituted on the way.

The wire shape and its digests belong to `collection_contracts`, because the consumers that
schedule and verify work must read it without depending on this connector. What lives here is
only the source semantics: which class becomes which category, and which claim becomes which
field.
"""

from __future__ import annotations

import json
from collections import abc
from dataclasses import dataclass
from datetime import UTC, datetime

from collection_contracts import owner_error
from collection_contracts.errors import OwnerContextError
from collection_contracts.wikidata_entity_query_spec import WikidataEntityQuerySpec
from collection_contracts.wikidata_entity_result import (
    RESULT_CONTRACT,
    build_wikidata_entity_result,
)
from wikidata_studios.adapt import adapt_entity
from wikidata_studios.classification import WikidataClassificationMap
from wikidata_studios.request import WikidataEntityRequest

MAX_RESULT_ENTITIES = 50

__all__ = [
    "RESULT_CONTRACT",
    "WikidataEntityResult",
    "seal_entity_result",
]


@dataclass(frozen=True, slots=True)
class WikidataEntityResult:
    """The exact response bytes, the envelope, and their identities."""

    raw_content: bytes
    raw_digest: str
    envelope_content: bytes
    envelope_digest: str
    response_digest: str
    retrieved_at_utc: datetime
    requested_entity_ids: tuple[str, ...]
    accepted_entity_ids: tuple[str, ...]
    rejected_entity_ids: tuple[str, ...]


def seal_entity_result(
    payload: bytes,
    *,
    spec: WikidataEntityQuerySpec,
    retrieved_at_utc: datetime,
    classification: WikidataClassificationMap | None = None,
) -> WikidataEntityResult:
    """Seal one response for the declared entity set, adapting every entity it answered.

    The response must describe exactly the requested items: an extra item means the read
    answered a different question, and a missing one means the bounded request was not
    served, so both are refusals rather than a partial success. Inside that set one
    unusable entity is isolated: its siblings keep their records and the refusal is
    published with the reason, so a run never silently loses an entity.
    """

    requested = tuple(sorted(set(spec.entity_ids)))
    if not requested:
        raise _result_error(
            "WIKIDATA_RESULT_EMPTY_REQUEST",
            "The Wikidata result was built for an empty entity set.",
            {"entityCount": 0},
        )
    if len(requested) > MAX_RESULT_ENTITIES:
        raise _result_error(
            "WIKIDATA_RESULT_LIMIT_EXCEEDED",
            "The Wikidata result exceeds the bounded entity limit.",
            {"entityCount": len(requested), "limit": MAX_RESULT_ENTITIES},
        )
    if retrieved_at_utc.tzinfo is None or retrieved_at_utc.utcoffset() != UTC.utcoffset(None):
        raise _result_error(
            "WIKIDATA_RESULT_TIMESTAMP_INVALID",
            "The Wikidata retrieval time is not an instant in UTC.",
            {"retrievedAtUtc": retrieved_at_utc.isoformat()},
        )
    entities = _entities(payload)
    returned = tuple(sorted(str(key) for key in entities))
    if returned != requested:
        raise _result_error(
            "WIKIDATA_RESULT_ENTITY_SET_MISMATCH",
            "The Wikidata response does not describe exactly the requested entities.",
            {
                "requestedEntityIds": ",".join(requested),
                "returnedEntityIds": ",".join(returned),
            },
        )
    mapping = classification or _declared_classification(spec)
    request = WikidataEntityRequest(
        entity_ids=requested,
        retrieved_at_utc=retrieved_at_utc,
        run_id=spec.run_id,
    )
    documents: list[abc.Mapping[str, object]] = []
    rejected: list[tuple[str, str, str]] = []
    for entity_id in requested:
        entity_payload = entities[entity_id]
        if not isinstance(entity_payload, abc.Mapping):
            rejected.append(
                (entity_id, "WIKIDATA_ENTITY_PAYLOAD_INVALID", "The entity is not an object.")
            )
            continue
        try:
            record = adapt_entity(
                {"entities": {entity_id: entity_payload}},
                entity_id=entity_id,
                classification=mapping,
                request=request,
                label_languages=spec.label_languages,
            )
        except OwnerContextError as exc:
            # One unusable entity must not discard its valid siblings. The refusal is
            # published with the entity that caused it, never dropped.
            rejected.append((entity_id, exc.envelope.code, exc.envelope.message))
        except ValueError as exc:
            rejected.append((entity_id, "WIKIDATA_ENTITY_VALUE_INVALID", str(exc)))
        else:
            documents.append(record.document)
    if not documents:
        raise _result_error(
            "WIKIDATA_RESULT_ALL_ENTITIES_UNREPRESENTABLE",
            "No requested entity could be adapted into a source record.",
            {"rejectedEntityIds": ",".join(item[0] for item in rejected)},
        )
    envelope_content = build_wikidata_entity_result(
        requested_entity_ids=requested,
        retrieved_at_utc=retrieved_at_utc,
        response_digest=_digest(payload),
        records=documents,
        rejected=rejected,
    )
    return WikidataEntityResult(
        raw_content=payload,
        raw_digest=_digest(payload),
        envelope_content=envelope_content,
        envelope_digest=_digest(envelope_content),
        response_digest=_digest(payload),
        retrieved_at_utc=retrieved_at_utc,
        requested_entity_ids=requested,
        accepted_entity_ids=tuple(sorted(str(item["sourceRecordId"]) for item in documents)),
        rejected_entity_ids=tuple(item[0] for item in rejected),
    )


def _entities(payload: bytes) -> abc.Mapping[str, object]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _result_error(
            "WIKIDATA_RESULT_PAYLOAD_INVALID",
            "The Wikidata response is not valid JSON.",
            {"causeType": type(exc).__name__},
        ) from exc
    if not isinstance(document, abc.Mapping):
        raise _result_error(
            "WIKIDATA_RESULT_PAYLOAD_INVALID",
            "The Wikidata response is not a JSON object.",
            {"causeType": "not_a_mapping"},
        )
    entities = document.get("entities")
    if not isinstance(entities, abc.Mapping):
        raise _result_error(
            "WIKIDATA_RESULT_ENTITIES_MISSING",
            "The Wikidata response carries no entity map.",
            {"causeType": "entities_missing"},
        )
    return entities


def _declared_classification(spec: WikidataEntityQuerySpec) -> WikidataClassificationMap:
    return WikidataClassificationMap(
        studio_class_id=spec.classification.studio_class_id,
        campaign_category=spec.classification.campaign_category,
        compatible_class_ids=frozenset(spec.classification.compatible_class_ids),
    )


def _digest(content: bytes) -> str:
    from hashlib import sha256

    return "sha256:" + sha256(content).hexdigest()


def _result_error(code: str, message: str, context: dict[str, object]) -> OwnerContextError:
    return owner_error(
        error_type="collection/wikidata-result-invalid",
        owner="WikidataStudios",
        code=code,
        message=message,
        context=context,
        required_action=(
            "Repeat the bounded Wikidata acquisition for the exact declared entity set."
        ),
        correlation_id=str(
            context.get("entityId") or context.get("requestedEntityIds") or "wikidata-result"
        ),
    )
