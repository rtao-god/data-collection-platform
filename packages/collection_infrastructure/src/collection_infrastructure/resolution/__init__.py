"""Deterministic entity-resolution batch assembly from persisted candidate revisions.

Owned by the infrastructure layer because the batch is a projection of persisted state into
the resolution owner's contract; the application layer above it never names that contract.

The batch is the only input the resolution worker accepts, so its content must be a pure
function of already-persisted candidate revisions plus the campaign's authored quality
policy. That is what makes a batch replay-safe: the same persisted revisions always produce
the same batch digest, and the snapshot applier can re-derive the batch and refuse anything
that does not match byte-for-byte.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, UUID, uuid5

from collection_contracts import QualityProfileDocument, QualityRule, quality_profile_digest
from resolution_contracts import (
    CandidateField,
    ClusterQualityRule,
    EntityKind,
    GeographyCoverage,
    GeographyReference,
    MarketAreaReadiness,
    PriorCluster,
    ResolutionBatch,
    ResolutionBatchPayload,
    ResolutionCandidate,
    ResolutionThresholds,
    seal_resolution_batch,
)

# Stage 6 records observation identity as a canonical digest, while the resolution contract
# addresses observations by UUID. The projection below is a pure function of the digest, so
# it neither invents an observation nor changes across replays.
_OBSERVATION_ID_NAMESPACE = UUID("0d7f5b23-4f81-5c6a-9a2e-6b13c0d84f97")
_BATCH_ID_NAMESPACE = uuid5(NAMESPACE_URL, "collector-entity-resolution-batch")
_NAME_FIELD_KEYS = ("display_name", "names")
_WEBSITE_FIELD_KEYS = ("website", "website_urls")
_ADDRESS_FIELD_KEYS = ("address", "addresses")
_COORDINATE_FIELD_KEY = "coordinates"


class ResolutionAssemblyError(ValueError):
    """A persisted revision cannot be expressed under the resolution contract."""

    def __init__(self, code: str, message: str, **context: object) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


@dataclass(frozen=True, slots=True)
class ResolutionCandidateSource:
    """One persisted candidate revision, in the form the batch needs."""

    candidate_id: UUID
    revision: int
    entity_kind: str
    normalized_payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("candidate revision must not be negative")


@dataclass(frozen=True, slots=True)
class ResolutionMarketArea:
    revision: str
    digest: str


def assemble_resolution_batch(
    *,
    run_id: UUID,
    market_area: ResolutionMarketArea,
    quality_profile: QualityProfileDocument,
    revisions: Sequence[ResolutionCandidateSource],
    prior_clusters: Sequence[PriorCluster] = (),
) -> ResolutionBatch:
    """Build the single canonical batch for one run and its persisted revisions."""
    if not revisions:
        raise ResolutionAssemblyError(
            "RESOLUTION_BATCH_EMPTY",
            "A resolution batch requires at least one persisted candidate revision.",
            runId=str(run_id),
        )
    ordered = tuple(sorted(revisions, key=lambda item: (item.candidate_id.hex, item.revision)))
    coordinates = tuple((item.candidate_id, item.revision) for item in ordered)
    if len({item.candidate_id for item in ordered}) != len(ordered):
        raise ResolutionAssemblyError(
            "RESOLUTION_BATCH_DUPLICATE_CANDIDATE",
            "A batch addresses each candidate exactly once.",
            runId=str(run_id),
        )
    rules = {rule.entity_kind for rule in quality_profile.quality_rules}
    missing = sorted({item.entity_kind for item in ordered}.difference(rules))
    if missing:
        raise ResolutionAssemblyError(
            "RESOLUTION_QUALITY_RULE_MISSING",
            "The campaign quality profile does not cover every candidate entity kind.",
            entityKinds=missing,
        )
    candidates = tuple(_candidate(item, market_area) for item in ordered)
    present_kinds = sorted({item.entity_kind for item in ordered})
    profile_rules = tuple(
        _quality_rule(rule)
        for rule in sorted(quality_profile.quality_rules, key=lambda item: item.entity_kind)
        if rule.entity_kind in present_kinds
    )
    thresholds = quality_profile.thresholds
    payload = ResolutionBatchPayload(
        batch_id=uuid5(
            _BATCH_ID_NAMESPACE,
            "|".join(
                (
                    str(run_id),
                    quality_profile_digest(quality_profile),
                    market_area.revision,
                    market_area.digest,
                    *(f"{item.candidate_id}:{item.revision}" for item in ordered),
                )
            ),
        ),
        market_area_revision=market_area.revision,
        market_area_digest=market_area.digest,
        market_area_readiness=MarketAreaReadiness.READY,
        candidates=candidates,
        prior_clusters=tuple(prior_clusters),
        thresholds=ResolutionThresholds(
            candidate_limit=thresholds.candidate_limit,
            pair_limit=thresholds.pair_limit,
            name_review_minimum_bps=thresholds.name_review_minimum_bps,
            address_review_minimum_bps=thresholds.address_review_minimum_bps,
            website_auto_match_minimum_strong_features=(
                thresholds.website_auto_match_minimum_strong_features
            ),
            address_auto_match_minimum_strong_features=(
                thresholds.address_auto_match_minimum_strong_features
            ),
            fuzzy_primary_area_review_reason_code=(
                thresholds.fuzzy_primary_area_review_reason_code
            ),
        ),
        quality_rules=profile_rules,
    )
    batch = seal_resolution_batch(payload)
    if len(coordinates) > thresholds.candidate_limit:
        raise ResolutionAssemblyError(
            "RESOLUTION_BATCH_LIMIT_EXCEEDED",
            "Persisted revisions exceed the campaign's declared candidate limit.",
            candidateCount=len(coordinates),
            candidateLimit=thresholds.candidate_limit,
        )
    return batch


def _quality_rule(rule: QualityRule) -> ClusterQualityRule:
    # The authored document speaks in plain keys; the resolution contract speaks in enums.
    # The conversion happens once, here, so no consumer has to re-interpret a raw key.
    return ClusterQualityRule(
        entity_kind=EntityKind(rule.entity_kind),
        required_fields=tuple(sorted(CandidateField(field) for field in rule.required_fields)),
        single_value_fields=tuple(
            sorted(CandidateField(field) for field in rule.single_value_fields)
        ),
        minimum_distinct_source_count=rule.minimum_distinct_source_count,
        allowed_geography=tuple(
            sorted(GeographyCoverage(value) for value in rule.allowed_geography)
        ),
        boundary_requires_review=rule.boundary_requires_review,
        pending_review_blocks_export=rule.pending_review_blocks_export,
    )


def _candidate(
    source: ResolutionCandidateSource,
    market_area: ResolutionMarketArea,
) -> ResolutionCandidate:
    payload = source.normalized_payload
    observations = _observations(source)
    return ResolutionCandidate(
        candidate_id=source.candidate_id,
        entity_kind=EntityKind(source.entity_kind),
        names=_values(observations, _NAME_FIELD_KEYS, normalized=True),
        phones=(),
        emails=(),
        website_urls=_values(observations, _WEBSITE_FIELD_KEYS),
        addresses=_values(observations, _ADDRESS_FIELD_KEYS, normalized=True),
        observation_ids=_observation_ids(observations),
        source_artifact_ids=_source_artifact_ids(observations),
        source_keys=_source_keys(payload),
        geography=_geography(payload, observations, market_area, source),
    )


def _observations(
    source: ResolutionCandidateSource,
) -> tuple[Mapping[str, object], ...]:
    raw = source.normalized_payload.get("fieldObservations")
    if not isinstance(raw, list) or not raw:
        raise ResolutionAssemblyError(
            "RESOLUTION_CANDIDATE_OBSERVATIONS_MISSING",
            "A persisted candidate revision carries no field observations.",
            candidateId=str(source.candidate_id),
            revision=source.revision,
        )
    observations: list[Mapping[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ResolutionAssemblyError(
                "RESOLUTION_CANDIDATE_OBSERVATION_INVALID",
                "A persisted field observation is not an object.",
                candidateId=str(source.candidate_id),
                revision=source.revision,
            )
        observations.append(item)
    return tuple(observations)


def _values(
    observations: Sequence[Mapping[str, object]],
    field_keys: Sequence[str],
    *,
    normalized: bool = False,
) -> tuple[str, ...]:
    values: set[str] = set()
    for observation in observations:
        if observation.get("fieldKey") not in field_keys:
            continue
        value = observation.get("value")
        if not isinstance(value, Mapping):
            continue
        text = value.get("normalized") or value.get("original")
        if not isinstance(text, str) or not text.strip():
            continue
        candidate = _canonical_text(text) if normalized else text.strip()
        values.add(candidate)
    return tuple(sorted(values))


def _canonical_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def _observation_ids(
    observations: Sequence[Mapping[str, object]],
) -> tuple[UUID, ...]:
    identities: set[UUID] = set()
    for observation in observations:
        digest = observation.get("observationId")
        if not isinstance(digest, str) or not digest:
            raise ResolutionAssemblyError(
                "RESOLUTION_CANDIDATE_OBSERVATION_IDENTITY_MISSING",
                "A persisted field observation has no canonical identity.",
            )
        identities.add(uuid5(_OBSERVATION_ID_NAMESPACE, digest))
    return tuple(sorted(identities, key=lambda value: value.hex))


def _source_artifact_ids(
    observations: Sequence[Mapping[str, object]],
) -> tuple[UUID, ...]:
    identities: set[UUID] = set()
    for observation in observations:
        evidence = observation.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        raw = evidence.get("rawArtifactId")
        if raw is None:
            continue
        try:
            identities.add(UUID(str(raw)))
        except (TypeError, ValueError) as exc:
            raise ResolutionAssemblyError(
                "RESOLUTION_CANDIDATE_ARTIFACT_IDENTITY_INVALID",
                "A persisted observation evidence reference has no valid artifact identity.",
            ) from exc
    return tuple(sorted(identities, key=lambda value: value.hex))


def _source_keys(payload: Mapping[str, object]) -> tuple[str, ...]:
    policy = payload.get("exportPolicy")
    if not isinstance(policy, Mapping):
        raise ResolutionAssemblyError(
            "RESOLUTION_CANDIDATE_POLICY_MISSING",
            "A persisted candidate revision carries no export policy.",
        )
    source_key = policy.get("sourceKey")
    if not isinstance(source_key, str) or not source_key:
        raise ResolutionAssemblyError(
            "RESOLUTION_CANDIDATE_SOURCE_KEY_MISSING",
            "A persisted candidate revision carries no source key.",
        )
    return (source_key,)


def _geography(
    payload: Mapping[str, object],
    observations: Sequence[Mapping[str, object]],
    market_area: ResolutionMarketArea,
    source: ResolutionCandidateSource,
) -> GeographyReference:
    geography = payload.get("geography")
    if not isinstance(geography, Mapping):
        raise ResolutionAssemblyError(
            "RESOLUTION_CANDIDATE_GEOGRAPHY_MISSING",
            "A persisted candidate revision carries no geography decision.",
            candidateId=str(source.candidate_id),
        )
    coverage = geography.get("coverage")
    payload_revision = geography.get("marketAreaRevision")
    payload_digest = geography.get("marketAreaDigest")
    if payload_revision != market_area.revision or payload_digest != market_area.digest:
        raise ResolutionAssemblyError(
            "RESOLUTION_MARKET_AREA_CONFLICT",
            "The candidate geography does not belong to the sealed market area.",
            candidateId=str(source.candidate_id),
            payloadRevision=payload_revision,
            marketAreaRevision=market_area.revision,
        )
    if not isinstance(coverage, str):
        raise ResolutionAssemblyError(
            "RESOLUTION_CANDIDATE_GEOGRAPHY_UNSUPPORTED",
            "The candidate geography carries no coverage value.",
            candidateId=str(source.candidate_id),
        )
    try:
        coverage_value = GeographyCoverage(coverage)
    except ValueError as exc:
        raise ResolutionAssemblyError(
            "RESOLUTION_CANDIDATE_GEOGRAPHY_UNSUPPORTED",
            "The candidate geography coverage is not a contract value.",
            coverage=coverage,
        ) from exc
    if coverage_value is GeographyCoverage.UNKNOWN:
        return GeographyReference(
            coverage=coverage_value,
            market_area_revision=market_area.revision,
            market_area_digest=market_area.digest,
        )
    latitude = _decimal(geography.get("latitude"), "latitude", source)
    longitude = _decimal(geography.get("longitude"), "longitude", source)
    return GeographyReference(
        coverage=coverage_value,
        market_area_revision=market_area.revision,
        market_area_digest=market_area.digest,
        latitude=latitude,
        longitude=longitude,
        observation_ids=_coordinate_observation_ids(observations, source),
    )


def _decimal(value: object, name: str, source: ResolutionCandidateSource) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ResolutionAssemblyError(
            "RESOLUTION_CANDIDATE_COORDINATE_INVALID",
            "A classified candidate has no usable coordinate.",
            field=name,
            candidateId=str(source.candidate_id),
        ) from exc


def _coordinate_observation_ids(
    observations: Sequence[Mapping[str, object]],
    source: ResolutionCandidateSource,
) -> tuple[UUID, ...]:
    identities = _observation_ids(
        tuple(item for item in observations if item.get("fieldKey") == _COORDINATE_FIELD_KEY)
    )
    if not identities:
        raise ResolutionAssemblyError(
            "RESOLUTION_CANDIDATE_COORDINATE_PROVENANCE_MISSING",
            "A classified candidate geography has no coordinate observation.",
            candidateId=str(source.candidate_id),
        )
    return identities
