from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from entity_resolution_core import resolve_entities
from quality_core import evaluate_cluster_quality
from resolution_contracts import (
    CandidateField,
    ClusterQualityRule,
    EntityKind,
    GeographyCoverage,
    GeographyReference,
    ManualDecisionAction,
    ManualResolutionDecisionPayload,
    MarketAreaReadiness,
    ResolutionBatchPayload,
    ResolutionCandidate,
    ResolutionThresholds,
    seal_manual_decision,
    seal_resolution_batch,
)

_MARKET_DIGEST = f"sha256:{'b' * 64}"


def _candidate(
    identity: int,
    *,
    name: str,
    phone: str | None = None,
    website: str | None = "https://shared.example/",
    source_key: str | None = None,
    provenance: bool = True,
    coverage: GeographyCoverage = GeographyCoverage.INSIDE,
) -> ResolutionCandidate:
    return ResolutionCandidate(
        candidate_id=UUID(int=identity),
        entity_kind=EntityKind.PLACE,
        names=(name,) if name else (),
        phones=(phone,) if phone else (),
        emails=(),
        website_urls=(website,) if website else (),
        addresses=(),
        observation_ids=(UUID(int=100 + identity),) if provenance else (),
        source_artifact_ids=(UUID(int=200 + identity),) if provenance else (),
        source_keys=(source_key or f"source_{identity}",) if provenance else (),
        geography=GeographyReference(
            coverage=coverage,
            market_area_revision="berlin-quality-test@1",
            market_area_digest=_MARKET_DIGEST,
            latitude=Decimal("52.500000") if coverage is not GeographyCoverage.UNKNOWN else None,
            longitude=Decimal("13.400000") if coverage is not GeographyCoverage.UNKNOWN else None,
            observation_ids=(UUID(int=300 + identity),)
            if coverage is not GeographyCoverage.UNKNOWN
            else (),
        ),
    )


def _rule(*, minimum_sources: int = 2) -> ClusterQualityRule:
    return ClusterQualityRule(
        entity_kind=EntityKind.PLACE,
        required_fields=(CandidateField.NAMES, CandidateField.WEBSITE_URLS),
        single_value_fields=(CandidateField.WEBSITE_URLS,),
        minimum_distinct_source_count=minimum_sources,
        allowed_geography=(GeographyCoverage.BOUNDARY, GeographyCoverage.INSIDE),
        boundary_requires_review=True,
        pending_review_blocks_export=True,
    )


def _batch(
    candidates: tuple[ResolutionCandidate, ...],
    *,
    rules: tuple[ClusterQualityRule, ...] | None = None,
    decisions: tuple[object, ...] = (),
):
    return seal_resolution_batch(
        ResolutionBatchPayload(
            batch_id=UUID(int=900),
            market_area_revision="berlin-quality-test@1",
            market_area_digest=_MARKET_DIGEST,
            market_area_readiness=MarketAreaReadiness.READY,
            candidates=tuple(sorted(candidates, key=lambda item: item.candidate_id.hex)),
            manual_decisions=tuple(decisions),
            thresholds=ResolutionThresholds(
                candidate_limit=20,
                pair_limit=200,
                name_review_minimum_bps=7_000,
                address_review_minimum_bps=8_000,
                website_auto_match_minimum_strong_features=2,
                address_auto_match_minimum_strong_features=2,
                fuzzy_primary_area_review_reason_code="FUZZY_BERLIN_MATCH_REQUIRES_REVIEW",
            ),
            quality_rules=(_rule(),) if rules is None else rules,
        )
    )


def _assess(batch):
    core = resolve_entities(batch)
    return core, evaluate_cluster_quality(
        batch,
        core.clusters,
        core.pair_resolutions,
        core.pending_review_pair_ids,
    )


def test_quality_becomes_eligible_only_after_every_blocker_passes() -> None:
    batch = _batch(
        (
            _candidate(1, name="alpha", phone="+49301111111", source_key="source_a"),
            _candidate(2, name="beta", phone="+49301111111", source_key="source_b"),
        )
    )

    _, assessments = _assess(batch)

    assert len(assessments) == 1
    assert assessments[0].export_eligible is True
    assert assessments[0].issues == ()


def test_quality_blocks_conflicts_missing_provenance_geography_and_pending_review() -> None:
    batch = _batch(
        (
            _candidate(
                1,
                name="berlin audio studio",
                website="https://one.example/",
                provenance=False,
            ),
            _candidate(
                2,
                name="berlin audio studios",
                website="https://two.example/",
                coverage=GeographyCoverage.BOUNDARY,
            ),
        )
    )

    _, assessments = _assess(batch)
    codes = {issue.code for assessment in assessments for issue in assessment.issues}

    assert "MISSING_OBSERVATION_PROVENANCE" in codes
    assert "GEOGRAPHY_BOUNDARY_REVIEW_REQUIRED" in codes
    assert "PENDING_RESOLUTION_REVIEW" in codes
    assert all(assessment.export_eligible is False for assessment in assessments)


def test_missing_policy_and_outside_geography_fail_closed() -> None:
    batch = _batch(
        (_candidate(1, name="outside", coverage=GeographyCoverage.OUTSIDE),),
        rules=(),
    )

    _, assessments = _assess(batch)

    assert assessments[0].export_eligible is False
    assert {item.code for item in assessments[0].issues} == {"MISSING_QUALITY_POLICY"}


def test_single_value_conflict_and_separation_block_export() -> None:
    pair = tuple(sorted((UUID(int=1), UUID(int=3)), key=lambda value: value.hex))
    separate = seal_manual_decision(
        ManualResolutionDecisionPayload(
            decision_id=UUID(int=700),
            left_candidate_id=pair[0],
            right_candidate_id=pair[1],
            action=ManualDecisionAction.SEPARATE,
            revision=1,
            actor_reference="reviewer-test",
            reason_code="TEST_SEPARATE",
        )
    )
    batch = _batch(
        (
            _candidate(
                1,
                name="alpha",
                phone="+49301111111",
                website="https://one.example/",
                source_key="source_a",
            ),
            _candidate(
                2,
                name="beta",
                phone="+49301111111",
                website="https://two.example/",
                source_key="source_b",
            ),
            _candidate(
                3,
                name="gamma",
                phone="+49302222222",
                website="https://three.example/",
                source_key="source_c",
            ),
        ),
        decisions=(separate,),
    )

    _, assessments = _assess(batch)
    codes = {issue.code for assessment in assessments for issue in assessment.issues}

    assert "SINGLE_VALUE_FIELD_CONFLICT" in codes
    assert all(assessment.export_eligible is False for assessment in assessments)


def test_outside_geography_is_blocked_by_present_policy() -> None:
    batch = _batch(
        (_candidate(1, name="outside", coverage=GeographyCoverage.OUTSIDE),),
        rules=(_rule(minimum_sources=1),),
    )

    _, assessments = _assess(batch)

    assert assessments[0].export_eligible is False
    assert {item.code for item in assessments[0].issues} == {"GEOGRAPHY_NOT_ALLOWED"}
