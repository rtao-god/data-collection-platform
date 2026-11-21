"""Read-only PostgreSQL ports that feed the sealed export assembly.

Both readers only read. They never evaluate quality, never insert, and never repair
state: an absent or incomplete row set stays absent so the assembler can fail closed
with owner context instead of publishing a partial catalog.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from collection_contracts import (
    ExportContact,
    ExportFieldEvidence,
    ExportGeography,
    ExportLocalizedName,
    ExportQualityIssue,
    ExportQualitySummary,
    ExportReviewDecision,
    ExportSourcePolicy,
    UsagePolicy,
)
from collection_contracts.export_package import ContactKind
from collection_infrastructure.export.assembly import ExportCandidateSource
from collection_infrastructure.export.service import ExportServiceError
from collection_infrastructure.resolution.decisions import (
    current_decision_basis,
    recorded_decision_basis,
)
from resolution_contracts import ClusterQualityAssessment, ClusterQualityIssue, QualitySeverity

_CANDIDATE_COLUMNS = (
    "candidate_id",
    "revision",
    "cluster_id",
    "snapshot_digest",
    "entity_kind",
    "normalized_payload",
)


@dataclass(frozen=True, slots=True)
class ExportRow:
    candidate_id: UUID
    revision: int
    cluster_id: UUID
    snapshot_digest: str
    entity_kind: str
    normalized_payload: Mapping[str, object]
    evidence_digests: tuple[str, ...]
    export_eligible: bool
    policy_digest: str
    evaluation_digest: str
    blockers: tuple[str, ...]
    decision_id: UUID | None
    decision_outcome: str | None
    decision_digest: str | None
    decided_at_utc: datetime | None
    source_key: str
    legal_status: str
    usage_policy: str | None
    attribution: str | None


class _SealedExportPolicy(NamedTuple):
    source_key: str
    legal_status: str
    usage_policy: str | None
    attribution: str | None


def _sealed_export_policy(
    payload: Mapping[str, object], candidate_id: UUID, revision: int
) -> _SealedExportPolicy:
    """Read the export policy facts sealed into the immutable revision.

    The usage policy and attribution were decided when the revision was recorded against
    the run's exact source policy. Re-deriving them here from mutable campaign state would
    let one candidate be exported under a policy it was never collected under.
    """

    facts = payload.get("exportPolicy")
    if not isinstance(facts, Mapping):
        raise _owner_gap(
            code="EXPORT_CANDIDATE_POLICY_MISSING",
            owner="CandidateMaterialization",
            message="The candidate revision carries no sealed export policy.",
            context={"candidateId": str(candidate_id), "candidateRevision": str(revision)},
            action="Re-materialize the candidate revision under its source policy.",
        )
    source_key = facts.get("sourceKey")
    legal_status = facts.get("legalStatus")
    usage_policy = facts.get("usagePolicy")
    attribution = facts.get("attribution")
    if (
        not isinstance(source_key, str)
        or not isinstance(legal_status, str)
        or (usage_policy is not None and not isinstance(usage_policy, str))
    ):
        raise _owner_gap(
            code="EXPORT_CANDIDATE_POLICY_INVALID",
            owner="CandidateMaterialization",
            message="The sealed export policy is incomplete.",
            context={"candidateId": str(candidate_id), "candidateRevision": str(revision)},
            action="Re-materialize the candidate revision under its source policy.",
        )
    return _SealedExportPolicy(
        source_key=source_key,
        legal_status=legal_status,
        usage_policy=usage_policy,
        attribution=attribution if isinstance(attribution, str) else None,
    )


def _require_current_decision_basis(connection: Connection, run_id: UUID) -> None:
    """A verdict derived from decisions the operators have since replaced authorizes nothing.

    The resolution owner records which operator decisions its resolution reflected. When the
    run's decisions have moved on, the stored verdicts describe a resolution input that no
    longer exists, so the export fails closed instead of publishing a stale verdict.
    """

    recorded = recorded_decision_basis(connection, run_id)
    if recorded is None:
        return
    current = current_decision_basis(connection, run_id)
    if current == recorded:
        return
    raise _owner_gap(
        code="EXPORT_QUALITY_DECISION_BASIS_STALE",
        owner="ResolutionPairDecisions",
        message="The run's operator pair decisions changed after its current resolution.",
        context={"runId": str(run_id), "recordedBasis": recorded, "currentBasis": str(current)},
        action=(
            "Re-resolve the run and export the resolution that reflects the current decisions."
        ),
    )


def _blocker_codes(value: object) -> tuple[str, ...]:
    """Blocker codes are comma-separated in the authority query; their regex forbids commas."""
    if not isinstance(value, str) or not value:
        return ()
    return tuple(code for code in value.split(",") if code)


def _read_rows(connection: Connection) -> Sequence[sa.Row[tuple[object, ...]]]:
    return list(
        connection.execute(
            sa.text(
                """
                SELECT
                    q.authoritative_count,
                    r.candidate_id,
                    r.revision,
                    -- Stage 7 owns cluster membership: the resolution that produced the
                    -- authoritative verdict names the cluster, and the provisional
                    -- single-candidate cluster intake recorded is only a fallback.
                    COALESCE(q.cluster_id, r.cluster_id) AS cluster_id,
                    r.snapshot_digest,
                    c.entity_kind,
                    r.normalized_payload,
                    q.export_eligible,
                    q.policy_digest,
                    q.evaluation_digest,
                    q.blocker_codes,
                    d.decision_id,
                    d.outcome AS decision_outcome,
                    d.command_digest,
                    d.decided_at_utc
                FROM candidates.candidate_revisions AS r
                JOIN candidates.candidates AS c
                  ON c.candidate_id = r.candidate_id
                -- The authoritative quality verdict is the one produced by the run's
                -- current resolution, decided by the explicit supersession chain. A second
                -- surviving verdict is ambiguity, not a row to pick arbitrarily.
                LEFT JOIN LATERAL (
                    SELECT
                        count(*) AS authoritative_count,
                        (array_agg(q.export_eligible))[1] AS export_eligible,
                        (array_agg(q.policy_digest))[1] AS policy_digest,
                        (array_agg(q.evaluation_digest))[1] AS evaluation_digest,
                        (array_agg(array_to_string(q.blockers, ',')))[1] AS blocker_codes,
                        (array_agg(m.cluster_id))[1] AS cluster_id
                    FROM quality.quality_evaluations AS q
                    JOIN candidates.resolution_runs AS rr
                      ON rr.resolution_id = q.resolution_id
                    LEFT JOIN candidates.resolution_cluster_members AS m
                      ON m.resolution_id = q.resolution_id
                     AND m.candidate_id = q.candidate_id
                    WHERE q.candidate_id = r.candidate_id
                      AND q.candidate_revision = r.revision
                      AND NOT EXISTS (
                          SELECT 1 FROM candidates.resolution_runs AS s
                          WHERE s.supersedes_resolution_id = rr.resolution_id
                      )
                ) AS q ON TRUE
                LEFT JOIN review.review_cases AS rc
                  ON rc.candidate_id = r.candidate_id
                 AND rc.candidate_revision = r.revision
                LEFT JOIN review.review_case_revisions AS rr
                  ON rr.case_id = rc.case_id
                 AND rr.current_decision_id IS NOT NULL
                LEFT JOIN review.review_decisions AS d
                  ON d.decision_id = rr.current_decision_id
                ORDER BY r.candidate_id, r.revision
                """
            )
        ).all()
    )


def _read_evidence(connection: Connection) -> Mapping[tuple[UUID, int], tuple[str, ...]]:
    grouped: dict[tuple[UUID, int], list[str]] = {}
    for candidate_id, revision, evidence_digest in connection.execute(
        sa.text(
            """
            SELECT candidate_id, candidate_revision, evidence_digest
            FROM candidates.candidate_revision_evidence
            ORDER BY candidate_id, candidate_revision, position
            """
        )
    ).all():
        grouped.setdefault((candidate_id, revision), []).append(evidence_digest)
    return {key: tuple(value) for key, value in grouped.items()}


class PostgresExportCandidateReader:
    """Reads candidate revisions exactly as persisted; performs no projection of its own."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read_rows(
        self,
        *,
        candidate_revisions: Sequence[tuple[UUID, int]] | None = None,
    ) -> Sequence[ExportRow]:
        wanted = None if candidate_revisions is None else set(candidate_revisions)
        with self._engine.connect() as connection:
            evidence = _read_evidence(connection)
            rows = []
            for row in _read_rows(connection):
                if wanted is not None and (row.candidate_id, row.revision) not in wanted:
                    # An explicitly requested revision set never depends on unrelated rows:
                    # a legacy or foreign revision cannot block the exact requested export.
                    continue
                if int(row.authoritative_count or 0) > 1:
                    raise _owner_gap(
                        code="EXPORT_QUALITY_AUTHORITY_AMBIGUOUS",
                        owner="CandidateQuality",
                        message=(
                            "A candidate revision has more than one authoritative quality "
                            "evaluation."
                        ),
                        context={**_row_context_row(row), "verdictCount": row.authoritative_count},
                        action=(
                            "Supersede the stale resolution so exactly one verdict is current."
                        ),
                    )
                payload = row.normalized_payload
                if not isinstance(payload, Mapping):
                    raise ValueError(
                        "candidate revision payload is not a JSON object: "
                        f"candidateId={row.candidate_id} revision={row.revision}"
                    )
                policy = _sealed_export_policy(payload, row.candidate_id, row.revision)
                rows.append(
                    ExportRow(
                        candidate_id=row.candidate_id,
                        revision=row.revision,
                        cluster_id=row.cluster_id,
                        snapshot_digest=row.snapshot_digest,
                        entity_kind=row.entity_kind,
                        normalized_payload=payload,
                        evidence_digests=evidence.get((row.candidate_id, row.revision), ()),
                        export_eligible=bool(row.export_eligible)
                        if row.export_eligible is not None
                        else False,
                        policy_digest=row.policy_digest or "",
                        evaluation_digest=row.evaluation_digest or "",
                        blockers=_blocker_codes(row.blocker_codes),
                        decision_id=row.decision_id,
                        decision_outcome=row.decision_outcome,
                        decision_digest=row.command_digest,
                        decided_at_utc=row.decided_at_utc,
                        source_key=policy.source_key,
                        legal_status=policy.legal_status,
                        usage_policy=policy.usage_policy,
                        attribution=policy.attribution,
                    )
                )
            return tuple(rows)


class PostgresExportQualityReader:
    """Reads the singular quality owner's persisted assessments."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read_assessments(
        self, *, run_id: UUID, cluster_ids: Sequence[UUID]
    ) -> Mapping[UUID, ClusterQualityAssessment]:
        if not cluster_ids:
            return {}
        with self._engine.connect() as connection:
            _require_current_decision_basis(connection, run_id)
            rows = connection.execute(
                sa.text(
                    """
                    SELECT
                        m.cluster_id,
                        count(DISTINCT q.evaluation_id) AS authoritative_count,
                        (array_agg(q.export_eligible))[1] AS export_eligible,
                        (array_agg(array_to_string(q.blockers, ',')))[1] AS blocker_codes
                    FROM quality.quality_evaluations AS q
                    JOIN candidates.resolution_runs AS rr
                      ON rr.resolution_id = q.resolution_id
                    JOIN candidates.resolution_cluster_members AS m
                      ON m.resolution_id = q.resolution_id
                     AND m.candidate_id = q.candidate_id
                    WHERE m.cluster_id IN :cluster_ids
                      AND NOT EXISTS (
                          SELECT 1 FROM candidates.resolution_runs AS s
                          WHERE s.supersedes_resolution_id = rr.resolution_id
                      )
                    GROUP BY m.cluster_id
                    ORDER BY m.cluster_id
                    """
                ).bindparams(sa.bindparam("cluster_ids", expanding=True)),
                {"cluster_ids": list(cluster_ids)},
            ).all()

        assessments: dict[UUID, ClusterQualityAssessment] = {}
        for cluster_id, authoritative_count, export_eligible, blocker_codes in rows:
            if int(authoritative_count) > 1:
                raise _owner_gap(
                    code="EXPORT_QUALITY_AUTHORITY_AMBIGUOUS",
                    owner="CandidateQuality",
                    message="A cluster has more than one authoritative quality verdict.",
                    context={
                        "clusterId": str(cluster_id),
                        "verdictCount": str(authoritative_count),
                    },
                    action="Supersede the stale resolution so exactly one verdict is current.",
                )
            assessments[cluster_id] = ClusterQualityAssessment(
                cluster_id=cluster_id,
                export_eligible=export_eligible,
                issues=tuple(
                    ClusterQualityIssue(code=code, severity=QualitySeverity.BLOCKING)
                    for code in _blocker_codes(blocker_codes)
                ),
            )
        return assessments


_FIELD_KEYS = {
    "names": "names",
    "phones": "phones",
    "emails": "emails",
    "website_urls": "website_urls",
    "addresses": "addresses",
}
# Coverage a catalog entry can carry. Unresolved and outside coverage stay blocked at the
# owner: an unclassified candidate is not a Berlin entry by default.
_EXPORTABLE_COVERAGE_VALUES = frozenset({"inside", "boundary"})
# The only source legal statuses that grant a redistribution right; every other status,
# `research_only` above all, keeps a merged candidate non-exportable.
_REDISTRIBUTABLE_LEGAL_STATUSES = frozenset({"approved", "reference_only"})


def _authorizes_redistribution(legal_status: str) -> bool:
    return legal_status in _REDISTRIBUTABLE_LEGAL_STATUSES


class PostgresExportCandidatePort:
    """Adapts persisted rows into sealed export sources without inventing anything.

    Every exported fact comes from the candidate revision itself: its observed field
    values carry the source evidence they were recorded with, its geography is the
    decision stored at intake, and its quality and review state come from their owners.
    The schema has no candidate-to-run binding, so the caller names the exact candidate
    revisions of the run it is exporting.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read_candidates(
        self, *, candidate_revisions: Sequence[tuple[UUID, int]]
    ) -> Sequence[ExportCandidateSource]:
        if not candidate_revisions:
            return ()
        wanted = set(candidate_revisions)
        rows = PostgresExportCandidateReader(self._engine).read_rows(
            candidate_revisions=tuple(wanted)
        )
        sources: list[ExportCandidateSource] = []
        for row in rows:
            if (row.candidate_id, row.revision) not in wanted:
                continue
            sources.append(_export_source(row))
        return tuple(sources)


def _export_source(row: ExportRow) -> ExportCandidateSource:
    context = _row_context(row)
    projection = row.normalized_payload.get("projection")
    if not isinstance(projection, Mapping):
        raise _owner_gap(
            code="EXPORT_CANDIDATE_PROJECTION_MISSING",
            owner="CandidateMaterialization",
            message="The candidate revision carries no field projection.",
            context=context,
            action="Re-materialize the candidate revision before export.",
        )
    field_paths = _field_paths(projection)
    observations = _observed_fields(row)
    if not observations:
        raise _owner_gap(
            code="EXPORT_CANDIDATE_OBSERVATIONS_MISSING",
            owner="CandidateMaterialization",
            message="The candidate revision carries no observed field values.",
            context=context,
            action="Re-materialize the candidate revision from its observation batch.",
        )
    evidence = {
        **_field_evidence(row, field_paths=field_paths),
        **_campaign_category_evidence(row),
        **_geography_field_evidence(row),
    }
    return ExportCandidateSource(
        candidate_id=row.candidate_id,
        candidate_revision=row.revision,
        snapshot_digest=row.snapshot_digest,
        entity_kind=row.entity_kind,
        cluster_id=row.cluster_id,
        localized_names=_localized_names(observations, field_paths=field_paths),
        contacts=_contacts(observations, field_paths=field_paths),
        typed_attributes=(),
        category_keys=_campaign_category_keys(row),
        geography=_export_geography(row),
        field_evidence=tuple(evidence[key].record for key in sorted(evidence)),
        quality=_export_quality(row),
        quality_evaluation_digest=row.evaluation_digest,
        quality_policy_digest=row.policy_digest,
        review_decisions=_review_decisions(row),
        source_policies=_source_policies({key: item.record for key, item in evidence.items()}),
        source_artifact_ids=tuple(sorted({item.raw_artifact_id for item in evidence.values()})),
    )


def _row_context_row(row: sa.Row[tuple[object, ...]]) -> dict[str, str]:
    return {"candidateId": str(row.candidate_id), "candidateRevision": str(row.revision)}


def _row_context(row: ExportRow) -> dict[str, str]:
    return {"candidateId": str(row.candidate_id), "candidateRevision": str(row.revision)}


def _export_quality_issue_code(code: str, row: ExportRow) -> str:
    """Project the owner's quality blocker onto the export package vocabulary.

    Quality blockers are owned by the review contracts and use upper-case codes;
    the export package declares lower-case issue codes. The two vocabularies are
    translated explicitly so an unknown blocker cannot be published silently.
    """
    issue_code = _EXPORT_QUALITY_ISSUE_CODES.get(code)
    if issue_code is None:
        raise _owner_gap(
            code="EXPORT_QUALITY_ISSUE_CODE_UNMAPPED",
            owner="CandidateQuality",
            message="The candidate quality blocker has no export issue code.",
            context={**_row_context(row), "blocker": code},
            action="Register the blocker in the export quality vocabulary at its owner.",
        )
    return issue_code


def _field_paths(projection: Mapping[str, object]) -> Mapping[str, str]:
    """Source field key -> the candidate field it was projected onto."""

    paths: dict[str, str] = {}
    for source_field, target in projection.items():
        if isinstance(target, str):
            paths[str(source_field)] = target
    return paths


class _ObservedField(NamedTuple):
    """One observed value with the locale the source or operator recorded for it."""

    field_key: str
    value: str
    locale: str | None


def _observed_fields(row: ExportRow) -> tuple[_ObservedField, ...]:
    """Observed source field values exactly as the revision recorded them."""

    observed: list[_ObservedField] = []
    for observation in _observations(row):
        field_key = observation.get("fieldKey")
        value = observation.get("value")
        if not isinstance(field_key, str) or not isinstance(value, Mapping):
            continue
        normalized = value.get("normalized")
        if isinstance(normalized, str) and normalized:
            locale = value.get("locale")
            observed.append(
                _ObservedField(
                    field_key=field_key,
                    value=normalized,
                    locale=locale if isinstance(locale, str) and locale else None,
                )
            )
    return tuple(sorted(observed))


def _observations(row: ExportRow) -> tuple[Mapping[str, object], ...]:
    raw = row.normalized_payload.get("fieldObservations")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(
        observation
        for observation in raw
        if isinstance(observation, Mapping) and observation.get("state") == "observed"
    )


def _field_evidence(
    row: ExportRow, *, field_paths: Mapping[str, str]
) -> Mapping[str, _FieldEvidence]:
    """One evidence record per exported field path, from the recorded source evidence."""

    evidence: dict[str, _FieldEvidence] = {}
    for observation in _observations(row):
        field_key = observation.get("fieldKey")
        if not isinstance(field_key, str):
            continue
        field_path = field_paths.get(field_key)
        if field_path is None:
            continue
        evidence[field_path] = _evidence_record(row, observation, field_path=field_path)
    return evidence


def _evidence_record(
    row: ExportRow, observation: Mapping[str, object], *, field_path: str
) -> _FieldEvidence:
    """The sealed evidence one recorded observation carries for one exported field path."""

    detail = observation.get("evidence")
    if not isinstance(detail, Mapping):
        raise _owner_gap(
            code="EXPORT_FIELD_EVIDENCE_INCOMPLETE",
            owner="CandidateMaterialization",
            message="A recorded field observation carries incomplete evidence.",
            context={**_row_context(row), "fieldPath": field_path},
            action="Re-map the source record so every field carries its exact evidence.",
        )
    source_url = detail.get("sourceUrl")
    observed_at = detail.get("observedAtUtc")
    evidence_digest = detail.get("evidenceDigest")
    raw_artifact_id = detail.get("rawArtifactId")
    if not all(
        isinstance(item, str) and item
        for item in (source_url, observed_at, evidence_digest, raw_artifact_id)
    ):
        raise _owner_gap(
            code="EXPORT_FIELD_EVIDENCE_INCOMPLETE",
            owner="CandidateMaterialization",
            message="A recorded field observation carries incomplete evidence.",
            context={**_row_context(row), "fieldPath": field_path},
            action="Re-map the source record so every field carries its exact evidence.",
        )
    return _FieldEvidence(
        record=ExportFieldEvidence(
            field_path=field_path,
            source_key=_source_key(row),
            source_record_id=str(observation.get("sourceRecordId")),
            source_url=str(source_url),
            observed_at_utc=_observed_at(observed_at, row=row, field_path=field_path),
            legal_status=_field_legal_status(observation, row=row, field_path=field_path),
            usage_policy=_field_usage_policy(observation, row=row, field_path=field_path),
            evidence_digest=str(evidence_digest),
            source_policy_digest=_policy_digest(observation, row=row, field_path=field_path),
            attribution=row.attribution,
        ),
        raw_artifact_id=UUID(str(raw_artifact_id)),
    )


class _FieldEvidence(NamedTuple):
    """A sealed evidence record plus the raw artifact it was read from.

    The artifact identity is not part of the exported evidence contract (that carries its
    digest), so it is carried beside it instead of being re-derived from the document.
    """

    record: ExportFieldEvidence
    raw_artifact_id: UUID


def _source_key(row: ExportRow) -> str:
    return row.source_key


def _usage_policy(row: ExportRow) -> UsagePolicy:
    """The sealed usage policy, which must exist before any entry is sealed."""

    if row.usage_policy is None:
        raise _owner_gap(
            code="EXPORT_CANDIDATE_SOURCE_POLICY_NOT_EXPORTABLE",
            owner="CandidateMaterialization",
            message="The sealed source policy grants no redistribution right.",
            context={
                "candidateId": str(row.candidate_id),
                "candidateRevision": str(row.revision),
                "legalStatus": row.legal_status,
            },
            action=(
                "Record an external redistribution decision for this source before "
                "exporting its candidates."
            ),
        )
    return cast("UsagePolicy", row.usage_policy)


def _observed_at(value: object, *, row: ExportRow, field_path: str) -> datetime:
    """The recorded observation time, or a hard failure.

    A missing or unparseable time would silently date the evidence wrongly, so it fails
    at the owner that produced it instead of defaulting to the export time.
    """

    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    raise _owner_gap(
        code="EXPORT_FIELD_EVIDENCE_TIME_INVALID",
        owner="CandidateMaterialization",
        message="A recorded field observation carries no usable observation time.",
        context={**_row_context(row), "fieldPath": field_path},
        action="Re-map the source record so each field keeps its acquisition time.",
    )


def _field_legal_status(
    observation: Mapping[str, object], *, row: ExportRow, field_path: str
) -> str:
    """The legal status recorded for this field's own evidence.

    A merged candidate combines evidence from more than one source grant. Reading the
    per-field value keeps a stricter restriction visible instead of flattening every field
    to the candidate's single summary policy.
    """

    value = observation.get("sourceLegalStatus")
    if value is None:
        return row.legal_status
    if not isinstance(value, str) or not value:
        raise _owner_gap(
            code="EXPORT_FIELD_EVIDENCE_POLICY_INVALID",
            owner="CandidateMaterialization",
            message="A recorded field observation carries an invalid source legal status.",
            context={**_row_context(row), "fieldPath": field_path},
            action="Re-map the source record so each field keeps the status it was read under.",
        )
    return value


def _field_usage_policy(
    observation: Mapping[str, object], *, row: ExportRow, field_path: str
) -> UsagePolicy:
    legal_status = _field_legal_status(observation, row=row, field_path=field_path)
    value = observation.get("sourceUsagePolicy")
    if value is None:
        if legal_status == row.legal_status:
            return _usage_policy(row)
        if not _authorizes_redistribution(legal_status):
            raise _owner_gap(
                code="EXPORT_CANDIDATE_SOURCE_POLICY_NOT_EXPORTABLE",
                owner="CandidateMaterialization",
                message="A merged field carries a source policy without redistribution rights.",
                context={
                    **_row_context(row),
                    "fieldPath": field_path,
                    "legalStatus": legal_status,
                },
                action=(
                    "Record an external redistribution decision for that source before "
                    "exporting a candidate that merges its evidence."
                ),
            )
    if not isinstance(value, str):
        raise _owner_gap(
            code="EXPORT_FIELD_EVIDENCE_POLICY_INVALID",
            owner="CandidateMaterialization",
            message="A recorded field observation carries an invalid source usage policy.",
            context={**_row_context(row), "fieldPath": field_path},
            action="Re-map the source record so each field keeps the policy it was read under.",
        )
    return cast("UsagePolicy", value)


def _policy_digest(observation: Mapping[str, object], *, row: ExportRow, field_path: str) -> str:
    value = observation.get("sourcePolicyDigest")
    if not isinstance(value, str) or not value:
        raise _owner_gap(
            code="EXPORT_FIELD_EVIDENCE_POLICY_MISSING",
            owner="CandidateMaterialization",
            message="A recorded field observation carries no source policy digest.",
            context={**_row_context(row), "fieldPath": field_path},
            action="Re-map the source record so each field keeps the policy it was read under.",
        )
    return value


def _localized_names(
    observations: Sequence[_ObservedField], *, field_paths: Mapping[str, str]
) -> tuple[ExportLocalizedName, ...]:
    return tuple(
        ExportLocalizedName(
            locale=_name_locale(observation),
            state="observed",
            value=observation.value,
            field_path=field_paths[observation.field_key],
        )
        for observation in observations
        if _is_name_field(observation.field_key)
    )


def _contacts(
    observations: Sequence[_ObservedField], *, field_paths: Mapping[str, str]
) -> tuple[ExportContact, ...]:
    return tuple(
        ExportContact(
            kind=_CONTACT_KINDS[observation.field_key],
            state="observed",
            target=observation.value,
            field_path=field_paths[observation.field_key],
        )
        for observation in observations
        if observation.field_key in _CONTACT_KINDS
    )


def _is_name_field(field_key: str) -> bool:
    return field_key == "display_name" or field_key.startswith("display_name:")


def _name_locale(observation: _ObservedField) -> str:
    """The locale a name was published under, or `und` when nothing established one.

    The recorded observation value is authoritative: extraction and normalization keep the
    language the source declared, and an operator-authored record keeps the language the
    operator declared. A tag such as `name:de` is the next best evidence. A plain `name`
    establishes no language at all, so it is exported as undetermined instead of being
    guessed from the campaign's own locale list.
    """

    if observation.locale is not None:
        return observation.locale
    _, _, suffix = observation.field_key.partition(":")
    if suffix and re.fullmatch(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*", suffix):
        return suffix
    return "und"


def _campaign_category_keys(row: ExportRow) -> tuple[str, ...]:
    """The campaign categories the revision observed, as recorded classification evidence.

    A category is a claim about the entry, so it is read from the revision's own observed
    field records - `category_<campaign category>` - and never from a campaign-wide
    default. The revision carries the evidence that supports each claim, which
    `_campaign_category_evidence` re-addresses to the exported category field path.
    """

    return tuple(
        sorted(
            path.removeprefix(_CATEGORY_FIELD_PATH_PREFIX)
            for path in _campaign_category_evidence(row)
        )
    )


# The observation contract's field-key format admits no `:` separator, so a source
# classification travels as `category_<campaign category>`. The key is the meaning: the
# recorded value has to repeat it, and anything else is a malformed claim.
_CATEGORY_OBSERVATION_PREFIX = "category_"
_CATEGORY_FIELD_PATH_PREFIX = "categories/"


def _category_field_path(campaign_category: str) -> str:
    return f"{_CATEGORY_FIELD_PATH_PREFIX}{campaign_category}"


def _campaign_category_evidence(row: ExportRow) -> Mapping[str, _FieldEvidence]:
    """One evidence record per observed campaign category, addressed by category.

    The classification is a field claim like any other: it carries the source it came
    from, when it was observed and the digest of the record it was read from. A claim
    without that detail is refused at its owner instead of being exported as bare text.
    """

    evidence: dict[str, _FieldEvidence] = {}
    for observation in _observations(row):
        field_key = observation.get("fieldKey")
        if not isinstance(field_key, str):
            continue
        if not field_key.startswith(_CATEGORY_OBSERVATION_PREFIX):
            continue
        campaign_category = _observation_category(row, observation, field_key=field_key)
        detail = observation.get("evidence")
        if not isinstance(detail, Mapping):
            raise _owner_gap(
                code="EXPORT_CATEGORY_EVIDENCE_MISSING",
                owner="CandidateMaterialization",
                message="An observed candidate category carries no evidence.",
                context={**_row_context(row), "campaignCategory": campaign_category},
                action="Re-map the source classification so it keeps the record it was read from.",
            )
        evidence[_category_field_path(campaign_category)] = _evidence_record(
            row, observation, field_path=_category_field_path(campaign_category)
        )
    return evidence


def _observation_category(
    row: ExportRow, observation: Mapping[str, object], *, field_key: str
) -> str:
    """The campaign category one observation asserts, checked against its own key."""

    campaign_category = field_key.removeprefix(_CATEGORY_OBSERVATION_PREFIX)
    value = observation.get("value")
    normalized = value.get("normalized") if isinstance(value, Mapping) else None
    if not campaign_category or not isinstance(normalized, str) or not normalized:
        raise _owner_gap(
            code="EXPORT_CATEGORY_VALUE_INVALID",
            owner="CandidateMaterialization",
            message="An observed candidate category carries no category value.",
            context={**_row_context(row), "fieldKey": field_key},
            action="Re-map the source classification so the observed category is carried exactly.",
        )
    if normalized != campaign_category:
        # Boiling the meaning out of the value instead of the key would let one claim be
        # filed under a category and exported as another.
        raise _owner_gap(
            code="EXPORT_CATEGORY_VALUE_CONFLICT",
            owner="CandidateMaterialization",
            message="An observed candidate category disagrees with the field it is filed under.",
            context={
                **_row_context(row),
                "fieldKey": field_key,
                "observedCategory": normalized,
            },
            action=(
                "Re-map the source classification so the category claim and its field key "
                "name the same campaign category."
            ),
        )
    return campaign_category


def _geography_field_evidence(row: ExportRow) -> Mapping[str, _FieldEvidence]:
    """The evidence the exported point claim is proven by.

    A candidate's market-area decision is derived from one observed point, and the export
    states that point as the `geography` claim. Its evidence is the point observation's own
    record, filed under the exported field path. More than one observed point is a source
    conflict, not a choice the reader may make; no point at all leaves the claim without
    evidence, so the geography/ingestion owners block it instead.
    """

    points = [
        observation
        for observation in _observations(row)
        if observation.get("fieldKey") == _GEOGRAPHY_OBSERVATION_FIELD
        and _is_observed_point(observation)
    ]
    if len(points) > 1:
        raise _owner_gap(
            code="EXPORT_GEOGRAPHY_EVIDENCE_AMBIGUOUS",
            owner="CandidateMaterialization",
            message="The candidate revision carries more than one observed point.",
            context={**_row_context(row), "observedPoints": str(len(points))},
            action="Resolve the conflicting points at their source before exporting.",
        )
    if not points:
        return {}
    return {
        _GEOGRAPHY_FIELD_PATH: _evidence_record(row, points[0], field_path=_GEOGRAPHY_FIELD_PATH)
    }


def _is_observed_point(observation: Mapping[str, object]) -> bool:
    value = observation.get("value")
    return isinstance(value, Mapping) and value.get("kind") == "geo_point"


def _export_geography(row: ExportRow) -> ExportGeography:
    geography = row.normalized_payload.get("geography")
    if not isinstance(geography, Mapping):
        raise _owner_gap(
            code="EXPORT_CANDIDATE_GEOGRAPHY_MISSING",
            owner="CandidateGeography",
            message="The candidate revision carries no geography decision.",
            context=_row_context(row),
            action=(
                "Re-run candidate intake against the run's exact boundary artifact so the "
                "revision records its market-area classification."
            ),
        )
    coverage = geography.get("coverage")
    if coverage not in _EXPORTABLE_COVERAGE_VALUES:
        raise _owner_gap(
            code="EXPORT_CANDIDATE_GEOGRAPHY_UNRESOLVED",
            owner="CandidateGeography",
            message="The candidate revision has no exportable market-area coverage.",
            context={**_row_context(row), "geographyCoverage": str(coverage)},
            action=(
                "Observe the candidate's own coordinate evidence and classify it against "
                "the run boundary; unresolved coverage is not exportable."
            ),
        )
    revision = geography.get("marketAreaRevision")
    digest = geography.get("marketAreaDigest")
    if not isinstance(revision, str) or not isinstance(digest, str):
        raise _owner_gap(
            code="EXPORT_CANDIDATE_GEOGRAPHY_PROVENANCE_INVALID",
            owner="CandidateGeography",
            message="The candidate geography names no verified market-area revision.",
            context=_row_context(row),
            action="Repair the candidate revision geography at its owner.",
        )
    latitude = geography.get("latitude")
    longitude = geography.get("longitude")
    return ExportGeography(
        coverage=coverage,
        market_area_revision=revision,
        market_area_digest=digest,
        latitude=float(latitude) if isinstance(latitude, (int, float, str)) else None,
        longitude=float(longitude) if isinstance(longitude, (int, float, str)) else None,
        field_path=_GEOGRAPHY_FIELD_PATH,
    )


def _export_quality(row: ExportRow) -> ExportQualitySummary:
    issues = tuple(
        ExportQualityIssue(
            code=_export_quality_issue_code(code, row),
            severity="blocking",
            required_action="Resolve the recorded blocking quality issue.",
        )
        for code in row.blockers
    )
    if not row.export_eligible:
        return ExportQualitySummary(state="blocked", issues=issues)
    if issues:
        return ExportQualitySummary(state="review_required", issues=issues)
    return ExportQualitySummary(state="passed")


def _review_decisions(row: ExportRow) -> tuple[ExportReviewDecision, ...]:
    if row.decision_id is None or row.decision_digest is None or row.decided_at_utc is None:
        return ()
    return (
        ExportReviewDecision(
            decision_id=row.decision_id,
            decision_kind=str(row.decision_outcome),
            decision_digest=row.decision_digest,
            decided_at_utc=row.decided_at_utc,
        ),
    )


def _source_policies(
    evidence: Mapping[str, ExportFieldEvidence],
) -> tuple[ExportSourcePolicy, ...]:
    policies: dict[str, ExportSourcePolicy] = {}
    for item in evidence.values():
        policies.setdefault(
            item.source_key,
            ExportSourcePolicy(
                source_key=item.source_key,
                policy_digest=item.source_policy_digest,
                legal_status=item.legal_status,
                usage_policy=item.usage_policy,
                attribution=item.attribution,
            ),
        )
    return tuple(policies[key] for key in sorted(policies))


def _owner_gap(
    *, code: str, owner: str, message: str, context: Mapping[str, str], action: str
) -> ExportServiceError:
    return ExportServiceError(
        code=code,
        owner=owner,
        message=message,
        context=context,
        required_action=action,
    )


# Review- and quality-owned blocker -> export package issue code. The export package
# declares lower-case issue codes while the owners emit upper-case ones, so every code an
# owner can actually decide is registered here; anything else fails closed.
_EXPORT_QUALITY_ISSUE_CODES: Mapping[str, str] = {
    "CANDIDATE_REQUIRES_REVIEW": "candidate_requires_review",
    "REQUIRED_FIELD_ABSENT": "required_field_absent",
    "SINGLE_VALUE_FIELD_CONFLICT": "single_value_field_conflict",
    "MIXED_ENTITY_KINDS": "mixed_entity_kinds",
    "MISSING_QUALITY_POLICY": "missing_quality_policy",
    "MISSING_OBSERVATION_PROVENANCE": "missing_observation_provenance",
    "INSUFFICIENT_DISTINCT_SOURCES": "insufficient_distinct_sources",
    "GEOGRAPHY_NOT_ALLOWED": "geography_not_allowed",
    "GEOGRAPHY_BOUNDARY_REVIEW_REQUIRED": "geography_boundary_review_required",
    "PENDING_RESOLUTION_REVIEW": "pending_resolution_review",
}

_GEOGRAPHY_FIELD_PATH = "geography"
# The source-native field that carries a candidate's observed point.
_GEOGRAPHY_OBSERVATION_FIELD = "coordinates"
_CONTACT_KINDS: Mapping[str, ContactKind] = {
    "website": "website",
    "email": "email",
    "phone": "phone",
}


def _string_values(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value if isinstance(item, str))
    return ()
