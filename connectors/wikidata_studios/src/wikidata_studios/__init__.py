"""Read-only Wikidata adapter for recording-studio entities.

The adapter translates one Wikidata entity document into the source record the existing
mapping owner consumes. It performs no network call on its own, invents no field, and
refuses an entity it cannot classify exactly. `fetch.py` performs the one bounded
`wbgetentities` read, and `result.py` seals the exact response the planner re-verifies.
"""

from wikidata_studios.adapt import (
    DEFAULT_LABEL_LANGUAGES,
    SOURCE_KEY,
    SOURCE_RECORD_CONTRACT,
    AdaptedWikidataRecord,
    adapt_entity,
)
from wikidata_studios.classification import WikidataClassificationMap
from wikidata_studios.fetch import (
    WikidataEndpointPolicy,
    WikidataFetchFailure,
    WikidataFetchResult,
    WikidataHttpClient,
)
from wikidata_studios.request import MAX_ENTITIES_PER_REQUEST, WikidataEntityRequest
from wikidata_studios.result import (
    MAX_RESULT_ENTITIES,
    RESULT_CONTRACT,
    WikidataEntityResult,
    seal_entity_result,
)

__all__ = [
    "DEFAULT_LABEL_LANGUAGES",
    "MAX_ENTITIES_PER_REQUEST",
    "MAX_RESULT_ENTITIES",
    "RESULT_CONTRACT",
    "SOURCE_KEY",
    "SOURCE_RECORD_CONTRACT",
    "AdaptedWikidataRecord",
    "WikidataClassificationMap",
    "WikidataEndpointPolicy",
    "WikidataEntityRequest",
    "WikidataEntityResult",
    "WikidataFetchFailure",
    "WikidataFetchResult",
    "WikidataHttpClient",
    "adapt_entity",
    "seal_entity_result",
]
