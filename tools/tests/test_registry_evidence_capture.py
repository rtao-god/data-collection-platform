"""Structured facts come from the artifact itself, with the block and JSON path named."""

from __future__ import annotations

from tools import registry_evidence_capture as capture


def _page(*blocks: str, visible: str = "Willkommen im Studio.") -> bytes:
    scripts = "".join(
        f'<script type="application/ld+json">{block}</script>' for block in blocks
    )
    return f"<html><head>{scripts}</head><body><p>{visible}</p></body></html>".encode()


def test_fact_stated_only_in_jsonld_is_extracted_with_its_pointer() -> None:
    page = _page(
        '{"@context": "https://schema.org", "@type": "LocalBusiness",'
        ' "name": "Flyy Studios", "address": {"@type": "PostalAddress",'
        ' "streetAddress": "Liebermannstraße 77", "postalCode": "13088",'
        ' "addressLocality": "Berlin"}}'
    )

    structured = capture.extract_jsonld_fact(
        page, ["Liebermannstraße 77", "13088"], subject_name="Flyy Studios"
    )

    assert structured is not None
    assert structured["medium"] == "application/ld+json"
    assert structured["blockIndex"] == 0
    assert structured["pointers"]["Liebermannstraße 77"].endswith("/streetAddress")
    assert structured["pointers"]["13088"].endswith("/postalCode")
    assert "Liebermannstraße 77" in structured["excerpt"]


def test_jsonld_fact_of_another_organization_is_refused() -> None:
    page = _page(
        '{"@type": "LocalBusiness", "name": "Other Studio",'
        ' "address": {"streetAddress": "Liebermannstraße 77"}}'
    )

    assert (
        capture.extract_jsonld_fact(page, ["Liebermannstraße 77"], subject_name="Flyy Studios")
        is None
    )


def test_jsonld_without_the_fact_is_not_evidence() -> None:
    page = _page('{"@type": "LocalBusiness", "name": "Flyy Studios", "telephone": "+4930123"}')

    assert capture.extract_jsonld_fact(page, ["Liebermannstraße 77"]) is None


def test_invalid_jsonld_block_is_skipped_and_never_guessed() -> None:
    broken = _page('{"@type": "LocalBusiness", "name": "Flyy Studios", broken json')
    assert capture.extract_jsonld_fact(broken, ["+4930123"]) is None

    page = _page(
        '{"@type": "LocalBusiness", "name": "Flyy Studios", broken json',
        '{"@type": "LocalBusiness", "name": "Flyy Studios", "telephone": "+4930123"}',
    )
    structured = capture.extract_jsonld_fact(page, ["+4930123"])
    assert structured is not None
    assert structured["blockIndex"] == 1


def test_jsonld_context_is_never_fetched_and_visible_text_ignores_scripts() -> None:
    page = _page('{"@type": "LocalBusiness", "name": "Flyy Studios", "telephone": "+4930123"}')

    visible = capture._text_of(page, "text/html")

    assert "+4930123" not in visible
    assert capture.extract_jsonld_fact(page, ["+4930123"]) is not None
