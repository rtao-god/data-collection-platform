from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from collection_contracts import ExtractionRequest
from extraction_core import ExtractionError, extract_html_document

_HTML = b"""<!doctype html>
<html lang="de">
  <head>
    <title>Example Studio</title>
    <link rel="canonical" href="https://studio.example/contact?utm_source=test#top">
  </head>
  <body>
    <h1>Example Studio</h1>
    <p>Recording, mixing and mastering. No grand piano.</p>
    <address>Example Street 1, 10115 Berlin, DE</address>
    <a href="tel:+49 30 123456">Call</a>
    <a href="mailto:Info@Studio.Example">Email</a>
    <a href="https://www.instagram.com/example.studio">Instagram</a>
  </body>
</html>
"""


class _Metadata:
    def extract(self, html_text: str, *, base_url: str) -> Mapping[str, object]:
        assert "Example Studio" in html_text
        assert base_url == "https://studio.example/"
        return {
            "json-ld": [
                {
                    "@type": ["LocalBusiness", "ProfessionalService"],
                    "name": "Example Studio",
                    "legalName": "Example Studio GmbH",
                    "url": "https://studio.example/",
                    "telephone": "+49 30 123456",
                    "email": "Info@Studio.Example",
                    "address": {
                        "@type": "PostalAddress",
                        "streetAddress": "Example Street 1",
                        "postalCode": "10115",
                        "addressLocality": "Berlin",
                        "addressCountry": "DE",
                    },
                    "knowsLanguage": ["de", "en"],
                    "offers": {
                        "price": "80",
                        "priceCurrency": "EUR",
                        "unitText": "hour",
                    },
                    "review_text": "This must never be retained.",
                }
            ],
            "microdata": [],
            "rdfa": [],
        }


def _request(content: bytes = _HTML) -> ExtractionRequest:
    return ExtractionRequest(
        source_record_id="source-record-example",
        raw_artifact_digest=f"sha256:{sha256(content).hexdigest()}",
        source_url="https://studio.example/",
        content_type="text/html",
        source_policy_digest=f"sha256:{'1' * 64}",
        extractor_revision="official-website-extractor@1",
        observed_at_utc=datetime(2026, 8, 14, tzinfo=UTC),
        locale="de-DE",
        allowed_fields=(
            "display_name",
            "legal_name",
            "website",
            "phone",
            "email",
            "address",
            "supported_languages",
            "hourly_price",
            "instagram",
        ),
        prohibited_fields=("review_text",),
    )


def test_extracts_structured_and_html_evidence_without_full_page_copy() -> None:
    record = extract_html_document(_HTML, _request(), metadata_extractor=_Metadata())

    assert record.entity_kind_candidates == ("organization", "place", "provider")
    assert record.source_categories == ("LocalBusiness", "ProfessionalService")
    assert {field.field_key for field in record.fields} >= {
        "display_name",
        "legal_name",
        "website",
        "phone",
        "email",
        "address",
        "supported_languages",
        "hourly_price",
        "instagram",
    }
    assert record.prohibited_fields[0].field_key == "review_text"
    assert record.prohibited_fields[0].evidence.evidence_span is None
    assert "This must never be retained" not in record.model_dump_json()
    assert all(len(block.text) <= 300 for block in record.text_blocks)
    assert any("No grand piano" in block.text for block in record.text_blocks)
    assert all(field.evidence.evidence_span for field in record.fields)
    assert record.content_digest.startswith("sha256:")


def test_same_document_and_request_produce_same_record_digest() -> None:
    first = extract_html_document(_HTML, _request(), metadata_extractor=_Metadata())
    second = extract_html_document(_HTML, _request(), metadata_extractor=_Metadata())

    assert first == second
    assert first.content_digest == second.content_digest


def test_rejects_raw_artifact_digest_mismatch() -> None:
    request = _request().model_copy(update={"raw_artifact_digest": f"sha256:{'0' * 64}"})

    with pytest.raises(ExtractionError) as failure:
        extract_html_document(_HTML, request, metadata_extractor=_Metadata())

    assert failure.value.code == "EXTRACTION_RAW_DIGEST_MISMATCH"


def test_rejects_document_over_request_limit() -> None:
    request = _request().model_copy(update={"maximum_document_bytes": 1024})
    content = _HTML + b"x" * 2048
    request = request.model_copy(
        update={"raw_artifact_digest": f"sha256:{sha256(content).hexdigest()}"}
    )

    with pytest.raises(ExtractionError) as failure:
        extract_html_document(content, request, metadata_extractor=_Metadata())

    assert failure.value.code == "EXTRACTION_DOCUMENT_TOO_LARGE"
