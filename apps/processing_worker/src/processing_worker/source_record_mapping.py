"""Deterministic source-record -> typed observation mapping.

Deliberately independent of HTML extraction and manual-import lineage: a source record
is proven by its own artifact identity plus the pinned mapping profile. Fields the
source does not carry are `NOT_OBSERVED`; `ABSENT_IN_SOURCE` is only emitted when the
mapping profile declares that the source is authoritative for that field and the
record explicitly omits it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid5

from collection_contracts.observations import (
    GeoPointObservationValue,
    ObservationIssue,
    ObservationState,
    ObservationValue,
    SourceRecordBatchPayload,
    SourceRecordEvidence,
    SourceRecordLocatorKind,
    SourceRecordObservationBatch,
    SourceRecordObservationPayload,
    TextObservationValue,
    seal_source_record_observation,
    seal_source_record_observation_batch,
)
from collection_contracts.osm_source_record import OsmSourceRecord, decode_osm_source_record
from normalization_core import NormalizationError, normalize_contact_value

_PROFILE_FIELDS = frozenset(
    {
        "campaignKey",
        "mappingRevision",
        "sourceBindingKey",
        "sourceKey",
        "sourcePolicyDigest",
    }
)
# A candidate field only accepts the value kind its target defines, so the mapper must
# emit that kind. Reading a raw tag and emitting text would produce an observation that
# can never materialize into a candidate.
# Candidate target -> the value kind the source field must be normalized into. Kept as
# plain target names so the worker needs no dependency on candidate domain types.
# A coordinate target must become typed geometry: text would leave geography unresolved
# even though the source published an exact point.
_CANDIDATE_VALUE_KINDS = {
    "website_urls": "url",
    "phones": "phone",
    "emails": "email",
    "coordinates": "geo_point",
}
_LOCATOR_KIND = SourceRecordLocatorKind.COLUMN
_MAX_FIELD_TEXT = 2_000
# A campaign classification is a keyed claim of the record, named with the contract's
# valid-key convention `category_<campaign category>`. The value repeats the category so
# a claim cannot be filed under one meaning and read as another.
_CATEGORY_FIELD_PREFIX = "category_"
_CAMPAIGN_CATEGORY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OSM_RECORD_ARTIFACT_NAMESPACE = UUID("6f4d2b1e-8a3c-5b7d-9e2f-1c3a5b7d9e2f")


class SourceRecordMappingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def materialize_source_record_observations(
    record_content: bytes,
    profile_content: bytes,
    *,
    maximum_records: int,
) -> SourceRecordObservationBatch:
    """Map one source record artifact into a sealed source-native observation batch.

    The observation time is taken from the record itself, never from a wall clock and
    never from an unchecked caller argument: identical evidence must seal to an identical
    batch, and the time must be attributable to the acquisition that produced it.
    """

    profile = _decode_profile(profile_content)
    document = _decode_record(record_content, maximum_records=maximum_records)
    if document.get("contract") == _OSM_SOURCE_RECORD_CONTRACT:
        document = _osm_record_as_generic_record(
            record_content,
            document,
            profile=profile,
        )
    source_record_id = _required_text(document, "sourceRecordId", "source_record_id")
    dataset_release = _required_text(document, "datasetRelease", "dataset_release")
    records = document.get("records")
    if not isinstance(records, list) or not records:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_ENTRIES_MISSING",
            "The source record artifact contains no records.",
        )
    if len(records) > maximum_records:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_ENTRIES_EXCEEDED",
            "The source record artifact exceeds the bounded record limit.",
        )
    if len(records) != 1:
        # One artifact is one source record: a batch never mixes element identities.
        raise SourceRecordMappingError(
            "SOURCE_RECORD_ENTRY_COUNT_INVALID",
            "A source record artifact must contain exactly one record.",
        )
    fields = document.get("fields")
    if not isinstance(fields, list) or not fields:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_FIELDS_MISSING",
            "The source record artifact declares no mappable fields.",
        )

    source_url = _required_text(document, "sourceUrl", "source_url")
    observed_at = _acquisition_observed_at(document)
    record_artifact_id = UUID(_required_text(document, "artifactRecordId", "artifact_record_id"))
    declared_digest = _required_text(document, "artifactDigest", "artifact_digest")
    if document.get("contract") == _OSM_SOURCE_RECORD_CONTRACT:
        # The OSM adapter already verified the source record against its own sealed
        # digest; re-deriving one from the adapted shape would only prove the adapter.
        record_digest = declared_digest
    else:
        # The artifact digest covers the document without its own digest field, so the
        # producer's manifest stays verifiable and cannot be satisfied by editing it.
        unsealed = {key: value for key, value in document.items() if key != "artifactDigest"}
        record_digest = f"sha256:{sha256(_canonical(unsealed)).hexdigest()}"
        if declared_digest != record_digest:
            raise SourceRecordMappingError(
                "SOURCE_RECORD_ARTIFACT_TAMPERED",
                "The source record artifact differs from its declared digest.",
            )

    observations: list[SourceRecordObservationPayload] = []
    issues: list[ObservationIssue] = []
    for index, entry in enumerate(records):
        if not isinstance(entry, dict):
            issues.append(
                ObservationIssue(
                    code="SOURCE_RECORD_ENTRY_INVALID",
                    message=f"Record entry {index} is not an object and was rejected.",
                )
            )
            continue
        record_scoped = str(entry.get("sourceRecordId", source_record_id))
        if record_scoped != source_record_id:
            raise SourceRecordMappingError(
                "SOURCE_RECORD_IDENTITY_CONFLICT",
                "The record entry identity differs from the artifact source record.",
            )
        for field_key in fields:
            if not isinstance(field_key, str):
                raise SourceRecordMappingError(
                    "SOURCE_RECORD_FIELD_INVALID",
                    "The mapping profile declares a non-string field key.",
                )
            source_evidence = entry.get(field_key)
            for candidate in _distinct_candidates(source_evidence):
                observations.append(
                    _observation(
                        record_scoped=record_scoped,
                        field_key=field_key,
                        source_evidence=candidate,
                        source_url=source_url,
                        record_artifact_id=record_artifact_id,
                        record_digest=record_digest,
                        policy_digest=str(profile["sourcePolicyDigest"]),
                        mapping_revision=str(profile["mappingRevision"]),
                        observed_at=observed_at,
                        value_kind=_contact_value_kind(profile, field_key),
                    )
                )

    if not observations:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_OBSERVATIONS_EMPTY",
            "The mapping produced no observations for the source record.",
        )
    return seal_source_record_observation_batch(
        SourceRecordBatchPayload(
            run_id=UUID(_required_text(document, "runId", "run_id")),
            source_key=str(profile["sourceKey"]),
            source_binding_key=str(profile["sourceBindingKey"]),
            source_record_id=source_record_id,
            source_record_artifact_id=record_artifact_id,
            source_record_artifact_digest=record_digest,
            dataset_release=dataset_release,
            source_policy_digest=str(profile["sourcePolicyDigest"]),
            mapping_revision=str(profile["mappingRevision"]),
            observation_profile_digest=f"sha256:{sha256(profile_content).hexdigest()}",
            observations=tuple(seal_source_record_observation(item) for item in observations),
            issues=tuple(issues),
        )
    )


def _distinct_candidates(source_evidence: object) -> tuple[object, ...]:
    """One evidence entry, or one per distinct normalized value from source-tag aliases.

    Identical normalized values are collapsed so two tags carrying the same value never
    become two observations. Distinct values are all returned, so a conflicting pair stays
    visible for review instead of one silently overwriting the other.
    """

    if not isinstance(source_evidence, list):
        return (source_evidence,)
    if not source_evidence:
        return (None,)
    distinct: list[object] = []
    seen: set[str] = set()
    for item in source_evidence:
        value = item.get("value") if isinstance(item, dict) else None
        marker = " ".join(value.split()) if isinstance(value, str) else ""
        # An unusable candidate keeps a unique marker so it still surfaces as invalid.
        key = marker or f"<unusable:{len(distinct)}>"
        if key in seen:
            continue
        seen.add(key)
        distinct.append(item)
    return tuple(distinct)


def _observation(
    *,
    record_scoped: str,
    field_key: str,
    source_evidence: object,
    source_url: str,
    record_artifact_id: UUID,
    record_digest: str,
    policy_digest: str,
    mapping_revision: str,
    observed_at: datetime,
    value_kind: str | None = None,
) -> SourceRecordObservationPayload:
    if source_evidence is None:
        # The source simply carried no value: unknown, never a confirmed absence.
        return SourceRecordObservationPayload(
            source_record_id=record_scoped,
            field_key=field_key,
            state=ObservationState.NOT_OBSERVED,
            source_policy_digest=policy_digest,
            mapping_revision=mapping_revision,
            confidence=Decimal("0"),
        )
    if not isinstance(source_evidence, dict):
        return _invalid(
            record_scoped=record_scoped,
            source_url=source_url,
            field_key=field_key,
            code="SOURCE_RECORD_FIELD_SHAPE_INVALID",
            excerpt=str(source_evidence),
            record_artifact_id=record_artifact_id,
            record_digest=record_digest,
            policy_digest=policy_digest,
            mapping_revision=mapping_revision,
            observed_at=observed_at,
        )
    value = source_evidence.get("value")
    locator = source_evidence.get("pointer")
    if value is None or not isinstance(locator, str) or not locator:
        return _invalid(
            record_scoped=record_scoped,
            source_url=source_url,
            field_key=field_key,
            code="SOURCE_RECORD_FIELD_EVIDENCE_INVALID",
            excerpt=json.dumps(source_evidence, ensure_ascii=False)[:120],
            record_artifact_id=record_artifact_id,
            record_digest=record_digest,
            policy_digest=policy_digest,
            mapping_revision=mapping_revision,
            observed_at=observed_at,
        )
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_FIELD_TEXT:
        return _invalid(
            record_scoped=record_scoped,
            source_url=source_url,
            field_key=field_key,
            code="SOURCE_RECORD_FIELD_VALUE_INVALID",
            excerpt=str(value)[:120],
            record_artifact_id=record_artifact_id,
            record_digest=record_digest,
            policy_digest=policy_digest,
            mapping_revision=mapping_revision,
            observed_at=observed_at,
        )
    kind_supported, declared_kind = _declared_value_kind(source_evidence)
    if not kind_supported:
        return _invalid(
            record_scoped=record_scoped,
            source_url=source_url,
            field_key=field_key,
            code="SOURCE_RECORD_VALUE_KIND_UNSUPPORTED",
            excerpt=str(source_evidence.get("valueKind"))[:120],
            record_artifact_id=record_artifact_id,
            record_digest=record_digest,
            policy_digest=policy_digest,
            mapping_revision=mapping_revision,
            observed_at=observed_at,
        )
    # A category claim is not language text: it names the campaign category the source
    # classification was accepted as, and the key itself carries that category verbatim.
    # A value that disagrees with its own field key is malformed evidence, never a
    # second candidate category.
    if field_key.startswith(_CATEGORY_FIELD_PREFIX):
        campaign_category = field_key[len(_CATEGORY_FIELD_PREFIX) :]
        if (
            value_kind is not None
            or declared_kind is not None
            or _CAMPAIGN_CATEGORY.fullmatch(campaign_category) is None
            or " ".join(value.split()) != campaign_category
        ):
            return _invalid(
                record_scoped=record_scoped,
                source_url=source_url,
                field_key=field_key,
                code="SOURCE_RECORD_CATEGORY_INVALID",
                excerpt=value[:120],
                record_artifact_id=record_artifact_id,
                record_digest=record_digest,
                policy_digest=policy_digest,
                mapping_revision=mapping_revision,
                observed_at=observed_at,
            )
        return SourceRecordObservationPayload(
            source_record_id=record_scoped,
            field_key=field_key,
            state=ObservationState.OBSERVED,
            value=TextObservationValue(original=value, normalized=campaign_category),
            evidence=SourceRecordEvidence(
                raw_artifact_id=record_artifact_id,
                raw_artifact_digest=record_digest,
                source_url=source_url,
                locator_kind=_LOCATOR_KIND,
                locator_value=locator,
                evidence_digest=f"sha256:{sha256(value.encode('utf-8')).hexdigest()}",
                evidence_span=value[:_MAX_FIELD_TEXT],
                observed_at_utc=observed_at,
                mapping_revision=mapping_revision,
            ),
            source_policy_digest=policy_digest,
            mapping_revision=mapping_revision,
            confidence=_confidence(source_evidence.get("confidence")),
        )
    # An evidence entry may describe its own value kind (a source-native point carries
    # one), and a declared projection wins over it. Neither may be guessed.
    if value_kind is None:
        value_kind = declared_kind
    typed: ObservationValue
    if value_kind == "geo_point":
        try:
            typed = _geo_point_value(
                value,
                source_evidence=source_evidence,
                record_scoped=record_scoped,
            )
        except _GeoPointValueError:
            # An unattributable or malformed point is invalid evidence, never a guessed
            # location; the candidate then stays geographically unresolved.
            return _invalid(
                record_scoped=record_scoped,
                source_url=source_url,
                field_key=field_key,
                code="SOURCE_RECORD_GEOMETRY_INVALID",
                excerpt=value[:120],
                record_artifact_id=record_artifact_id,
                record_digest=record_digest,
                policy_digest=policy_digest,
                mapping_revision=mapping_revision,
                observed_at=observed_at,
            )
    elif value_kind is None:
        # A name is only usable with the language it was published in. Evidence that
        # states no language stays unresolved, and an unusable tag is invalid evidence -
        # never a guessed locale.
        locale_supported, resolved_locale = _evidence_locale(source_evidence)
        if not locale_supported:
            return _invalid(
                record_scoped=record_scoped,
                source_url=source_url,
                field_key=field_key,
                code="SOURCE_RECORD_LOCALE_INVALID",
                excerpt=str(source_evidence.get("locale"))[:120],
                record_artifact_id=record_artifact_id,
                record_digest=record_digest,
                policy_digest=policy_digest,
                mapping_revision=mapping_revision,
                observed_at=observed_at,
            )
        typed = TextObservationValue(
            original=value,
            normalized=" ".join(value.split()),
            locale=resolved_locale,
        )
    else:
        try:
            typed = normalize_contact_value(value, value_kind)
        except NormalizationError:
            # An unparseable contact value is reported as invalid, never coerced.
            return _invalid(
                record_scoped=record_scoped,
                source_url=source_url,
                field_key=field_key,
                code="SOURCE_RECORD_FIELD_VALUE_INVALID",
                excerpt=value[:120],
                record_artifact_id=record_artifact_id,
                record_digest=record_digest,
                policy_digest=policy_digest,
                mapping_revision=mapping_revision,
                observed_at=observed_at,
            )
    return SourceRecordObservationPayload(
        source_record_id=record_scoped,
        field_key=field_key,
        state=ObservationState.OBSERVED,
        value=typed,
        evidence=SourceRecordEvidence(
            raw_artifact_id=record_artifact_id,
            raw_artifact_digest=record_digest,
            source_url=source_url,
            locator_kind=_LOCATOR_KIND,
            locator_value=locator,
            evidence_digest=f"sha256:{sha256(value.encode('utf-8')).hexdigest()}",
            evidence_span=value[:_MAX_FIELD_TEXT],
            observed_at_utc=observed_at,
            mapping_revision=mapping_revision,
        ),
        source_policy_digest=policy_digest,
        mapping_revision=mapping_revision,
        confidence=_confidence(source_evidence.get("confidence")),
    )


# A source-native value kind the record may declare for one of its fields. `geo_point` is
# source geometry; `url` is a value the source property types as a link, so it is
# normalized as a website rather than as text that could never become one.
_SUPPORTED_EVIDENCE_VALUE_KINDS = frozenset({"geo_point", "url"})


def _declared_value_kind(
    source_evidence: Mapping[str, object],
) -> tuple[bool, str | None]:
    """The value kind the evidence entry declares, and whether it is supported."""

    declared = source_evidence.get("valueKind")
    if declared is None:
        return (True, None)
    if not isinstance(declared, str) or declared not in _SUPPORTED_EVIDENCE_VALUE_KINDS:
        return (False, None)
    return (True, declared)


_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def _evidence_locale(source_evidence: Mapping[str, object]) -> tuple[bool, str | None]:
    """The language the source published the value in, when it declared one.

    `None` means the source established no language, so the value keeps no display
    locale. A malformed tag returns the invalid marker instead of a silently dropped
    tag, because dropping it would turn a wrong locale into an unresolved one.
    """

    locale = source_evidence.get("locale")
    if locale is None:
        return (True, None)
    if not isinstance(locale, str) or not _LANGUAGE_TAG.fullmatch(locale):
        return (False, None)
    return (True, locale)


class _GeoPointValueError(ValueError):
    """A coordinate that cannot be emitted as observed geometry."""


def _geo_point_value(
    value: str,
    *,
    source_evidence: Mapping[str, object],
    record_scoped: str,
) -> GeoPointObservationValue:
    """Build the typed point, refusing any coordinate the source did not publish.

    The value text is the exact point object the adapter wrote. A malformed, out-of-range
    or un-attributed point is an error: it is never clamped, rounded or replaced, because
    a shifted coordinate silently moves a studio into the wrong market area.
    """

    kind = source_evidence.get("valueKind")
    if kind != "geo_point":
        raise _GeoPointValueError("geometry evidence is not a point")
    geometry_source = source_evidence.get("geometrySourceRecordId")
    if not isinstance(geometry_source, str) or not geometry_source:
        raise _GeoPointValueError("geometry evidence carries no source element")
    if geometry_source != record_scoped:
        raise _GeoPointValueError("geometry belongs to another source element")
    locator = source_evidence.get("pointer")
    if not isinstance(locator, str) or not locator:
        raise _GeoPointValueError("geometry evidence carries no locator")
    try:
        document = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise _GeoPointValueError("geometry is not valid point JSON") from exc
    if not isinstance(document, Mapping) or set(document) != {"latitude", "longitude"}:
        raise _GeoPointValueError("geometry is not an exact point object")
    try:
        return GeoPointObservationValue(
            latitude=Decimal(str(document["latitude"])),
            longitude=Decimal(str(document["longitude"])),
            geometry_source_record_id=geometry_source,
            geometry_locator=locator,
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise _GeoPointValueError("geometry coordinates are invalid") from exc


def _invalid(
    *,
    record_scoped: str,
    source_url: str,
    field_key: str,
    code: str,
    excerpt: str,
    record_artifact_id: UUID,
    record_digest: str,
    policy_digest: str,
    mapping_revision: str,
    observed_at: datetime,
) -> SourceRecordObservationPayload:
    from collection_contracts.observations import InvalidObservationValue

    return SourceRecordObservationPayload(
        source_record_id=record_scoped,
        field_key=field_key,
        state=ObservationState.INVALID,
        value=InvalidObservationValue(reason_code=code, original_excerpt=(excerpt or "?")[:120]),
        evidence=SourceRecordEvidence(
            raw_artifact_id=record_artifact_id,
            raw_artifact_digest=record_digest,
            source_url=source_url,
            locator_kind=_LOCATOR_KIND,
            locator_value=f"/records/{field_key}",
            evidence_digest=f"sha256:{sha256(excerpt.encode('utf-8', 'replace')).hexdigest()}",
            evidence_span=None,
            observed_at_utc=observed_at,
            mapping_revision=mapping_revision,
        ),
        source_policy_digest=policy_digest,
        mapping_revision=mapping_revision,
        confidence=Decimal("0"),
    )


def _confidence(value: object) -> Decimal:
    if value is None:
        return Decimal("0.5")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_CONFIDENCE_INVALID",
            "The source record carries a non-numeric confidence.",
        ) from exc
    if not Decimal("0") <= parsed <= Decimal("1"):
        raise SourceRecordMappingError(
            "SOURCE_RECORD_CONFIDENCE_INVALID",
            "The source record confidence is outside [0, 1].",
        )
    return parsed.quantize(Decimal("0.0001"))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_profile(content: bytes) -> dict[str, Any]:
    try:
        profile = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceRecordMappingError(
            "SOURCE_MAPPING_PROFILE_INVALID",
            "The source mapping profile is not valid JSON.",
        ) from exc
    # `fieldAliases` is additive: a profile without it keeps its exact original shape.
    allowed_shapes = {
        _PROFILE_FIELDS,
        _OSM_PROFILE_FIELDS,
        _PROFILE_FIELDS | {"fieldAliases"},
        _OSM_PROFILE_FIELDS | {"fieldAliases"},
    }
    if not isinstance(profile, dict) or set(profile) not in allowed_shapes:
        raise SourceRecordMappingError(
            "SOURCE_MAPPING_PROFILE_INVALID",
            "The source mapping profile has an unexpected shape.",
        )
    for name in ("mappingRevision", "sourceBindingKey", "sourceKey", "sourcePolicyDigest"):
        if not isinstance(profile[name], str) or not profile[name]:
            raise SourceRecordMappingError(
                "SOURCE_MAPPING_PROFILE_INVALID",
                f"The source mapping profile field {name} is invalid.",
            )
    return profile


_OSM_SOURCE_RECORD_CONTRACT = "osm-source-record@1"
_OSM_PROFILE_FIELDS = frozenset(
    {
        "campaignKey",
        "entityKind",
        "fieldProjection",
        "mappingRevision",
        "runId",
        "sourceBindingKey",
        "sourceKey",
        "sourcePolicyDigest",
    }
)


def _osm_record_as_generic_record(
    record_content: bytes,
    document: dict[str, Any],
    *,
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Adapt a per-element OSM record onto the generic source-record shape.

    Only fields the element actually publishes become values. A field declared in the
    campaign projection but absent from the element stays absent so the mapper emits
    `NOT_OBSERVED` rather than an invented empty value.
    """
    try:
        record = decode_osm_source_record(record_content)
    except (TypeError, ValueError) as exc:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_ARTIFACT_TAMPERED",
            "The OSM source record differs from its declared digest.",
        ) from exc
    projection = profile.get("fieldProjection")
    if not isinstance(projection, Mapping) or not projection:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_PROJECTION_MISSING",
            "The mapping profile declares no field projection.",
        )
    run_id = profile.get("runId")
    if not isinstance(run_id, str) or not run_id:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_RUN_MISSING",
            "The mapping profile does not pin the run identity.",
        )
    aliases = profile.get("fieldAliases")
    if aliases is not None and not isinstance(aliases, Mapping):
        raise SourceRecordMappingError(
            "SOURCE_RECORD_PROJECTION_INVALID",
            "The mapping profile declares an invalid field alias map.",
        )
    address = record.address
    # Field key -> the source tags that may carry it, primary tag first. A field whose
    # name differs from its tag (display_name reads `name`) declares the mapping here.
    field_tags: dict[str, tuple[str, ...]] = {
        "display_name": ("name",),
        "name": ("name",),
        "website": ("website",),
        "phone": ("phone",),
        "email": ("email",),
    }
    values: dict[str, str] = {
        "display_name": record.tags.get("name", ""),
        "name": record.tags.get("name", ""),
        "website": record.tags.get("website", ""),
        "phone": record.tags.get("phone", ""),
        "email": record.tags.get("email", ""),
        "address": ", ".join(
            part
            for part in (
                address.get("street"),
                address.get("houseNumber"),
                address.get("postcode"),
                address.get("city"),
            )
            if part
        ),
    }
    # The element's own point is source geometry, not a tag. It is emitted only when the
    # campaign projects the coordinate field, and it always carries the element identity
    # and the geometry pointer so the value stays attributable to exactly one element.
    geometry_entry = _osm_geometry_entry(record, projection=projection)
    fields = sorted(str(key) for key in projection)
    # Every authorized source tag is read, so a value published only under an alias
    # (for example `contact:website`) is still observed. Each candidate keeps its own
    # tag provenance; nothing is merged here.
    entries: dict[str, list[dict[str, str]]] = {}
    for key in values:
        if key not in projection:
            continue
        # Fields without declared aliases read only their own primary source tag.
        aliased = (aliases or {}).get(key)
        if isinstance(aliased, list | tuple) and aliased:
            declared = tuple(str(item) for item in aliased)
        else:
            declared = field_tags.get(key, (key,))
        candidates: list[dict[str, str]] = []
        for tag in declared:
            value = record.tags.get(tag)
            if isinstance(value, str) and value.strip():
                candidates.append({"value": value, "pointer": f"/tags/{tag}"})
        if candidates:
            entries[key] = candidates
    if geometry_entry is not None:
        entries[_COORDINATE_FIELD_KEY] = [geometry_entry]
    # The acquisition time travels with the adapted record so the mapper never has to
    # guess it. A connector that did not publish one keeps it absent, and the mapper then
    # refuses the record instead of stamping it with the current clock.
    adapted: dict[str, Any] = {}
    if record.osm_base_timestamp_utc is not None:
        adapted["osmBaseTimestampUtc"] = record.osm_base_timestamp_utc
    if record.element_timestamp_utc is not None:
        adapted["elementTimestampUtc"] = record.element_timestamp_utc
    return {
        "contract": _OSM_SOURCE_RECORD_CONTRACT,
        "sourceRecordId": record.source_record_id,
        **adapted,
        "datasetRelease": record.raw_response_digest,
        "artifactRecordId": str(uuid5(_OSM_RECORD_ARTIFACT_NAMESPACE, record.record_digest)),
        "artifactDigest": record.record_digest,
        "runId": run_id,
        "sourceUrl": record.element.source_url,
        "fields": fields,
        "records": [{"sourceRecordId": record.source_record_id, **entries}],
    }


# The campaign field key that carries a source-native point. Kept identical to the
# geography owner's key so one observation finds one meaning.
_COORDINATE_FIELD_KEY = "coordinates"


def _osm_geometry_entry(
    record: OsmSourceRecord, *, projection: Mapping[str, object]
) -> dict[str, str] | None:
    """The element's own point, or nothing when the campaign does not project it.

    Only a point the element publishes is used. A way or relation centre is a computed
    value, so it is never substituted: without an observed point the candidate stays
    unresolved instead of being given a fabricated location.
    """

    if _COORDINATE_FIELD_KEY not in projection:
        return None
    return {
        "value": json.dumps(
            {"latitude": str(record.latitude), "longitude": str(record.longitude)},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "pointer": "/geometry",
        "valueKind": "geo_point",
        "geometrySourceRecordId": record.source_record_id,
    }


_OBSERVED_AT_FIELDS = ("osmBaseTimestampUtc", "observedAtUtc", "acquiredAtUtc")


def _acquisition_observed_at(document: dict[str, Any]) -> datetime:
    """The immutable acquisition time of the record, or a hard failure.

    Scanned in a fixed order. A record carrying no acquisition time is rejected instead
    of being stamped with the current clock, because a made-up time silently destroys
    replay identity and makes the evidence unattributable. Two fields that disagree mean
    the provenance is contradictory, so the record is rejected rather than one winning.
    """

    declared: list[tuple[str, datetime]] = []
    for name in _OBSERVED_AT_FIELDS:
        raw = document.get(name)
        if raw is None:
            continue
        if not isinstance(raw, str) or not raw:
            raise SourceRecordMappingError(
                "SOURCE_RECORD_TIMESTAMP_INVALID",
                f"The source record acquisition time {name} is not a timestamp string.",
            )
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SourceRecordMappingError(
                "SOURCE_RECORD_TIMESTAMP_INVALID",
                f"The source record acquisition time {name} is not a valid timestamp.",
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise SourceRecordMappingError(
                "SOURCE_RECORD_TIMESTAMP_INVALID",
                f"The source record acquisition time {name} is not UTC.",
            )
        declared.append((name, parsed))
    if not declared:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_TIMESTAMP_MISSING",
            "The source record carries no acquisition time, so it cannot be observed.",
        )
    first_name, observed_at = declared[0]
    for name, candidate in declared[1:]:
        if candidate != observed_at:
            raise SourceRecordMappingError(
                "SOURCE_RECORD_TIMESTAMP_CONFLICT",
                "The source record declares conflicting acquisition times "
                f"({first_name} and {name}).",
            )
    return observed_at


def _element_version_utc(document: dict[str, Any]) -> str | None:
    """The OSM element's own modification time, kept as provenance, not as observed time.

    An element edit time describes the object in OSM, not when we acquired it. It is only
    reported when the source record actually carries it, so nothing is invented.
    """

    raw = document.get("elementTimestampUtc")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_TIMESTAMP_INVALID",
            "The OSM element modification time is not a timestamp string.",
        )
    return raw


def _decode_record(content: bytes, *, maximum_records: int) -> dict[str, Any]:
    if maximum_records <= 0:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_LIMIT_INVALID",
            "The source record limit must be positive.",
        )
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_ARTIFACT_INVALID",
            "The source record artifact is not valid JSON.",
        ) from exc
    if not isinstance(document, dict):
        raise SourceRecordMappingError(
            "SOURCE_RECORD_ARTIFACT_INVALID",
            "The source record artifact must be a JSON object.",
        )
    return document


_VALUE_KINDS = frozenset({"geo_point"})


def _contact_value_kind(profile: Mapping[str, Any], field_key: str) -> str | None:
    """The value kind a declared target requires, or None when plain text is correct."""

    projection = profile.get("fieldProjection")
    if not isinstance(projection, Mapping):
        return None
    return _CANDIDATE_VALUE_KINDS.get(str(projection.get(field_key, "")))


def _required_text(document: dict[str, Any], key: str, name: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise SourceRecordMappingError(
            "SOURCE_RECORD_ARTIFACT_INVALID",
            f"The source record artifact field {name} is missing or invalid.",
        )
    return value
