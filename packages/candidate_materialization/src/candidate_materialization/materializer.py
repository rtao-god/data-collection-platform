from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID, uuid5

from collection_contracts import (
    CandidateEvidence,
    CandidateRevision,
    EmailObservationValue,
    FieldObservation,
    ObservationBatch,
    ObservationState,
    PhoneObservationValue,
    StringSetObservationValue,
    StructuredAddressObservationValue,
    TextObservationValue,
    UrlObservationValue,
    candidate_snapshot_digest,
    canonical_digest,
    verify_observation_batch,
)
from resolution_contracts import (
    CandidateField,
    EntityKind,
    GeographyCoverage,
    GeographyReference,
    ResolutionCandidate,
)
from resolution_contracts.identity import deterministic_cluster_id

_OBSERVATION_NAMESPACE = UUID("a4b02859-eecb-5653-8f31-3011deae05dd")


def materialize_candidate(
    batch: ObservationBatch,
    *,
    candidate_id: UUID,
    entity_kind: EntityKind,
    source_key: str,
    source_artifact_ids: tuple[UUID, ...],
    observation_artifact_ids: Mapping[str, UUID],
    field_projection: Mapping[str, CandidateField],
    geography: GeographyReference,
    recorded_at_utc: datetime,
    correlation_id: str,
    revision: int = 0,
) -> tuple[CandidateRevision, ResolutionCandidate]:
    """Build an immutable candidate revision and resolution input from exact observations."""
    verify_observation_batch(batch)
    geography = GeographyReference.model_validate(
        geography.model_dump(mode="python", by_alias=True)
    )
    # A classified coverage is admitted only when it carries the exact point and the
    # observation identity that produced it; GeographyReference itself rejects any
    # classified coverage without them, so no unverified coverage can reach a revision.
    if geography.coverage is not GeographyCoverage.UNKNOWN and not geography.observation_ids:
        raise ValueError("classified candidate geography requires observation provenance")
    if len(source_artifact_ids) != len(set(source_artifact_ids)):
        raise ValueError("candidate source artifact identities must be unique")
    if not source_key or not field_projection:
        raise ValueError("candidate source key and explicit field projection are required")
    if not source_artifact_ids:
        raise ValueError("candidate materialization requires source artifact provenance")
    observed = tuple(item for item in batch.observations if item.state is ObservationState.OBSERVED)
    if not observed:
        raise ValueError("candidate materialization requires at least one observed field")

    evidence = tuple(
        CandidateEvidence(
            position=position,
            evidence_kind="source_observation",
            evidence_digest=observation.observation_id,
        )
        for position, observation in enumerate(
            sorted(batch.observations, key=lambda item: item.observation_id)
        )
    )
    observation_ids = tuple(
        sorted({_observation_uuid(observation.observation_id) for observation in observed})
    )
    values: dict[CandidateField, set[str]] = {field: set() for field in CandidateField}
    for observation in observed:
        target = field_projection.get(observation.field_key)
        if target is None:
            continue
        value = observation.value
        if isinstance(value, TextObservationValue) and target in {
            CandidateField.NAMES,
            CandidateField.ADDRESSES,
        }:
            values[target].add(_normalize_candidate_text(value.normalized))
        elif isinstance(value, PhoneObservationValue) and target is CandidateField.PHONES:
            values[target].add(value.e164)
        elif (isinstance(value, EmailObservationValue) and target is CandidateField.EMAILS) or (
            isinstance(value, UrlObservationValue) and target is CandidateField.WEBSITE_URLS
        ):
            values[target].add(value.normalized)
        elif isinstance(value, StringSetObservationValue) and target in {
            CandidateField.NAMES,
            CandidateField.ADDRESSES,
        }:
            values[target].update(
                _normalize_candidate_text(item) for item in value.normalized_values
            )
        elif (
            isinstance(value, StructuredAddressObservationValue)
            and target is CandidateField.ADDRESSES
        ):
            parts = (
                value.street_address,
                value.postal_code,
                value.locality,
                value.region,
                value.country_code,
                value.free_form,
            )
            rendered = _normalize_candidate_text(
                ", ".join(part.strip() for part in parts if part and part.strip())
            )
            if rendered:
                values[target].add(rendered)
        else:
            raise ValueError(
                f"observation field {observation.field_key!r} cannot project to {target.value!r}"
            )

    normalized_payload: dict[str, object] = {
        "fieldObservations": [
            _bound_observation_evidence(item, observation_artifact_ids)
            for item in sorted(batch.observations, key=lambda item: item.observation_id)
        ],
        "projection": {key: value.value for key, value in sorted(field_projection.items())},
        # The decided geography travels inside the immutable revision so a later export
        # reads it instead of re-deriving coverage from mutable state.
        "geography": geography.model_dump(mode="json", by_alias=True),
    }
    lineage_digest = canonical_digest(
        {
            "batchDigest": batch.content_digest,
            "extractedRecordDigest": batch.extracted_record_digest,
            "rawArtifactDigest": batch.raw_artifact_digest,
            "sourcePolicyDigest": batch.source_policy_digest,
            "sourceRecordId": batch.source_record_id,
            "sourceArtifactIds": [str(item) for item in sorted(source_artifact_ids)],
            "sourceKey": source_key,
        }
    )
    cluster_id = deterministic_cluster_id((candidate_id,))
    snapshot_digest = candidate_snapshot_digest(
        candidate_id=candidate_id,
        revision=revision,
        entity_kind=entity_kind.value,
        cluster_id=cluster_id,
        resolution_state="review",
        normalized_payload=normalized_payload,
        evidence=evidence,
        source_lineage_digest=lineage_digest,
    )
    candidate_revision = CandidateRevision(
        candidate_id=candidate_id,
        revision=revision,
        entity_kind=entity_kind.value,
        cluster_id=cluster_id,
        resolution_state="review",
        snapshot_digest=snapshot_digest,
        source_lineage_digest=lineage_digest,
        normalized_payload=normalized_payload,
        evidence=evidence,
        recorded_at_utc=recorded_at_utc,
        correlation_id=correlation_id,
    )
    resolution_candidate = ResolutionCandidate(
        candidate_id=candidate_id,
        entity_kind=entity_kind,
        names=tuple(sorted(values[CandidateField.NAMES])),
        phones=tuple(sorted(values[CandidateField.PHONES])),
        emails=tuple(sorted(values[CandidateField.EMAILS])),
        website_urls=tuple(sorted(values[CandidateField.WEBSITE_URLS])),
        addresses=tuple(sorted(values[CandidateField.ADDRESSES])),
        observation_ids=observation_ids,
        source_artifact_ids=tuple(sorted(source_artifact_ids)),
        source_keys=(source_key,),
        geography=geography,
    )
    return candidate_revision, resolution_candidate


def _normalize_candidate_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def _observation_uuid(observation_id: str) -> UUID:
    return uuid5(_OBSERVATION_NAMESPACE, observation_id)


def _bound_observation_evidence(
    observation: FieldObservation,
    observation_artifact_ids: Mapping[str, UUID],
) -> dict[str, object]:
    """Seal the observation with the exact artifact identity its evidence was read from.

    The field-observation contract identifies its source by content digest only, while every
    downstream owner (resolution provenance, quality, sealed export) proves a field against
    the artifact identity. An observation that already carries that identity keeps it; one
    that carries only the digest receives the identity its caller resolved for this exact
    run, and an unbound digest stays a defect instead of a guessed value.
    """

    payload = observation.model_dump(mode="json", by_alias=True)
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        # A source-native observation may legitimately record no evidence at all; the export
        # then proves no field against it instead of being handed an invented identity.
        return payload
    recorded = evidence.get("rawArtifactId")
    if isinstance(recorded, str) and recorded:
        return payload
    digest = evidence.get("rawArtifactDigest")
    artifact_id = observation_artifact_ids.get(digest) if isinstance(digest, str) else None
    if artifact_id is None:
        raise ValueError(
            "observation evidence does not bind exactly one source artifact: "
            f"observationId={observation.observation_id} rawArtifactDigest={digest}"
        )
    evidence["rawArtifactId"] = str(artifact_id)
    return payload
