"""Campaign-owned Wikidata entity query specification.

The Wikidata connector owns how an entity read is rendered into one `wbgetentities` URL and
how one entity document is adapted. Collection needs to publish the immutable
`wikidata_entity_query_spec` artifact the worker consumes, so the document shape lives here —
in the shared contracts layer — and is the single source of truth for both the producer and
the deployment that validates it.

The specification also carries the operator-authored classification the adaptation applies, so
the worker never has to read campaign state and a replayed run reproduces the exact same
records from the exact same bytes.

Deliberately free of any connector import: `collection_application` must not depend on
`connectors/*`.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

QUERY_SPEC_CONTRACT = "wikidata-entity-query-spec@2"
_SCHEMA_REVISION: Literal["wikidata-entity-query-spec-v2"] = "wikidata-entity-query-spec-v2"
_MAX_ENTITY_IDS = 50
_MAX_LABEL_LANGUAGES = 8
_ENTITY_ID = re.compile(r"^Q[1-9][0-9]{0,11}$")
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_BINDING_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CAMPAIGN_CATEGORY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _QueryModel(BaseModel):
    # The canonical document is camelCase, so the model must be able to read back
    # exactly the bytes this module writes.
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, alias_generator=_camel
    )


class WikidataClassificationSpec(_QueryModel):
    """The one class this campaign accepts, and the category it means.

    Operator-authored, never inferred: an entity carrying any class outside this declaration
    stays ambiguous and must not become a candidate.
    """

    studio_class_id: str
    campaign_category: str
    compatible_class_ids: tuple[str, ...] = ()

    @field_validator("studio_class_id")
    @classmethod
    def validate_studio_class(cls, value: str) -> str:
        if _ENTITY_ID.fullmatch(value) is None:
            raise ValueError("Wikidata classification studio class is not an item id")
        return value

    @field_validator("campaign_category")
    @classmethod
    def validate_campaign_category(cls, value: str) -> str:
        if _CAMPAIGN_CATEGORY.fullmatch(value) is None:
            raise ValueError("Wikidata classification campaign category is not a valid key")
        return value

    @field_validator("compatible_class_ids")
    @classmethod
    def validate_compatible_classes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("Wikidata compatible classes must be unique and sorted")
        invalid = [item for item in value if _ENTITY_ID.fullmatch(item) is None]
        if invalid:
            raise ValueError(f"Wikidata compatible classes are not item ids: {invalid}")
        return value


class WikidataEntityQuerySpec(_QueryModel):
    """Exactly which entity documents are read, in which label languages, as what meaning.

    The set is operator-authored and bounded: a pilot cannot turn into a crawl, and the
    same declared set always renders the same bytes so a replayed run publishes an
    identical artifact digest.
    """

    schema_revision: Literal["wikidata-entity-query-spec-v2"] = _SCHEMA_REVISION
    run_id: UUID
    binding_key: str
    entity_ids: tuple[str, ...]
    label_languages: tuple[str, ...]
    classification: WikidataClassificationSpec
    source_policy_digest: str

    @field_validator("binding_key")
    @classmethod
    def validate_binding_key(cls, value: str) -> str:
        if _BINDING_KEY.fullmatch(value) is None:
            raise ValueError("Wikidata query binding key is invalid")
        return value

    @field_validator("source_policy_digest")
    @classmethod
    def validate_policy_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("Wikidata query source policy digest is invalid")
        return value

    @model_validator(mode="after")
    def validate_bounded_set(self) -> WikidataEntityQuerySpec:
        if not 1 <= len(self.entity_ids) <= _MAX_ENTITY_IDS:
            raise ValueError(
                f"Wikidata query must declare between 1 and {_MAX_ENTITY_IDS} entity ids"
            )
        if tuple(sorted(set(self.entity_ids))) != self.entity_ids:
            raise ValueError("Wikidata query entity ids must be unique and sorted")
        invalid = [item for item in self.entity_ids if _ENTITY_ID.fullmatch(item) is None]
        if invalid:
            raise ValueError(f"Wikidata query declares non-item entity ids: {invalid}")
        if not 1 <= len(self.label_languages) <= _MAX_LABEL_LANGUAGES:
            raise ValueError("Wikidata query must declare between 1 and 8 label languages")
        if len(set(self.label_languages)) != len(self.label_languages):
            raise ValueError("Wikidata query label languages must be unique")
        invalid_tags = [
            item for item in self.label_languages if _LANGUAGE_TAG.fullmatch(item) is None
        ]
        if invalid_tags:
            raise ValueError(f"Wikidata query declares invalid language tags: {invalid_tags}")
        return self


def canonical_wikidata_query_spec_document(spec: WikidataEntityQuerySpec) -> bytes:
    """Render the exact bytes the Wikidata connector hashes and replays."""

    document = {
        "bindingKey": spec.binding_key,
        "classification": {
            "campaignCategory": spec.classification.campaign_category,
            "compatibleClassIds": list(spec.classification.compatible_class_ids),
            "studioClassId": spec.classification.studio_class_id,
        },
        "entityIds": list(spec.entity_ids),
        "labelLanguages": list(spec.label_languages),
        "runId": str(spec.run_id),
        "schemaRevision": spec.schema_revision,
        "sourcePolicyDigest": spec.source_policy_digest,
    }
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def wikidata_query_spec_digest(spec: WikidataEntityQuerySpec) -> str:
    return "sha256:" + hashlib.sha256(canonical_wikidata_query_spec_document(spec)).hexdigest()


def decode_wikidata_query_spec(content: bytes) -> WikidataEntityQuerySpec:
    """Read back exactly the bytes the producer published, or refuse them."""

    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Wikidata query specification is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("Wikidata query specification must be a JSON object")
    spec = WikidataEntityQuerySpec.model_validate(document)
    if canonical_wikidata_query_spec_document(spec) != content:
        raise ValueError("Wikidata query specification bytes are not canonical")
    return spec
