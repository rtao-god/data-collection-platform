from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations
from urllib.parse import urlsplit
from uuid import UUID

from resolution_contracts import (
    BlockedMatchEdge,
    ClusterLineageKind,
    GeographyCoverage,
    ManualDecisionAction,
    ManualResolutionDecision,
    MatchDisposition,
    PairFeatures,
    PairResolution,
    PriorCluster,
    ResolutionBatch,
    ResolutionCandidate,
    ResolvedCluster,
    candidate_pair_id,
    canonical_digest,
    deterministic_cluster_id,
    verify_resolution_batch,
)

_TOKEN = re.compile(r"[a-z0-9]+")


class ResolutionError(ValueError):
    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ResolutionCoreResult:
    pair_resolutions: tuple[PairResolution, ...]
    blocked_match_edges: tuple[BlockedMatchEdge, ...]
    clusters: tuple[ResolvedCluster, ...]
    pending_review_pair_ids: tuple[str, ...]


def resolve_entities(batch: ResolutionBatch) -> ResolutionCoreResult:
    verify_resolution_batch(batch)
    if batch.market_area_readiness.value != "ready":
        raise ResolutionError(
            code="RESOLUTION_MARKET_AREA_BLOCKED",
            message="Entity resolution requires a ready exact market-area revision.",
        )
    candidates = {item.candidate_id: item for item in batch.candidates}
    pairs = _generate_pairs(batch)
    decisions = {
        (item.left_candidate_id, item.right_candidate_id): item for item in batch.manual_decisions
    }
    pair_resolutions = tuple(
        sorted(
            (
                _resolve_pair(
                    candidates[left],
                    candidates[right],
                    batch,
                    decisions.get((left, right)),
                )
                for left, right in pairs
            ),
            key=lambda item: item.features.pair_id,
        )
    )
    blocked_edges, clusters = _build_clusters(batch, pair_resolutions)
    pending_review_pair_ids = tuple(
        item.features.pair_id
        for item in pair_resolutions
        if item.disposition is MatchDisposition.REVIEW_REQUIRED
    )
    return ResolutionCoreResult(
        pair_resolutions=pair_resolutions,
        blocked_match_edges=blocked_edges,
        clusters=clusters,
        pending_review_pair_ids=pending_review_pair_ids,
    )


def _generate_pairs(batch: ResolutionBatch) -> tuple[tuple[UUID, UUID], ...]:
    buckets: dict[str, list[UUID]] = defaultdict(list)
    for candidate in batch.candidates:
        for value in candidate.phones:
            buckets[f"phone:{value}"].append(candidate.candidate_id)
        for value in candidate.emails:
            buckets[f"email:{value}"].append(candidate.candidate_id)
        for value in _website_hosts(candidate):
            buckets[f"website:{value}"].append(candidate.candidate_id)
        for value in candidate.names:
            for key in _text_block_keys("name", candidate.entity_kind.value, value):
                buckets[key].append(candidate.candidate_id)
        for value in candidate.addresses:
            for key in _text_block_keys("address", candidate.entity_kind.value, value):
                buckets[key].append(candidate.candidate_id)

    generated: set[tuple[UUID, UUID]] = set()
    for identities in buckets.values():
        ordered = tuple(sorted(set(identities), key=lambda value: value.hex))
        for left, right in combinations(ordered, 2):
            generated.add((left, right))
            if len(generated) > batch.thresholds.pair_limit:
                raise ResolutionError(
                    code="RESOLUTION_PAIR_LIMIT_EXCEEDED",
                    message="Generated candidate pairs exceed the declared batch limit.",
                )
    for decision in batch.manual_decisions:
        generated.add((decision.left_candidate_id, decision.right_candidate_id))
        if len(generated) > batch.thresholds.pair_limit:
            raise ResolutionError(
                code="RESOLUTION_PAIR_LIMIT_EXCEEDED",
                message="Manual decisions exceed the declared candidate-pair limit.",
            )
    return tuple(sorted(generated, key=lambda item: (item[0].hex, item[1].hex)))


def _text_block_keys(prefix: str, entity_kind: str, value: str) -> tuple[str, ...]:
    tokens = tuple(sorted(set(_TOKEN.findall(value))))
    if not tokens:
        return ()
    keys = {f"{prefix}:{entity_kind}:exact:{' '.join(tokens)}"}
    for token in tokens:
        if len(token) >= 4:
            keys.add(f"{prefix}:{entity_kind}:prefix:{token[:4]}")
    return tuple(sorted(keys))


def _resolve_pair(
    left: ResolutionCandidate,
    right: ResolutionCandidate,
    batch: ResolutionBatch,
    decision: ManualResolutionDecision | None,
) -> PairResolution:
    pair_id = candidate_pair_id(left.candidate_id, right.candidate_id)
    exact_phone = bool(set(left.phones).intersection(right.phones))
    exact_email = bool(set(left.emails).intersection(right.emails))
    exact_website = bool(set(_website_hosts(left)).intersection(_website_hosts(right)))
    name_similarity = _maximum_similarity(left.names, right.names)
    address_similarity = _maximum_similarity(left.addresses, right.addresses)
    kind_compatible = left.entity_kind is right.entity_kind
    geography_compatible = _geography_compatible(left, right)
    source_overlap_count = len(set(left.source_keys).intersection(right.source_keys))
    exact_address = address_similarity == 10_000 and bool(left.addresses and right.addresses)
    strong_non_name_count = sum((exact_phone, exact_email, exact_website, exact_address))
    name_only = (
        name_similarity > 0
        and not exact_phone
        and not exact_email
        and not exact_website
        and address_similarity == 0
    )
    features = PairFeatures(
        pair_id=pair_id,
        left_candidate_id=left.candidate_id,
        right_candidate_id=right.candidate_id,
        exact_phone_overlap=exact_phone,
        exact_email_overlap=exact_email,
        exact_website_host_overlap=exact_website,
        name_similarity_bps=name_similarity,
        address_similarity_bps=address_similarity,
        entity_kind_compatible=kind_compatible,
        geography_compatible=geography_compatible,
        source_overlap_count=source_overlap_count,
        strong_non_name_feature_count=strong_non_name_count,
        name_only=name_only,
    )
    disposition, reasons = _disposition(features, left, right, batch, decision)
    evidence_strength = min(
        100_000,
        int(exact_phone) * 30_000
        + int(exact_email) * 30_000
        + int(exact_website) * 15_000
        + int(exact_address) * 15_000
        + name_similarity
        + address_similarity,
    )
    if disposition is MatchDisposition.MANUAL_MATCH:
        evidence_strength = 100_000
    return PairResolution(
        features=features,
        disposition=disposition,
        reason_codes=tuple(sorted(reasons)),
        evidence_strength=evidence_strength,
    )


def _disposition(
    features: PairFeatures,
    left: ResolutionCandidate,
    right: ResolutionCandidate,
    batch: ResolutionBatch,
    decision: ManualResolutionDecision | None,
) -> tuple[MatchDisposition, tuple[str, ...]]:
    action = getattr(decision, "action", None)
    if action is ManualDecisionAction.SEPARATE:
        return MatchDisposition.MANUAL_SEPARATE, ("MANUAL_SEPARATION",)
    if not features.entity_kind_compatible:
        return MatchDisposition.NO_MATCH, ("ENTITY_KIND_MISMATCH",)
    if action is ManualDecisionAction.MATCH:
        return MatchDisposition.MANUAL_MATCH, ("MANUAL_MATCH",)
    if features.geography_compatible and (
        features.exact_phone_overlap or features.exact_email_overlap
    ):
        reasons = []
        if features.exact_phone_overlap:
            reasons.append("EXACT_PHONE_MATCH")
        if features.exact_email_overlap:
            reasons.append("EXACT_EMAIL_MATCH")
        return MatchDisposition.AUTO_MATCH, tuple(reasons)
    if (
        features.geography_compatible
        and features.exact_website_host_overlap
        and features.strong_non_name_feature_count
        >= batch.thresholds.website_auto_match_minimum_strong_features
    ):
        return MatchDisposition.AUTO_MATCH, ("CORROBORATED_WEBSITE_MATCH",)
    if (
        features.geography_compatible
        and features.address_similarity_bps == 10_000
        and features.strong_non_name_feature_count
        >= batch.thresholds.address_auto_match_minimum_strong_features
    ):
        return MatchDisposition.AUTO_MATCH, ("CORROBORATED_ADDRESS_MATCH",)

    fuzzy = (
        features.name_similarity_bps >= batch.thresholds.name_review_minimum_bps
        or features.address_similarity_bps >= batch.thresholds.address_review_minimum_bps
    )
    if fuzzy and _is_primary_review_scope(left) and _is_primary_review_scope(right):
        return (
            MatchDisposition.REVIEW_REQUIRED,
            (batch.thresholds.fuzzy_primary_area_review_reason_code,),
        )
    if fuzzy or features.name_only or features.exact_website_host_overlap:
        return MatchDisposition.REVIEW_REQUIRED, ("AMBIGUOUS_MATCH_REQUIRES_REVIEW",)
    if not features.geography_compatible:
        return MatchDisposition.NO_MATCH, ("GEOGRAPHY_CONFLICT",)
    return MatchDisposition.NO_MATCH, ("INSUFFICIENT_MATCH_EVIDENCE",)


def _build_clusters(
    batch: ResolutionBatch,
    pair_resolutions: tuple[PairResolution, ...],
) -> tuple[tuple[BlockedMatchEdge, ...], tuple[ResolvedCluster, ...]]:
    candidate_ids = tuple(item.candidate_id for item in batch.candidates)
    union = _UnionFind(candidate_ids)
    separation_pairs = {
        (item.left_candidate_id, item.right_candidate_id): candidate_pair_id(
            item.left_candidate_id, item.right_candidate_id
        )
        for item in batch.manual_decisions
        if item.action is ManualDecisionAction.SEPARATE
    }
    accepted = [
        item
        for item in pair_resolutions
        if item.disposition in {MatchDisposition.MANUAL_MATCH, MatchDisposition.AUTO_MATCH}
    ]
    accepted.sort(
        key=lambda item: (
            0 if item.disposition is MatchDisposition.MANUAL_MATCH else 1,
            -item.evidence_strength,
            item.features.pair_id,
        )
    )
    blocked: list[BlockedMatchEdge] = []
    for edge in accepted:
        left = edge.features.left_candidate_id
        right = edge.features.right_candidate_id
        left_members = union.members(left)
        right_members = union.members(right)
        if left_members == right_members:
            continue
        conflict = _separation_between(left_members, right_members, separation_pairs)
        if conflict is not None:
            blocked.append(
                BlockedMatchEdge(
                    blocked_edge_id=canonical_digest(
                        [edge.features.pair_id, conflict[1], "transitive-separation"]
                    ),
                    attempted_pair_id=edge.features.pair_id,
                    separation_pair_id=conflict[1],
                    reason_code="EXPLICIT_SEPARATION_TRANSITIVE_BLOCK",
                )
            )
            continue
        union.join(left, right)

    blocked_tuple = tuple(sorted(blocked, key=lambda item: item.blocked_edge_id))
    components = tuple(
        sorted(
            (tuple(sorted(values, key=lambda value: value.hex)) for values in union.components()),
            key=lambda values: values[0].hex,
        )
    )
    prior_by_id = {item.cluster_id: item for item in batch.prior_clusters}
    clusters: list[ResolvedCluster] = []
    for members in components:
        member_set = set(members)
        parents = tuple(
            sorted(
                (
                    parent
                    for parent in batch.prior_clusters
                    if member_set.intersection(parent.member_candidate_ids)
                ),
                key=lambda item: item.cluster_id.hex,
            )
        )
        parent_ids = tuple(item.cluster_id for item in parents)
        lineage = _lineage_kind(member_set, parents)
        parent_membership_digest = canonical_digest(
            [
                {
                    "clusterId": str(parent.cluster_id),
                    "memberCandidateIds": [str(value) for value in parent.member_candidate_ids],
                    "contentDigest": prior_by_id[parent.cluster_id].content_digest,
                }
                for parent in parents
            ]
        )
        affecting_blocked = tuple(
            item.blocked_edge_id
            for item in blocked_tuple
            if _pair_touches_members(item.attempted_pair_id, pair_resolutions, member_set)
        )
        clusters.append(
            ResolvedCluster(
                cluster_id=deterministic_cluster_id(members),
                member_candidate_ids=members,
                parent_cluster_ids=parent_ids,
                lineage_kind=lineage,
                parent_membership_digest=parent_membership_digest,
                blocked_match_edge_ids=affecting_blocked,
            )
        )
    clusters.sort(key=lambda item: item.cluster_id.hex)
    return blocked_tuple, tuple(clusters)


def _lineage_kind(
    members: set[UUID],
    parents: tuple[PriorCluster, ...],
) -> ClusterLineageKind:
    if not parents:
        return ClusterLineageKind.NEW
    parent_sets = [set(item.member_candidate_ids) for item in parents]
    if len(parent_sets) == 1 and parent_sets[0] == members:
        return ClusterLineageKind.UNCHANGED
    if len(parent_sets) == 1 and members < parent_sets[0]:
        return ClusterLineageKind.SPLIT
    parent_union = set().union(*parent_sets)
    if len(parent_sets) > 1 and parent_union == members:
        return ClusterLineageKind.MERGE
    return ClusterLineageKind.RECOMBINED


def _separation_between(
    left_members: frozenset[UUID],
    right_members: frozenset[UUID],
    separation_pairs: dict[tuple[UUID, UUID], str],
) -> tuple[tuple[UUID, UUID], str] | None:
    conflicts: list[tuple[tuple[UUID, UUID], str]] = []
    for left in left_members:
        for right in right_members:
            pair = tuple(sorted((left, right), key=lambda value: value.hex))
            pair_key = (pair[0], pair[1])
            if pair_key in separation_pairs:
                conflicts.append((pair_key, separation_pairs[pair_key]))
    return min(conflicts, key=lambda item: item[1]) if conflicts else None


def _pair_touches_members(
    pair_id: str,
    pair_resolutions: tuple[PairResolution, ...],
    members: set[UUID],
) -> bool:
    pair = next(item.features for item in pair_resolutions if item.features.pair_id == pair_id)
    return pair.left_candidate_id in members or pair.right_candidate_id in members


def _website_hosts(candidate: ResolutionCandidate) -> tuple[str, ...]:
    return tuple(sorted({urlsplit(value).hostname or "" for value in candidate.website_urls}))


def _maximum_similarity(left: Sequence[str], right: Sequence[str]) -> int:
    if not left or not right:
        return 0
    return max(_similarity_bps(a, b) for a in left for b in right)


def _similarity_bps(left: str, right: str) -> int:
    if left == right:
        return 10_000
    maximum = max(len(left), len(right))
    if maximum == 0:
        return 10_000
    distance = _levenshtein_distance(left, right)
    return max(0, (maximum - distance) * 10_000 // maximum)


def _levenshtein_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def _geography_compatible(left: ResolutionCandidate, right: ResolutionCandidate) -> bool:
    inside = {GeographyCoverage.INSIDE, GeographyCoverage.BOUNDARY}
    coverages = {left.geography.coverage, right.geography.coverage}
    return not (GeographyCoverage.OUTSIDE in coverages and bool(coverages.intersection(inside)))


def _is_primary_review_scope(candidate: ResolutionCandidate) -> bool:
    return candidate.geography.coverage in {
        GeographyCoverage.INSIDE,
        GeographyCoverage.BOUNDARY,
    }


class _UnionFind:
    def __init__(self, identities: Iterable[UUID]) -> None:
        self._parent = {identity: identity for identity in identities}
        self._members = {identity: {identity} for identity in identities}

    def find(self, identity: UUID) -> UUID:
        parent = self._parent[identity]
        if parent != identity:
            self._parent[identity] = self.find(parent)
        return self._parent[identity]

    def join(self, left: UUID, right: UUID) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        winner, loser = sorted((left_root, right_root), key=lambda value: value.hex)
        self._parent[loser] = winner
        self._members[winner].update(self._members.pop(loser))

    def members(self, identity: UUID) -> frozenset[UUID]:
        return frozenset(self._members[self.find(identity)])

    def components(self) -> tuple[frozenset[UUID], ...]:
        return tuple(frozenset(values) for values in self._members.values())
