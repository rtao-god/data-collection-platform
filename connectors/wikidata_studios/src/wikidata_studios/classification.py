"""Explicit Wikidata classification -> campaign category decision.

Wikidata does not publish a studio classification we may accept as a campaign category on
its own: an item is classified by an arbitrary set of `instance of` statements that anyone
can change. So the correspondence is operator-authored configuration, and an entity whose
statements do not match it exactly stays blocked instead of being guessed into a category.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from collection_contracts import owner_error
from collection_contracts.errors import OwnerContextError

_ITEM_ID = re.compile(r"^Q[1-9][0-9]{0,11}$")
_CAMPAIGN_CATEGORY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class WikidataClassificationMap:
    """One declared correspondence between a Wikidata class set and a campaign category."""

    studio_class_id: str
    campaign_category: str
    # Classes that may appear next to the studio class without changing its meaning.
    # Anything else is ambiguous and stays blocked.
    compatible_class_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if _ITEM_ID.fullmatch(self.studio_class_id) is None:
            raise _classification_error(
                "WIKIDATA_CLASSIFICATION_CONFIG_INVALID",
                "The Wikidata classification map names a non-item studio class.",
                {"studioClassId": self.studio_class_id},
            )
        if _CAMPAIGN_CATEGORY.fullmatch(self.campaign_category) is None:
            raise _classification_error(
                "WIKIDATA_CLASSIFICATION_CONFIG_INVALID",
                "The Wikidata classification map targets a non-campaign category.",
                {"campaignCategory": self.campaign_category},
            )
        invalid = sorted(
            item for item in self.compatible_class_ids if _ITEM_ID.fullmatch(item) is None
        )
        if invalid:
            raise _classification_error(
                "WIKIDATA_CLASSIFICATION_CONFIG_INVALID",
                "The Wikidata classification map names a non-item compatible class.",
                {"compatibleClassIds": ",".join(invalid)},
            )
        if self.studio_class_id in self.compatible_class_ids:
            raise _classification_error(
                "WIKIDATA_CLASSIFICATION_CONFIG_INVALID",
                "The Wikidata classification map lists its own studio class as compatible.",
                {"studioClassId": self.studio_class_id},
            )

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> WikidataClassificationMap:
        """Load the operator-authored map, refusing any shape it does not fully describe."""

        required = {"studioClassId", "campaignCategory"}
        allowed = required | {"compatibleClassIds"}
        if not required <= set(document) or set(document) - allowed:
            raise _classification_error(
                "WIKIDATA_CLASSIFICATION_CONFIG_INVALID",
                "The Wikidata classification document has an unexpected shape.",
                {"fields": ",".join(sorted(document))},
            )
        compatible = document.get("compatibleClassIds", ())
        if not isinstance(compatible, Sequence) or isinstance(compatible, str):
            raise _classification_error(
                "WIKIDATA_CLASSIFICATION_CONFIG_INVALID",
                "The Wikidata classification map declares a non-list of compatible classes.",
                {"compatibleClassIds": str(compatible)[:120]},
            )
        return cls(
            studio_class_id=str(document["studioClassId"]),
            campaign_category=str(document["campaignCategory"]),
            compatible_class_ids=frozenset(str(item) for item in compatible),
        )

    def campaign_category_for(self, class_ids: Sequence[str]) -> str:
        """The declared campaign category, or a coded refusal.

        The classification is accepted only when the studio class is present and every
        other class is one this map already declared compatible. A partially matching
        entity is ambiguous: an operator decides what it is, not the adapter.
        """

        present = tuple(sorted(set(class_ids)))
        if not present:
            raise _classification_error(
                "WIKIDATA_CLASSIFICATION_MISSING",
                "The Wikidata entity declares no instance-of statement.",
                {"declaredClasses": ""},
            )
        if self.studio_class_id not in present:
            raise _classification_error(
                "WIKIDATA_CLASSIFICATION_UNKNOWN",
                "The Wikidata entity is not classified as the mapped studio class.",
                {"declaredClasses": ",".join(present), "studioClassId": self.studio_class_id},
            )
        unexpected = tuple(
            item
            for item in present
            if item != self.studio_class_id and item not in self.compatible_class_ids
        )
        if unexpected:
            raise _classification_error(
                "WIKIDATA_CLASSIFICATION_AMBIGUOUS",
                "The Wikidata entity carries classes the mapping does not declare.",
                {
                    "declaredClasses": ",".join(present),
                    "unexpectedClasses": ",".join(unexpected),
                },
            )
        return self.campaign_category


def _classification_error(code: str, message: str, context: dict[str, object]) -> OwnerContextError:
    return owner_error(
        error_type="collection/wikidata-classification-invalid",
        owner="WikidataStudios",
        code=code,
        message=message,
        context=context,
        required_action=(
            "Correct the Wikidata classification mapping or exclude this entity, then run "
            "the acquisition again."
        ),
        correlation_id=str(
            context.get("entityId") or context.get("declaredClasses") or "wikidata-classification"
        ),
    )
