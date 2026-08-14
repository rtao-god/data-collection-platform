from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from resolution_contracts import (
    CandidateField,
    ClusterQualityRule,
    EntityKind,
    GeographyCoverage,
    GeographyReference,
    MarketAreaReadiness,
    PairFeatures,
    PriorClusterPayload,
    ResolutionBatchPayload,
    ResolutionCandidate,
    ResolutionSnapshot,
    ResolutionThresholds,
    candidate_pair_id,
    canonical_resolution_batch_json,
    decode_resolution_batch,
    deterministic_cluster_id,
    seal_resolution_batch,
)

_MARKET_DIGEST = f"sha256:{'a' * 64}"


def _candidate(identity: int) -> ResolutionCandidate:
    return ResolutionCandidate(
        candidate_id=UUID(int=identity),
        entity_kind=EntityKind.PLACE,
        names=(f"studio {identity}",),
        phones=(f"+49300000{identity:03d}",),
        emails=(f"contact{identity}@example.test",),
        website_urls=(f"https://studio{identity}.example/",),
        addresses=(f"street {identity} berlin",),
        observation_ids=(UUID(int=100 + identity),),
        source_artifact_ids=(UUID(int=200 + identity),),
        source_keys=(f"source_{identity}",),
        geography=GeographyReference(
            coverage=GeographyCoverage.INSIDE,
            market_area_revision="berlin-test-boundary@1",
            market_area_digest=_MARKET_DIGEST,
            latitude=Decimal("52.500000"),
            longitude=Decimal("13.400000"),
            observation_ids=(UUID(int=300 + identity),),
        ),
    )


def _batch_payload() -> ResolutionBatchPayload:
    candidates = tuple(
        sorted((_candidate(1), _candidate(2)), key=lambda item: item.candidate_id.hex)
    )
    return ResolutionBatchPayload(
        batch_id=UUID(int=500),
        market_area_revision="berlin-test-boundary@1",
        market_area_digest=_MARKET_DIGEST,
        market_area_readiness=MarketAreaReadiness.READY,
        candidates=candidates,
        thresholds=ResolutionThresholds(
            candidate_limit=10,
            pair_limit=100,
            name_review_minimum_bps=7_000,
            address_review_minimum_bps=8_000,
            website_auto_match_minimum_strong_features=2,
            address_auto_match_minimum_strong_features=2,
            fuzzy_primary_area_review_reason_code="FUZZY_BERLIN_MATCH_REQUIRES_REVIEW",
        ),
        quality_rules=(
            ClusterQualityRule(
                entity_kind=EntityKind.PLACE,
                required_fields=(CandidateField.NAMES,),
                single_value_fields=(CandidateField.WEBSITE_URLS,),
                minimum_distinct_source_count=1,
                allowed_geography=(GeographyCoverage.BOUNDARY, GeographyCoverage.INSIDE),
                boundary_requires_review=False,
                pending_review_blocks_export=True,
            ),
        ),
    )


def test_resolution_batch_round_trips_only_as_canonical_bytes() -> None:
    batch = seal_resolution_batch(_batch_payload())
    content = canonical_resolution_batch_json(batch).encode()

    assert decode_resolution_batch(content) == batch
    with pytest.raises(ValueError, match="not canonical"):
        decode_resolution_batch(b" " + content)


def test_resolution_batch_digest_detects_mutation() -> None:
    batch = seal_resolution_batch(_batch_payload())
    document = batch.model_dump(mode="json", by_alias=True)
    document["marketAreaRevision"] = "different-market@1"

    with pytest.raises((ValidationError, ValueError), match=r"geography|digest"):
        decode_resolution_batch(
            __import__("json").dumps(document, separators=(",", ":"), sort_keys=True).encode()
            + b"\n"
        )


def test_candidate_collections_must_be_canonical() -> None:
    with pytest.raises(ValidationError, match="sorted and unique"):
        ResolutionCandidate(
            **{
                **_candidate(1).model_dump(mode="python"),
                "names": ("z studio", "a studio"),
            }
        )


def test_unknown_geography_cannot_carry_invented_point() -> None:
    with pytest.raises(ValidationError, match="must not invent"):
        GeographyReference(
            coverage=GeographyCoverage.UNKNOWN,
            market_area_revision="berlin-test-boundary@1",
            market_area_digest=_MARKET_DIGEST,
            latitude=Decimal("52.5"),
            longitude=Decimal("13.4"),
        )


def test_pair_identity_is_owned_by_the_wire_contract() -> None:
    left = UUID(int=1)
    right = UUID(int=2)
    canonical = PairFeatures(
        pair_id=candidate_pair_id(left, right),
        left_candidate_id=left,
        right_candidate_id=right,
        exact_phone_overlap=False,
        exact_email_overlap=False,
        exact_website_host_overlap=False,
        name_similarity_bps=5_000,
        address_similarity_bps=0,
        entity_kind_compatible=True,
        geography_compatible=True,
        source_overlap_count=0,
        strong_non_name_feature_count=0,
        name_only=True,
    )

    with pytest.raises(ValidationError, match="pair ID does not match"):
        PairFeatures.model_validate(
            {
                **canonical.model_dump(mode="python"),
                "pair_id": f"sha256:{'0' * 64}",
            }
        )

    with pytest.raises(ValidationError, match="name-only marker does not match"):
        PairFeatures.model_validate(
            {
                **canonical.model_dump(mode="python"),
                "name_only": False,
            }
        )


def test_cluster_identity_is_owned_by_deterministic_membership() -> None:
    members = (UUID(int=1), UUID(int=2))
    cluster = PriorClusterPayload(
        cluster_id=deterministic_cluster_id(members),
        member_candidate_ids=members,
    )
    assert cluster.cluster_id == deterministic_cluster_id(members)

    with pytest.raises(ValidationError, match="cluster ID does not match"):
        PriorClusterPayload(
            cluster_id=UUID(int=999),
            member_candidate_ids=members,
        )


def test_snapshot_references_exact_review_dispositions() -> None:
    root = Path(__file__).parents[3]
    document = json.loads(
        (root / "datasets/entity_resolution/golden-snapshot.json").read_text(encoding="utf-8")
    )
    assert document["pendingReviewPairIds"]
    document["pendingReviewPairIds"] = []

    with pytest.raises(ValidationError, match="exactly cover review dispositions"):
        ResolutionSnapshot.model_validate(document)
