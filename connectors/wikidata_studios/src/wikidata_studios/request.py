"""Read-only Wikidata entity request contract.

The request is bounded and pinned to one host: the connector never discovers URLs, never
follows redirects and never asks for anything but the exact entity documents an operator
declared. Nothing here writes to Wikidata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from collection_contracts import owner_error
from collection_contracts.errors import OwnerContextError

_ENTITY_ID = re.compile(r"^Q[1-9][0-9]{0,11}$")
_ENTITY_DATA_HOST = "www.wikidata.org"
# One wbgetentities call carries at most this many ids, so a pilot can never turn into an
# unbounded crawl by accident.
MAX_ENTITIES_PER_REQUEST = 50
_API_PATH = "/w/api.php"


@dataclass(frozen=True, slots=True)
class WikidataEntityRequest:
    """Exactly which entity documents to read, and the acquisition time of the read."""

    entity_ids: tuple[str, ...]
    retrieved_at_utc: datetime
    run_id: UUID

    def __post_init__(self) -> None:
        if not self.entity_ids:
            raise _request_error(
                "WIKIDATA_REQUEST_EMPTY",
                "The Wikidata request declares no entities.",
                {"entityCount": 0},
            )
        if len(self.entity_ids) > MAX_ENTITIES_PER_REQUEST:
            raise _request_error(
                "WIKIDATA_REQUEST_LIMIT_EXCEEDED",
                "The Wikidata request exceeds the bounded entity limit.",
                {"entityCount": len(self.entity_ids), "limit": MAX_ENTITIES_PER_REQUEST},
            )
        duplicates = sorted({item for item in self.entity_ids if self.entity_ids.count(item) > 1})
        if duplicates:
            raise _request_error(
                "WIKIDATA_REQUEST_DUPLICATE_ENTITY",
                "The Wikidata request names the same entity more than once.",
                {"duplicateEntityIds": ",".join(duplicates)},
            )
        invalid = sorted(item for item in self.entity_ids if _ENTITY_ID.fullmatch(item) is None)
        if invalid:
            raise _request_error(
                "WIKIDATA_REQUEST_ENTITY_ID_INVALID",
                "The Wikidata request names an entity id that is not a Wikidata item.",
                {"invalidEntityIds": ",".join(invalid)},
            )
        if (
            self.retrieved_at_utc.tzinfo is None
            or self.retrieved_at_utc.utcoffset() != UTC.utcoffset(None)
        ):
            raise _request_error(
                "WIKIDATA_REQUEST_TIMESTAMP_INVALID",
                "The Wikidata acquisition time is not an instant in UTC.",
                {"retrievedAtUtc": self.retrieved_at_utc.isoformat()},
            )

    def entity_ids_query(self) -> str:
        """The exact read-only GET the acquirer may perform for this request."""

        return (
            f"https://{_ENTITY_DATA_HOST}{_API_PATH}?action=wbgetentities"
            f"&ids={'|'.join(self.entity_ids)}&format=json"
            "&props=info|labels|descriptions|claims"
        )

    def entity_source_url(self, entity_id: str) -> str:
        """The citable evidence address of one entity document.

        EntityData is revision-addressable: the revision this record was adapted from
        stays retrievable, so a claim can be re-checked later without trusting us.
        """

        if _ENTITY_ID.fullmatch(entity_id) is None:
            raise _request_error(
                "WIKIDATA_REQUEST_ENTITY_ID_INVALID",
                "The requested entity id is not a Wikidata item.",
                {"entityId": entity_id},
            )
        if entity_id not in self.entity_ids:
            raise _request_error(
                "WIKIDATA_REQUEST_ENTITY_UNKNOWN",
                "The source URL was asked for an entity this request did not declare.",
                {"entityId": entity_id},
            )
        return f"https://{_ENTITY_DATA_HOST}/wiki/Special:EntityData/{entity_id}.json"


def _request_error(code: str, message: str, context: dict[str, object]) -> OwnerContextError:
    return owner_error(
        error_type="collection/wikidata-request-invalid",
        owner="WikidataStudios",
        code=code,
        message=message,
        context=context,
        required_action="Correct the bounded Wikidata request and run the acquisition again.",
        correlation_id=str(context.get("runId") or "wikidata-request"),
    )
