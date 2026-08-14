from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal, Protocol, cast
from urllib.parse import unquote, urlsplit

from lxml import etree, html
from lxml.html import HtmlElement

from collection_contracts import (
    EvidenceLocatorKind,
    EvidenceReference,
    EvidenceTextBlock,
    ExtractedAddressValue,
    ExtractedBooleanValue,
    ExtractedEmailValue,
    ExtractedField,
    ExtractedMoneyValue,
    ExtractedPhoneValue,
    ExtractedRecord,
    ExtractedRecordPayload,
    ExtractedStringSetValue,
    ExtractedTextValue,
    ExtractedUrlValue,
    ExtractionIssue,
    ExtractionRequest,
    ProhibitedFieldEvidence,
    seal_extracted_record,
)

_TEXT_BLOCK_XPATH = "//h1|//h2|//h3|//h4|//h5|//h6|//p|//li|//dt|//dd|//address"
_EntityKindCandidate = Literal["organization", "place", "provider"]

_SCHEMA_TYPE_TO_ENTITY_KINDS: dict[str, tuple[_EntityKindCandidate, ...]] = {
    "Organization": ("organization",),
    "Corporation": ("organization",),
    "LocalBusiness": ("organization", "place"),
    "ProfessionalService": ("organization", "place", "provider"),
    "Place": ("place",),
    "Person": ("provider",),
}


class EmbeddedMetadataExtractor(Protocol):
    def extract(self, html_text: str, *, base_url: str) -> Mapping[str, object]: ...


class ExtructMetadataExtractor:
    def extract(self, html_text: str, *, base_url: str) -> Mapping[str, object]:
        import extruct

        result = extruct.extract(
            html_text,
            base_url=base_url,
            syntaxes=["json-ld", "microdata", "rdfa"],
            uniform=True,
        )
        if not isinstance(result, Mapping):
            raise ExtractionError(
                code="EXTRACTION_EMBEDDED_METADATA_INVALID",
                message="Embedded metadata extraction returned an invalid result.",
            )
        return cast(Mapping[str, object], result)


class ExtractionError(ValueError):
    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(slots=True)
class _Accumulator:
    request: ExtractionRequest
    fields: list[ExtractedField]
    prohibited: list[ProhibitedFieldEvidence]
    text_blocks: list[EvidenceTextBlock]
    issues: list[ExtractionIssue]
    entity_kinds: set[_EntityKindCandidate]
    source_categories: set[str]
    _field_identities: set[tuple[str, str, str]]
    _prohibited_identities: set[tuple[str, str, str]]
    _text_identities: set[tuple[str, str]]

    @classmethod
    def create(cls, request: ExtractionRequest) -> _Accumulator:
        return cls(request, [], [], [], [], set(), set(), set(), set(), set())

    def add_field(
        self,
        field_key: str,
        value: object,
        *,
        locator_kind: EvidenceLocatorKind,
        locator_value: str,
        raw_evidence: object,
    ) -> None:
        evidence = _evidence(
            self.request,
            locator_kind=locator_kind,
            locator_value=locator_value,
            raw_value=raw_evidence,
            retain_span=True,
        )
        if field_key in self.request.prohibited_fields:
            prohibited_evidence = evidence.model_copy(update={"evidence_span": None})
            identity = (field_key, locator_value, prohibited_evidence.evidence_digest)
            if identity not in self._prohibited_identities:
                self._prohibited_identities.add(identity)
                self.prohibited.append(
                    ProhibitedFieldEvidence(
                        field_key=field_key,
                        evidence=prohibited_evidence,
                    )
                )
            return
        if field_key not in self.request.allowed_fields:
            return
        if not isinstance(
            value,
            (
                ExtractedTextValue,
                ExtractedBooleanValue,
                ExtractedUrlValue,
                ExtractedEmailValue,
                ExtractedPhoneValue,
                ExtractedStringSetValue,
                ExtractedAddressValue,
                ExtractedBooleanValue,
                ExtractedMoneyValue,
            ),
        ):
            raise TypeError(f"unsupported extracted value for {field_key}")
        identity = (field_key, locator_value, evidence.evidence_digest)
        if identity in self._field_identities:
            return
        self._field_identities.add(identity)
        self.fields.append(ExtractedField(field_key=field_key, value=value, evidence=evidence))

    def add_text_block(self, element: HtmlElement, text: str, *, locator: str) -> None:
        normalized = _bounded_text(text, self.request.maximum_evidence_chars)
        if not normalized:
            return
        identity = (locator, _digest_text(normalized))
        if identity in self._text_identities:
            return
        self._text_identities.add(identity)
        evidence = _evidence(
            self.request,
            locator_kind=EvidenceLocatorKind.XPATH,
            locator_value=locator,
            raw_value=normalized,
            retain_span=True,
        )
        self.text_blocks.append(
            EvidenceTextBlock(
                text=normalized,
                locale=element.get("lang") or self.request.locale,
                evidence=evidence,
            )
        )


def extract_html_document(
    content: bytes,
    request: ExtractionRequest,
    *,
    metadata_extractor: EmbeddedMetadataExtractor | None = None,
) -> ExtractedRecord:
    if not content:
        raise ExtractionError(
            code="EXTRACTION_DOCUMENT_EMPTY",
            message="The raw source document is empty.",
        )
    if len(content) > request.maximum_document_bytes:
        raise ExtractionError(
            code="EXTRACTION_DOCUMENT_TOO_LARGE",
            message="The raw source document exceeds the extraction byte limit.",
        )
    observed_digest = f"sha256:{sha256(content).hexdigest()}"
    if observed_digest != request.raw_artifact_digest:
        raise ExtractionError(
            code="EXTRACTION_RAW_DIGEST_MISMATCH",
            message="The raw source document does not match the requested artifact digest.",
        )

    try:
        document = html.document_fromstring(content, base_url=request.source_url)
    except (etree.ParserError, ValueError) as exc:
        raise ExtractionError(
            code="EXTRACTION_HTML_INVALID",
            message="The raw source document is not parseable HTML.",
        ) from exc
    html_text = html.tostring(document, encoding="unicode", method="html")
    extractor = metadata_extractor or ExtructMetadataExtractor()
    try:
        metadata = extractor.extract(html_text, base_url=request.source_url)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(
            code="EXTRACTION_EMBEDDED_METADATA_FAILED",
            message="Embedded JSON-LD, microdata, or RDFa extraction failed.",
        ) from exc

    accumulator = _Accumulator.create(request)
    _extract_embedded_metadata(metadata, accumulator)
    _extract_html_contacts(document, accumulator)
    _extract_html_text(document, accumulator)

    payload = ExtractedRecordPayload(
        source_record_id=request.source_record_id,
        raw_artifact_digest=request.raw_artifact_digest,
        source_url=request.source_url,
        content_type=request.content_type,
        source_policy_digest=request.source_policy_digest,
        extractor_revision=request.extractor_revision,
        observed_at_utc=request.observed_at_utc,
        entity_kind_candidates=tuple(sorted(accumulator.entity_kinds)),
        source_categories=tuple(sorted(accumulator.source_categories)),
        fields=tuple(accumulator.fields),
        prohibited_fields=tuple(accumulator.prohibited),
        text_blocks=tuple(accumulator.text_blocks),
        issues=tuple(accumulator.issues),
    )
    return seal_extracted_record(payload)


def _extract_embedded_metadata(
    metadata: Mapping[str, object],
    accumulator: _Accumulator,
) -> None:
    for syntax, index, item in _iter_metadata_items(metadata):
        locator_prefix = f"/{syntax}/{index}"
        properties = _properties(item)
        schema_types = _schema_types(item)
        for schema_type in schema_types:
            accumulator.source_categories.add(schema_type)
            accumulator.entity_kinds.update(_SCHEMA_TYPE_TO_ENTITY_KINDS.get(schema_type, ()))

        _add_text_property(accumulator, properties, "name", "display_name", locator_prefix)
        _add_text_property(accumulator, properties, "legalName", "legal_name", locator_prefix)
        _add_url_property(accumulator, properties, "url", "website", locator_prefix)
        _add_email_property(accumulator, properties, "email", "email", locator_prefix)
        _add_phone_property(accumulator, properties, "telephone", "phone", locator_prefix)
        _add_string_set_property(
            accumulator,
            properties,
            "knowsLanguage",
            "supported_languages",
            locator_prefix,
        )
        _add_addresses(accumulator, properties.get("address"), f"{locator_prefix}/address")
        _add_external_links(accumulator, properties.get("sameAs"), f"{locator_prefix}/sameAs")
        _add_money_candidates(accumulator, properties, locator_prefix)

        for prohibited_name in accumulator.request.prohibited_fields:
            if prohibited_name in properties:
                accumulator.add_field(
                    prohibited_name,
                    ExtractedTextValue(value="redacted"),
                    locator_kind=EvidenceLocatorKind.JSON_POINTER,
                    locator_value=f"{locator_prefix}/{prohibited_name}",
                    raw_evidence=properties[prohibited_name],
                )


def _iter_metadata_items(
    metadata: Mapping[str, object],
) -> Iterable[tuple[str, int, Mapping[str, object]]]:
    position = 0
    for syntax in ("json-ld", "microdata", "rdfa"):
        raw_items = metadata.get(syntax, ())
        items: Sequence[object]
        if isinstance(raw_items, Mapping):
            items = (raw_items,)
        elif isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes, bytearray)):
            items = raw_items
        else:
            continue
        for raw_item in items:
            if not isinstance(raw_item, Mapping):
                continue
            graph = raw_item.get("@graph")
            if isinstance(graph, Sequence) and not isinstance(graph, (str, bytes, bytearray)):
                for graph_item in graph:
                    if isinstance(graph_item, Mapping):
                        yield syntax, position, cast(Mapping[str, object], graph_item)
                        position += 1
            else:
                yield syntax, position, cast(Mapping[str, object], raw_item)
                position += 1


def _properties(item: Mapping[str, object]) -> Mapping[str, object]:
    properties = item.get("properties")
    if isinstance(properties, Mapping):
        return cast(Mapping[str, object], properties)
    return item


def _schema_types(item: Mapping[str, object]) -> tuple[str, ...]:
    raw = item.get("@type", item.get("type", ()))
    values = _as_values(raw)
    result: list[str] = []
    for value in values:
        text = _scalar_text(value)
        if not text:
            continue
        schema_type = text.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        if schema_type not in result:
            result.append(schema_type)
    return tuple(result)


def _add_text_property(
    accumulator: _Accumulator,
    properties: Mapping[str, object],
    source_key: str,
    field_key: str,
    locator_prefix: str,
) -> None:
    for index, raw in enumerate(_as_values(properties.get(source_key))):
        value = _scalar_text(raw)
        if value:
            accumulator.add_field(
                field_key,
                ExtractedTextValue(value=_bounded_text(value, 2_000)),
                locator_kind=EvidenceLocatorKind.JSON_POINTER,
                locator_value=f"{locator_prefix}/{source_key}/{index}",
                raw_evidence=raw,
            )


def _add_url_property(
    accumulator: _Accumulator,
    properties: Mapping[str, object],
    source_key: str,
    field_key: str,
    locator_prefix: str,
) -> None:
    for index, raw in enumerate(_as_values(properties.get(source_key))):
        value = _scalar_text(raw)
        if value:
            accumulator.add_field(
                field_key,
                ExtractedUrlValue(value=value),
                locator_kind=EvidenceLocatorKind.JSON_POINTER,
                locator_value=f"{locator_prefix}/{source_key}/{index}",
                raw_evidence=raw,
            )


def _add_email_property(
    accumulator: _Accumulator,
    properties: Mapping[str, object],
    source_key: str,
    field_key: str,
    locator_prefix: str,
) -> None:
    for index, raw in enumerate(_as_values(properties.get(source_key))):
        value = _scalar_text(raw)
        if value:
            accumulator.add_field(
                field_key,
                ExtractedEmailValue(value=value.removeprefix("mailto:")),
                locator_kind=EvidenceLocatorKind.JSON_POINTER,
                locator_value=f"{locator_prefix}/{source_key}/{index}",
                raw_evidence=raw,
            )


def _add_phone_property(
    accumulator: _Accumulator,
    properties: Mapping[str, object],
    source_key: str,
    field_key: str,
    locator_prefix: str,
) -> None:
    for index, raw in enumerate(_as_values(properties.get(source_key))):
        value = _scalar_text(raw)
        if value:
            accumulator.add_field(
                field_key,
                ExtractedPhoneValue(value=value.removeprefix("tel:")),
                locator_kind=EvidenceLocatorKind.JSON_POINTER,
                locator_value=f"{locator_prefix}/{source_key}/{index}",
                raw_evidence=raw,
            )


def _add_string_set_property(
    accumulator: _Accumulator,
    properties: Mapping[str, object],
    source_key: str,
    field_key: str,
    locator_prefix: str,
) -> None:
    values = tuple(
        value for raw in _as_values(properties.get(source_key)) if (value := _scalar_text(raw))
    )
    if values:
        accumulator.add_field(
            field_key,
            ExtractedStringSetValue(values=tuple(dict.fromkeys(values))),
            locator_kind=EvidenceLocatorKind.JSON_POINTER,
            locator_value=f"{locator_prefix}/{source_key}",
            raw_evidence=properties[source_key],
        )


def _add_addresses(accumulator: _Accumulator, raw: object, locator_prefix: str) -> None:
    for index, value in enumerate(_as_values(raw)):
        if isinstance(value, Mapping):
            properties = _properties(cast(Mapping[str, object], value))
            address = ExtractedAddressValue(
                street_address=_first_text(properties.get("streetAddress")),
                postal_code=_first_text(properties.get("postalCode")),
                locality=_first_text(properties.get("addressLocality")),
                region=_first_text(properties.get("addressRegion")),
                country_code=_country_code(properties.get("addressCountry")),
                free_form=None,
            )
        else:
            text = _scalar_text(value)
            if not text:
                continue
            address = ExtractedAddressValue(free_form=_bounded_text(text, 500))
        accumulator.add_field(
            "address",
            address,
            locator_kind=EvidenceLocatorKind.JSON_POINTER,
            locator_value=f"{locator_prefix}/{index}",
            raw_evidence=value,
        )


def _add_external_links(accumulator: _Accumulator, raw: object, locator_prefix: str) -> None:
    for index, value in enumerate(_as_values(raw)):
        url = _scalar_text(value)
        if not url:
            continue
        host = (urlsplit(url).hostname or "").lower()
        field_key = "external_reference"
        if host in {"instagram.com", "www.instagram.com"}:
            field_key = "instagram"
        elif host in {"wa.me", "api.whatsapp.com", "www.whatsapp.com", "whatsapp.com"}:
            field_key = "whatsapp"
        accumulator.add_field(
            field_key,
            ExtractedUrlValue(value=url),
            locator_kind=EvidenceLocatorKind.JSON_POINTER,
            locator_value=f"{locator_prefix}/{index}",
            raw_evidence=value,
        )


def _add_money_candidates(
    accumulator: _Accumulator,
    properties: Mapping[str, object],
    locator_prefix: str,
) -> None:
    for source_key in ("offers", "priceSpecification"):
        for index, value in enumerate(_as_values(properties.get(source_key))):
            if not isinstance(value, Mapping):
                continue
            candidate = _money_candidate(cast(Mapping[str, object], value))
            if candidate is None:
                continue
            accumulator.add_field(
                "hourly_price",
                candidate,
                locator_kind=EvidenceLocatorKind.JSON_POINTER,
                locator_value=f"{locator_prefix}/{source_key}/{index}",
                raw_evidence=value,
            )


def _money_candidate(value: Mapping[str, object]) -> ExtractedMoneyValue | None:
    properties = _properties(value)
    nested = properties.get("priceSpecification")
    if isinstance(nested, Mapping):
        properties = _properties(cast(Mapping[str, object], nested))
    raw_amount = _first_text(properties.get("price"))
    raw_currency = _first_text(properties.get("priceCurrency"))
    basis = _first_text(properties.get("unitText")) or _first_text(properties.get("unitCode"))
    if raw_amount is None and raw_currency is None and basis is None:
        return None
    amount: Decimal | None = None
    if raw_amount is not None:
        try:
            amount = Decimal(raw_amount.replace(",", "."))
        except InvalidOperation:
            amount = None
    currency = raw_currency.upper() if raw_currency else None
    observed_text = _bounded_text(
        json.dumps(properties, ensure_ascii=False, sort_keys=True, default=str),
        300,
    )
    return ExtractedMoneyValue(
        amount=amount,
        currency=currency,
        basis=basis,
        observed_text=observed_text,
    )


def _extract_html_contacts(document: HtmlElement, accumulator: _Accumulator) -> None:
    root_tree = document.getroottree()
    for element in document.xpath("//link[@rel='canonical'][@href]"):
        if isinstance(element, HtmlElement):
            _add_link_element(element, "website", root_tree, accumulator)
    for element in document.xpath("//a[@href]"):
        if not isinstance(element, HtmlElement):
            continue
        href = element.get("href", "").strip()
        lower = href.lower()
        if lower.startswith("mailto:"):
            value = unquote(href[7:].split("?", 1)[0]).strip()
            if value:
                accumulator.add_field(
                    "email",
                    ExtractedEmailValue(value=value),
                    locator_kind=EvidenceLocatorKind.HTML_ATTRIBUTE,
                    locator_value=f"{root_tree.getpath(element)}/@href",
                    raw_evidence=href,
                )
        elif lower.startswith("tel:"):
            value = unquote(href[4:].split("?", 1)[0]).strip()
            if value:
                accumulator.add_field(
                    "phone",
                    ExtractedPhoneValue(value=value),
                    locator_kind=EvidenceLocatorKind.HTML_ATTRIBUTE,
                    locator_value=f"{root_tree.getpath(element)}/@href",
                    raw_evidence=href,
                )
        elif lower.startswith(("http://", "https://")):
            host = (urlsplit(href).hostname or "").lower()
            if host in {"instagram.com", "www.instagram.com"}:
                _add_link_element(element, "instagram", root_tree, accumulator)
            elif host in {"wa.me", "api.whatsapp.com", "www.whatsapp.com", "whatsapp.com"}:
                _add_link_element(element, "whatsapp", root_tree, accumulator)

    for element in document.xpath("//address"):
        if not isinstance(element, HtmlElement):
            continue
        value = _element_text(element)
        if value:
            accumulator.add_field(
                "address",
                ExtractedAddressValue(free_form=_bounded_text(value, 500)),
                locator_kind=EvidenceLocatorKind.XPATH,
                locator_value=root_tree.getpath(element),
                raw_evidence=value,
            )

    title_values = document.xpath("//title/text()")
    if title_values:
        value = _bounded_text(str(title_values[0]), 2_000)
        if value:
            accumulator.add_field(
                "display_name",
                ExtractedTextValue(value=value),
                locator_kind=EvidenceLocatorKind.XPATH,
                locator_value="/html/head/title",
                raw_evidence=value,
            )


def _add_link_element(
    element: HtmlElement,
    field_key: str,
    root_tree: etree._ElementTree,
    accumulator: _Accumulator,
) -> None:
    href = element.get("href", "").strip()
    if href:
        accumulator.add_field(
            field_key,
            ExtractedUrlValue(value=href),
            locator_kind=EvidenceLocatorKind.HTML_ATTRIBUTE,
            locator_value=f"{root_tree.getpath(element)}/@href",
            raw_evidence=href,
        )


def _extract_html_text(document: HtmlElement, accumulator: _Accumulator) -> None:
    root_tree = document.getroottree()
    for element in document.xpath(_TEXT_BLOCK_XPATH):
        if not isinstance(element, HtmlElement):
            continue
        accumulator.add_text_block(
            element,
            _element_text(element),
            locator=root_tree.getpath(element),
        )


def _element_text(element: HtmlElement) -> str:
    return _normalize_space(" ".join(element.itertext()))


def _evidence(
    request: ExtractionRequest,
    *,
    locator_kind: EvidenceLocatorKind,
    locator_value: str,
    raw_value: object,
    retain_span: bool,
) -> EvidenceReference:
    rendered = _render_evidence(raw_value)
    span = _bounded_text(rendered, request.maximum_evidence_chars) if retain_span else None
    return EvidenceReference(
        raw_artifact_digest=request.raw_artifact_digest,
        source_url=request.source_url,
        locator_kind=locator_kind,
        locator_value=locator_value,
        evidence_digest=_digest_text(rendered),
        evidence_span=span,
        observed_at_utc=request.observed_at_utc,
        extractor_revision=request.extractor_revision,
    )


def _render_evidence(value: object) -> str:
    if isinstance(value, str):
        return _normalize_space(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _as_values(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return (value,)


def _scalar_text(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key in ("@value", "value", "name"):
            if key in value:
                return _scalar_text(value[key])
        return None
    if isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool):
        text = _normalize_space(str(value))
        return text or None
    return None


def _first_text(value: object) -> str | None:
    for item in _as_values(value):
        if text := _scalar_text(item):
            return text
    return None


def _country_code(value: object) -> str | None:
    text = _first_text(value)
    if text is None:
        return None
    upper = text.upper()
    if re.fullmatch(r"[A-Z]{2}", upper):
        return upper
    return None


def _normalize_space(value: str) -> str:
    return " ".join(value.split())


def _bounded_text(value: str, maximum: int) -> str:
    normalized = _normalize_space(value)
    if len(normalized) <= maximum:
        return normalized
    return normalized[: maximum - 1].rstrip() + "…"


def _digest_text(value: str) -> str:
    return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"
