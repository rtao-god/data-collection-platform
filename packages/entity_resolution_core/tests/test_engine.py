from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from entity_resolution_core import ResolutionError, resolve_entities
from resolution_contracts import (
    CandidateField,
    ClusterLineageKind,
    ClusterQualityRule,
    EntityKind,
    GeographyCoverage,
    GeographyReference,
    ManualDecisionAction,
    ManualResolutionDecisionPayload,
    MarketAreaReadiness,
    MatchDisposition,
    PriorClusterPayload,
    ResolutionBatchPayload,
    ResolutionCandidate,
    ResolutionThresholds,
    deterministic_cluster_id,
    seal_manual_decision,
    seal_prior_cluster,
    seal_resolution_batch,
)

_MARKET_DIGEST = f"sha256:{'a' * 64}"


def _candidate(
    identity: int,
    *,
    name: str,
    phone: str | None = None,
    email: str | None = None,
    website: str | None = None,
    address: str | None = None,
    coverage: GeographyCoverage = GeographyCoverage.INSIDE,
) -> ResolutionCandidate:
    return ResolutionCandidate(
        candidate_id=UUID(int=identity),
        entity_kind=EntityKind.PLACE,
        names=(name,),
        phones=(phone,) if phone else (),
        emails=(email,) if email else (),
        website_urls=(website,) if website else (),
        addresses=(address,) if address else (),
        observation_ids=(UUID(int=1_000 + identity),),
        source_artifact_ids=(UUID(int=2_000 + identity),),
        source_keys=(f"source_{identity}",),
        geography=GeographyReference(
            coverage=coverage,
            market_area_revision="berlin-test-boundary@1",
            market_area_digest=_MARKET_DIGEST,
            latitude=Decimal("52.500000") if coverage is not GeographyCoverage.UNKNOWN else None,
            longitude=Decimal("13.400000") if coverage is not GeographyCoverage.UNKNOWN else None,
            observation_ids=(UUID(int=3_000 + identity),)
            if coverage is not GeographyCoverage.UNKNOWN
            else (),
        ),
    )


def _batch(
    candidates: tuple[ResolutionCandidate, ...],
    *,
    decisions: tuple[object, ...] = (),
    prior_clusters: tuple[object, ...] = (),
    pair_limit: int = 100,
    website_features: int = 2,
    address_features: int = 2,
):
    return seal_resolution_batch(
        ResolutionBatchPayload(
            batch_id=UUID(int=9_000 + len(candidates)),
            market_area_revision="berlin-test-boundary@1",
            market_area_digest=_MARKET_DIGEST,
            market_area_readiness=MarketAreaReadiness.READY,
            candidates=tuple(sorted(candidates, key=lambda item: item.candidate_id.hex)),
            manual_decisions=tuple(decisions),
            prior_clusters=tuple(prior_clusters),
            thresholds=ResolutionThresholds(
                candidate_limit=100,
                pair_limit=pair_limit,
                name_review_minimum_bps=7_000,
                address_review_minimum_bps=8_000,
                website_auto_match_minimum_strong_features=website_features,
                address_auto_match_minimum_strong_features=address_features,
                fuzzy_primary_area_review_reason_code=("FUZZY_BERLIN_MATCH_REQUIRES_REVIEW"),
            ),
            quality_rules=(
                ClusterQualityRule(
                    entity_kind=EntityKind.PLACE,
                    required_fields=(CandidateField.NAMES,),
                    single_value_fields=(),
                    minimum_distinct_source_count=1,
                    allowed_geography=(
                        GeographyCoverage.BOUNDARY,
                        GeographyCoverage.INSIDE,
                    ),
                    boundary_requires_review=False,
                    pending_review_blocks_export=True,
                ),
            ),
        )
    )


def _decision(identity: int, left: int, right: int, action: ManualDecisionAction):
    pair = tuple(sorted((UUID(int=left), UUID(int=right)), key=lambda value: value.hex))
    return seal_manual_decision(
        ManualResolutionDecisionPayload(
            decision_id=UUID(int=identity),
            left_candidate_id=pair[0],
            right_candidate_id=pair[1],
            action=action,
            revision=1,
            actor_reference="reviewer-test",
            reason_code="TEST_DECISION",
        )
    )


def test_name_only_pair_never_auto_merges_and_fuzzy_berlin_requires_review() -> None:
    batch = _batch(
        (
            _candidate(1, name="berlin audio studio"),
            _candidate(2, name="berlin audio studios"),
        )
    )

    result = resolve_entities(batch)

    assert len(result.pair_resolutions) == 1
    pair = result.pair_resolutions[0]
    assert pair.features.name_only is True
    assert pair.disposition is MatchDisposition.REVIEW_REQUIRED
    assert pair.reason_codes == ("FUZZY_BERLIN_MATCH_REQUIRES_REVIEW",)
    assert all(len(cluster.member_candidate_ids) == 1 for cluster in result.clusters)


def test_exact_phone_and_email_are_strong_deterministic_matches() -> None:
    phone_result = resolve_entities(
        _batch(
            (
                _candidate(1, name="alpha", phone="+49301111111"),
                _candidate(2, name="beta", phone="+49301111111"),
            )
        )
    )
    email_result = resolve_entities(
        _batch(
            (
                _candidate(3, name="gamma", email="hello@example.test"),
                _candidate(4, name="delta", email="hello@example.test"),
            )
        )
    )

    assert phone_result.pair_resolutions[0].disposition is MatchDisposition.AUTO_MATCH
    assert email_result.pair_resolutions[0].disposition is MatchDisposition.AUTO_MATCH
    assert len(phone_result.clusters[0].member_candidate_ids) == 2
    assert len(email_result.clusters[0].member_candidate_ids) == 2


def test_website_and_address_require_policy_corroboration() -> None:
    website_only = resolve_entities(
        _batch(
            (
                _candidate(1, name="alpha", website="https://shared.example/"),
                _candidate(2, name="beta", website="https://shared.example/contact"),
            ),
            website_features=2,
        )
    )
    website_with_address = resolve_entities(
        _batch(
            (
                _candidate(
                    3,
                    name="gamma",
                    website="https://shared.example/",
                    address="hauptstrasse 1 berlin",
                ),
                _candidate(
                    4,
                    name="delta",
                    website="https://shared.example/contact",
                    address="hauptstrasse 1 berlin",
                ),
            ),
            website_features=2,
        )
    )

    assert website_only.pair_resolutions[0].disposition is MatchDisposition.REVIEW_REQUIRED
    assert website_with_address.pair_resolutions[0].disposition is MatchDisposition.AUTO_MATCH


def test_explicit_separation_blocks_transitive_join() -> None:
    candidates = (
        _candidate(1, name="alpha", phone="+49301111111"),
        _candidate(2, name="beta", phone="+49301111111", email="team@example.test"),
        _candidate(3, name="gamma", email="team@example.test"),
    )
    result = resolve_entities(
        _batch(
            candidates,
            decisions=(_decision(50, 1, 3, ManualDecisionAction.SEPARATE),),
        )
    )

    memberships = {tuple(item.member_candidate_ids) for item in result.clusters}
    assert (UUID(int=1), UUID(int=2)) in memberships
    assert (UUID(int=3),) in memberships
    assert len(result.blocked_match_edges) == 1


def test_split_lineage_and_reversal_restore_original_cluster_identity() -> None:
    candidates = (
        _candidate(1, name="alpha", phone="+49301111111"),
        _candidate(2, name="beta", phone="+49301111111", email="team@example.test"),
        _candidate(3, name="gamma", email="team@example.test"),
    )
    original_members = tuple(item.candidate_id for item in candidates)
    prior = seal_prior_cluster(
        PriorClusterPayload(
            cluster_id=deterministic_cluster_id(original_members),
            member_candidate_ids=original_members,
        )
    )
    split = resolve_entities(
        _batch(
            candidates,
            decisions=(_decision(50, 1, 3, ManualDecisionAction.SEPARATE),),
            prior_clusters=(prior,),
        )
    )
    restored = resolve_entities(_batch(candidates, prior_clusters=(prior,)))

    assert {item.lineage_kind for item in split.clusters} == {ClusterLineageKind.SPLIT}
    assert len(restored.clusters) == 1
    assert restored.clusters[0].cluster_id == prior.cluster_id
    assert restored.clusters[0].lineage_kind is ClusterLineageKind.UNCHANGED


def test_pair_limit_fails_instead_of_dropping_pairs() -> None:
    candidates = tuple(_candidate(index, name="shared studio") for index in range(1, 5))
    with pytest.raises(ResolutionError) as failure:
        resolve_entities(_batch(candidates, pair_limit=2))
    assert failure.value.code == "RESOLUTION_PAIR_LIMIT_EXCEEDED"


def test_manual_match_is_explicit_and_blocked_market_area_cannot_run() -> None:
    candidates = (
        _candidate(1, name="manual one"),
        _candidate(2, name="manual two"),
    )
    manual = resolve_entities(
        _batch(
            candidates,
            decisions=(_decision(60, 1, 2, ManualDecisionAction.MATCH),),
        )
    )
    assert manual.pair_resolutions[0].disposition is MatchDisposition.MANUAL_MATCH

    ready = _batch(candidates)
    blocked = seal_resolution_batch(
        ResolutionBatchPayload.model_validate(
            {
                **ready.model_dump(mode="python", exclude={"batch_digest"}),
                "market_area_readiness": MarketAreaReadiness.BLOCKED,
            }
        )
    )
    with pytest.raises(ResolutionError) as failure:
        resolve_entities(blocked)
    assert failure.value.code == "RESOLUTION_MARKET_AREA_BLOCKED"
