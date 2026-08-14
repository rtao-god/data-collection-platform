from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from resolution_contracts import (
    CandidateField,
    ClusterQualityAssessment,
    ClusterQualityIssue,
    ClusterQualityRule,
    GeographyCoverage,
    PairFeatures,
    PairResolution,
    QualitySeverity,
    ResolutionBatch,
    ResolutionCandidate,
    ResolvedCluster,
)


def evaluate_cluster_quality(
    batch: ResolutionBatch,
    clusters: tuple[ResolvedCluster, ...],
    pair_resolutions: tuple[PairResolution, ...],
    pending_review_pair_ids: tuple[str, ...],
) -> tuple[ClusterQualityAssessment, ...]:
    candidates = {item.candidate_id: item for item in batch.candidates}
    rules = {item.entity_kind: item for item in batch.quality_rules}
    pairs = {item.features.pair_id: item.features for item in pair_resolutions}
    pending = set(pending_review_pair_ids)
    assessments: list[ClusterQualityAssessment] = []
    for cluster in clusters:
        members = tuple(candidates[item] for item in cluster.member_candidate_ids)
        issues: list[ClusterQualityIssue] = []
        kinds = {item.entity_kind for item in members}
        rule: ClusterQualityRule | None = None
        if len(kinds) != 1:
            issues.append(_block("MIXED_ENTITY_KINDS"))
        else:
            kind = next(iter(kinds))
            rule = rules.get(kind)
            if rule is None:
                issues.append(_block("MISSING_QUALITY_POLICY"))

        if any(
            not item.observation_ids or not item.source_artifact_ids or not item.source_keys
            for item in members
        ):
            issues.append(_block("MISSING_OBSERVATION_PROVENANCE"))

        if rule is not None:
            _evaluate_required_fields(members, rule, issues)
            _evaluate_single_value_fields(members, rule, issues)
            source_count = len({source for member in members for source in member.source_keys})
            if source_count < rule.minimum_distinct_source_count:
                issues.append(_block("INSUFFICIENT_DISTINCT_SOURCES"))
            for member in members:
                coverage = member.geography.coverage
                if coverage not in rule.allowed_geography:
                    issues.append(_block("GEOGRAPHY_NOT_ALLOWED"))
                    break
            if rule.boundary_requires_review and any(
                item.geography.coverage is GeographyCoverage.BOUNDARY for item in members
            ):
                issues.append(_block("GEOGRAPHY_BOUNDARY_REVIEW_REQUIRED"))
            if rule.pending_review_blocks_export:
                touching_pending = tuple(
                    sorted(
                        pair_id
                        for pair_id in pending
                        if _pair_touches(pairs[pair_id], cluster.member_candidate_ids)
                    )
                )
                for pair_id in touching_pending:
                    issues.append(_block("PENDING_RESOLUTION_REVIEW", related_pair_id=pair_id))

        for blocked_pair_id in cluster.blocked_match_edge_ids:
            issues.append(
                _block(
                    "MATCH_EDGE_BLOCKED_BY_SEPARATION",
                    related_blocked_edge_id=blocked_pair_id,
                )
            )
        ordered = tuple(sorted(set(issues), key=_issue_key))
        assessments.append(
            ClusterQualityAssessment(
                cluster_id=cluster.cluster_id,
                export_eligible=not any(
                    item.severity is QualitySeverity.BLOCKING for item in ordered
                ),
                issues=ordered,
            )
        )
    return tuple(sorted(assessments, key=lambda item: item.cluster_id.hex))


def _evaluate_required_fields(
    members: tuple[ResolutionCandidate, ...],
    rule: ClusterQualityRule,
    issues: list[ClusterQualityIssue],
) -> None:
    for field in rule.required_fields:
        if not _field_values(members, field):
            issues.append(_block("REQUIRED_FIELD_ABSENT", field=field))


def _evaluate_single_value_fields(
    members: tuple[ResolutionCandidate, ...],
    rule: ClusterQualityRule,
    issues: list[ClusterQualityIssue],
) -> None:
    for field in rule.single_value_fields:
        if len(_field_values(members, field)) > 1:
            issues.append(_block("SINGLE_VALUE_FIELD_CONFLICT", field=field))


def _field_values(members: Iterable[object], field: CandidateField) -> set[str]:
    attribute = field.value
    values: set[str] = set()
    for member in members:
        values.update(getattr(member, attribute))
    return values


def _pair_touches(features: PairFeatures, member_ids: tuple[UUID, ...]) -> bool:
    identities = set(member_ids)
    return features.left_candidate_id in identities or features.right_candidate_id in identities


def _block(
    code: str,
    *,
    field: CandidateField | None = None,
    related_pair_id: str | None = None,
    related_blocked_edge_id: str | None = None,
) -> ClusterQualityIssue:
    return ClusterQualityIssue(
        code=code,
        severity=QualitySeverity.BLOCKING,
        field=field,
        related_pair_id=related_pair_id,
        related_blocked_edge_id=related_blocked_edge_id,
    )


def _issue_key(issue: ClusterQualityIssue) -> tuple[str, str, str, str, str]:
    return (
        issue.severity.value,
        issue.code,
        issue.field.value if issue.field is not None else "",
        issue.related_pair_id or "",
        issue.related_blocked_edge_id or "",
    )
