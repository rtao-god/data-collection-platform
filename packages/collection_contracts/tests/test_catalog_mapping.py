"""The campaign's declared Catalog mapping may only state languages it can establish.

`und` is the campaign's own way of saying "no language was established". A Catalog display
locale is a claim about the audience a name is shown to, so mapping `und` onto one would
publish a name under a language nothing in the evidence supports.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from collection_contracts import (
    CatalogCategoryMappingDocument,
    CatalogNameLocaleMapping,
    ExportCatalogNameLocale,
)

_REVISION = "019fdeab-5c00-7000-8000-000000000002"


def _document(**overrides: object) -> CatalogCategoryMappingDocument:
    payload: dict[str, object] = {
        "schema_revision": "catalog-category-mapping-v1",
        "site_key": "berlin-recording",
        "catalog_key": "berlin-recording-services",
        "configuration_revision_id": _REVISION,
        "coordinate_precision_meters": 25,
        "categories": [
            {"campaign_category": "recording_studio", "catalog_category": "recording-studio"}
        ],
        "name_locales": [{"campaign_locale": "de", "catalog_locale": "de-DE"}],
    }
    payload.update(overrides)
    return CatalogCategoryMappingDocument.model_validate(payload)


def test_an_established_language_is_mapped_to_a_display_locale() -> None:
    document = _document()

    assert document.name_locale_pairs == {"de": "de-DE"}


def test_the_undetermined_locale_cannot_be_given_a_display_locale() -> None:
    with pytest.raises(ValidationError, match="undetermined locale"):
        CatalogNameLocaleMapping(campaign_locale="und", catalog_locale="de-DE")


def test_a_mapping_document_cannot_smuggle_the_undetermined_locale() -> None:
    with pytest.raises(ValidationError, match="undetermined locale"):
        _document(name_locales=[{"campaign_locale": "und", "catalog_locale": "de-DE"}])


def test_a_sealed_binding_carries_the_declared_language_only() -> None:
    binding = ExportCatalogNameLocale(campaign_locale="de", catalog_locale="de-DE")

    assert (binding.campaign_locale, binding.catalog_locale) == ("de", "de-DE")
    with pytest.raises(ValidationError):
        ExportCatalogNameLocale(campaign_locale="und", catalog_locale="de-DE")
