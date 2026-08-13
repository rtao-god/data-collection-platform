from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(content).lstrip(), encoding="utf-8")


def transformed(path: str, replacements: tuple[tuple[str, str], ...]) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    for old, new in replacements:
        text = text.replace(old, new)
    return text


write(
    "packages/entity_resolution_core/src/entity_resolution_core/engine.py",
    r'''
    from __future__ import annotations

    import unicodedata
    from collections import defaultdict
    from itertools import combinations
    from urllib.parse import urlsplit
    from uuid import UUID

    from resolution_contracts import (
        CandidateRecord,
        ClusterLineage,
        EntityResolutionBatch,
        PairFeatures,
        PairResolution,
        PriorCluster,
        ResolutionDecision,
        ResolutionDiagnostic,
        ResolutionGraph,
        canonical_pair,
        deterministic_cluster_id,
        deterministic_pair_id,
        membership_digest,
    )


    class EntityResolutionEngine:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def resolve(self, batch: EntityResolutionBatch) -> ResolutionGraph:
            candidates = {item.candidate_id: item for item in batch.candidates}
            decisions = {
                (item.left_candidate_id, item.right_candidate_id): item
                for item in batch.decisions
            }
            pairs = tuple(
                _resolve_pair(
                    candidates[left],
                    candidates[right],
                    decision=decisions.get((left, right)),
                    batch=batch,
                )
                for left, right in _candidate_pairs(batch)
            )
            pairs = tuple(sorted(pairs, key=lambda item: item.pair_id))
            clusters, blocked, diagnostics = _cluster(batch, pairs)
            pending = tuple(
                sorted(
                    item.pair_id
                    for item in pairs
                    if item.disposition == "review_required"
                )
            )
            return ResolutionGraph(
                pairs=pairs,
                blockedMatchPairIds=blocked,
                clusters=clusters,
                pendingReviewPairIds=pending,
                diagnostics=diagnostics,
            )


    def _candidate_pairs(batch: EntityResolutionBatch) -> tuple[tuple[UUID, UUID], ...]:
        blocks: dict[str, list[UUID]] = defaultdict(list)
        for candidate in batch.candidates:
            for phone in candidate.phones:
                blocks[f"phone:{phone}"].append(candidate.candidate_id)
            for email in candidate.emails:
                blocks[f"email:{email.casefold()}"].append(candidate.candidate_id)
            for host in _website_hosts(candidate):
                blocks[f"website:{host}"].append(candidate.candidate_id)
            for name in candidate.names:
                normalized = _normalize_text(name)
                tokens = normalized.split()
                if tokens:
                    blocks[
                        f"name:{candidate.entity_kind}:{tokens[0][:24]}"
                    ].append(candidate.candidate_id)
            for address in candidate.addresses:
                tokens = _normalize_text(address).split()
                if tokens:
                    signature = "|".join(tokens[:2])
                    blocks[
                        f"address:{candidate.entity_kind}:{signature}"
                    ].append(candidate.candidate_id)

        pairs: set[tuple[UUID, UUID]] = set()
        maximum = batch.policy.thresholds.maximum_pairs
        for members in blocks.values():
            unique = sorted(set(members))
            for left, right in combinations(unique, 2):
                pairs.add((left, right))
                if len(pairs) > maximum:
                    raise ValueError("resolution pair count exceeds the configured limit")
        for decision in batch.decisions:
            pairs.add((decision.left_candidate_id, decision.right_candidate_id))
            if len(pairs) > maximum:
                raise ValueError("resolution pair count exceeds the configured limit")
        return tuple(sorted(pairs))


    def _resolve_pair(
        left: CandidateRecord,
        right: CandidateRecord,
        *,
        decision: ResolutionDecision | None,
        batch: EntityResolutionBatch,
    ) -> PairResolution:
        phone = bool(set(left.phones) & set(right.phones))
        email = bool({item.casefold() for item in left.emails} & {item.casefold() for item in right.emails})
        website = bool(_website_hosts(left) & _website_hosts(right))
        exact_address = bool(
            {_normalize_text(item) for item in left.addresses}
            & {_normalize_text(item) for item in right.addresses}
        )
        name_similarity = _maximum_similarity(left.names, right.names)
        address_similarity = _maximum_similarity(left.addresses, right.addresses)
        compatible_kind = left.entity_kind == right.entity_kind
        both_market_covered = (
            left.geography.coverage in {"inside", "boundary"}
            and right.geography.coverage in {"inside", "boundary"}
        )
        shared_source = bool(set(left.source_keys) & set(right.source_keys))
        strong_count = sum((phone, email, website, exact_address))
        name_only = name_similarity > 0 and strong_count == 0 and address_similarity == 0
        features = PairFeatures(
            exactPhone=phone,
            exactEmail=email,
            exactWebsiteHost=website,
            exactAddress=exact_address,
            nameSimilarityBasisPoints=name_similarity,
            addressSimilarityBasisPoints=address_similarity,
            compatibleEntityKind=compatible_kind,
            bothMarketCovered=both_market_covered,
            sharedSource=shared_source,
            strongNonNameFeatureCount=strong_count,
            nameOnly=name_only,
        )
        disposition, reasons = _disposition(
            features,
            decision=decision,
            thresholds=batch.policy.thresholds,
        )
        return PairResolution(
            pairId=deterministic_pair_id(left.candidate_id, right.candidate_id),
            leftCandidateId=left.candidate_id,
            rightCandidateId=right.candidate_id,
            disposition=disposition,
            reasonCodes=reasons,
            features=features,
        )


    def _disposition(
        features: PairFeatures,
        *,
        decision: ResolutionDecision | None,
        thresholds: object,
    ) -> tuple[str, tuple[str, ...]]:
        values = thresholds
        if decision is not None and decision.action == "separate":
            return "manual_separate", ("MANUAL_SEPARATION", decision.reason_code)
        if not features.compatible_entity_kind:
            return "no_match", ("ENTITY_KIND_MISMATCH",)
        if decision is not None and decision.action == "match":
            return "manual_match", ("MANUAL_MATCH", decision.reason_code)
        if features.exact_phone:
            return "auto_match", ("EXACT_PHONE",)
        if features.exact_email:
            return "auto_match", ("EXACT_EMAIL",)
        if features.exact_website_host and (
            features.name_similarity_basis_points
            >= values.website_name_auto_basis_points
            or features.address_similarity_basis_points
            >= values.website_address_auto_basis_points
        ):
            return "auto_match", ("WEBSITE_WITH_CORROBORATION",)
        if features.exact_address and (
            features.name_similarity_basis_points
            >= values.address_name_auto_basis_points
        ):
            return "auto_match", ("ADDRESS_WITH_NAME_CORROBORATION",)

        review = (
            features.name_similarity_basis_points >= values.name_review_basis_points
            or features.address_similarity_basis_points
            >= values.address_review_basis_points
            or features.exact_website_host
            or features.exact_address
        )
        if not review:
            return "no_match", ("INSUFFICIENT_MATCH_EVIDENCE",)
        reasons = {"FUZZY_MATCH_REQUIRES_REVIEW"}
        if features.name_only:
            reasons.add("NAME_ONLY_REQUIRES_REVIEW")
        if features.exact_website_host:
            reasons.add("WEBSITE_REQUIRES_CORROBORATION")
        if features.exact_address:
            reasons.add("ADDRESS_REQUIRES_CORROBORATION")
        if features.both_market_covered:
            reasons.add("FUZZY_BERLIN_MATCH_REQUIRES_REVIEW")
        return "review_required", tuple(sorted(reasons))


    def _cluster(
        batch: EntityResolutionBatch,
        pairs: tuple[PairResolution, ...],
    ) -> tuple[
        tuple[ClusterLineage, ...],
        tuple[UUID, ...],
        tuple[ResolutionDiagnostic, ...],
    ]:
        candidate_ids = tuple(item.candidate_id for item in batch.candidates)
        disjoint = _DisjointSet(candidate_ids)
        separations = {
            (item.left_candidate_id, item.right_candidate_id)
            for item in pairs
            if item.disposition == "manual_separate"
        }
        separated_by: dict[UUID, set[UUID]] = defaultdict(set)
        for left, right in separations:
            separated_by[left].add(right)
            separated_by[right].add(left)

        match_edges = [
            item
            for item in pairs
            if item.disposition in {"manual_match", "auto_match"}
        ]
        match_edges.sort(key=_match_priority)
        blocked: set[UUID] = set()
        diagnostics: list[ResolutionDiagnostic] = []
        pair_by_id = {item.pair_id: item for item in pairs}
        for edge in match_edges:
            left_root = disjoint.find(edge.left_candidate_id)
            right_root = disjoint.find(edge.right_candidate_id)
            if left_root == right_root:
                continue
            if _would_violate_separation(
                disjoint.members[left_root],
                disjoint.members[right_root],
                separated_by,
            ):
                blocked.add(edge.pair_id)
                diagnostics.append(
                    ResolutionDiagnostic(
                        code="MATCH_EDGE_BLOCKED_BY_SEPARATION",
                        pairId=edge.pair_id,
                    )
                )
                continue
            disjoint.union(left_root, right_root)

        memberships = tuple(
            sorted(
                (tuple(sorted(members)) for members in disjoint.components()),
                key=lambda items: deterministic_cluster_id(items),
            )
        )
        prior_by_id = {item.cluster_id: item for item in batch.prior_clusters}
        parent_sets: dict[UUID, tuple[UUID, ...]] = {}
        children_per_parent: dict[UUID, int] = defaultdict(int)
        for members in memberships:
            cluster_id = deterministic_cluster_id(members)
            parents = tuple(
                sorted(
                    cluster.cluster_id
                    for cluster in batch.prior_clusters
                    if set(cluster.member_candidate_ids) & set(members)
                )
            )
            parent_sets[cluster_id] = parents
            for parent in parents:
                children_per_parent[parent] += 1

        clusters: list[ClusterLineage] = []
        for members in memberships:
            cluster_id = deterministic_cluster_id(members)
            parents = parent_sets[cluster_id]
            lineage = _lineage_kind(
                members,
                parents=parents,
                prior_by_id=prior_by_id,
                children_per_parent=children_per_parent,
            )
            parent_clusters = tuple(prior_by_id[parent] for parent in parents)
            affecting_blocked = tuple(
                sorted(
                    pair_id
                    for pair_id in blocked
                    if (
                        pair_by_id[pair_id].left_candidate_id in members
                        or pair_by_id[pair_id].right_candidate_id in members
                    )
                )
            )
            clusters.append(
                ClusterLineage(
                    clusterId=cluster_id,
                    memberCandidateIds=members,
                    parentClusterIds=parents,
                    lineageKind=lineage,
                    parentMembersDigest=membership_digest(parent_clusters),
                    blockedMatchPairIds=affecting_blocked,
                )
            )
        clusters.sort(key=lambda item: item.cluster_id)
        diagnostics.sort(
            key=lambda item: (
                item.code,
                str(item.pair_id or ""),
                str(item.cluster_id or ""),
            )
        )
        return tuple(clusters), tuple(sorted(blocked)), tuple(diagnostics)


    def _match_priority(item: PairResolution) -> tuple[int, int, UUID]:
        manual_priority = 0 if item.disposition == "manual_match" else 1
        strength = (
            item.features.strong_non_name_feature_count * 10000
            + item.features.name_similarity_basis_points
            + item.features.address_similarity_basis_points
        )
        return (manual_priority, -strength, item.pair_id)


    def _would_violate_separation(
        left_members: set[UUID],
        right_members: set[UUID],
        separated_by: dict[UUID, set[UUID]],
    ) -> bool:
        return any(
            separated_by.get(member, set()) & right_members
            for member in left_members
        )


    def _lineage_kind(
        members: tuple[UUID, ...],
        *,
        parents: tuple[UUID, ...],
        prior_by_id: dict[UUID, PriorCluster],
        children_per_parent: dict[UUID, int],
    ) -> str:
        if not parents:
            return "new"
        if len(parents) > 1:
            return "merge"
        parent = prior_by_id[parents[0]]
        if parent.member_candidate_ids == members:
            return "unchanged"
        if children_per_parent[parent.cluster_id] > 1:
            return "split"
        return "recombined"


    def _website_hosts(candidate: CandidateRecord) -> set[str]:
        hosts: set[str] = set()
        for value in candidate.websites:
            try:
                host = urlsplit(value).hostname
            except ValueError:
                host = None
            if host:
                hosts.add(host.rstrip(".").casefold())
        return hosts


    def _maximum_similarity(left: tuple[str, ...], right: tuple[str, ...]) -> int:
        return max(
            (
                _similarity(first, second)
                for first in left
                for second in right
            ),
            default=0,
        )


    def _similarity(left: str, right: str) -> int:
        first = _normalize_text(left)
        second = _normalize_text(right)
        if not first or not second:
            return 0
        if first == second:
            return 10000
        first_tokens = set(first.split())
        second_tokens = set(second.split())
        token_union = first_tokens | second_tokens
        token_score = (
            len(first_tokens & second_tokens) / len(token_union)
            if token_union
            else 0.0
        )
        first_bigrams = _bigrams(first.replace(" ", ""))
        second_bigrams = _bigrams(second.replace(" ", ""))
        bigram_union = first_bigrams | second_bigrams
        bigram_score = (
            len(first_bigrams & second_bigrams) / len(bigram_union)
            if bigram_union
            else 0.0
        )
        return int(round(max(token_score, bigram_score) * 10000))


    def _normalize_text(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value).casefold()
        characters = [
            character
            if character.isalnum()
            else " "
            for character in decomposed
            if not unicodedata.combining(character)
        ]
        return " ".join("".join(characters).split())


    def _bigrams(value: str) -> set[str]:
        if len(value) < 2:
            return {value} if value else set()
        return {value[index : index + 2] for index in range(len(value) - 1)}


    class _DisjointSet:
        def __init__(self, values: tuple[UUID, ...]) -> None:
            self.parent = {value: value for value in values}
            self.members = {value: {value} for value in values}

        def find(self, value: UUID) -> UUID:
            parent = self.parent[value]
            if parent != value:
                self.parent[value] = self.find(parent)
            return self.parent[value]

        def union(self, left: UUID, right: UUID) -> UUID:
            first = self.find(left)
            second = self.find(right)
            if first == second:
                return first
            winner, loser = (first, second) if first < second else (second, first)
            self.parent[loser] = winner
            self.members[winner].update(self.members.pop(loser))
            return winner

        def components(self) -> tuple[set[UUID], ...]:
            return tuple(self.members[root] for root in sorted(self.members))
    ''',
)

write(
    "packages/entity_resolution_core/src/entity_resolution_core/__init__.py",
    r'''
    from entity_resolution_core.engine import EntityResolutionEngine

    __all__ = ["EntityResolutionEngine"]
    ''',
)

write(
    "packages/quality_core/src/quality_core/evaluator.py",
    r'''
    from __future__ import annotations

    from uuid import UUID

    from resolution_contracts import (
        CandidateRecord,
        ClusterQuality,
        EntityQualityRule,
        EntityResolutionBatch,
        FieldCoverage,
        ResolutionGraph,
    )


    class QualityEvaluator:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def evaluate(
            self,
            batch: EntityResolutionBatch,
            graph: ResolutionGraph,
        ) -> tuple[ClusterQuality, ...]:
            candidates = {item.candidate_id: item for item in batch.candidates}
            rules = {item.entity_kind: item for item in batch.policy.quality_rules}
            pair_by_id = {item.pair_id: item for item in graph.pairs}
            pending = set(graph.pending_review_pair_ids)
            qualities: list[ClusterQuality] = []
            for cluster in graph.clusters:
                members = tuple(candidates[item] for item in cluster.member_candidate_ids)
                entity_kinds = {item.entity_kind for item in members}
                rule = rules.get(next(iter(entity_kinds))) if len(entity_kinds) == 1 else None
                blockers: set[str] = set()
                warnings: set[str] = set()
                if len(entity_kinds) != 1:
                    blockers.add("MIXED_ENTITY_KINDS")
                if rule is None:
                    blockers.add("QUALITY_POLICY_MISSING")
                if any(
                    not item.observation_ids
                    or not item.source_artifact_ids
                    or not item.source_keys
                    for item in members
                ):
                    blockers.add("MISSING_OBSERVATION_PROVENANCE")
                if rule is not None:
                    _evaluate_rule(
                        rule,
                        members,
                        blockers=blockers,
                        warnings=warnings,
                    )
                    if rule.pending_review_blocks_export and any(
                        pair_id in pending
                        and _pair_touches(
                            pair_by_id[pair_id].left_candidate_id,
                            pair_by_id[pair_id].right_candidate_id,
                            cluster.member_candidate_ids,
                        )
                        for pair_id in pending
                    ):
                        blockers.add("RESOLUTION_REVIEW_PENDING")
                if cluster.blocked_match_pair_ids:
                    blockers.add("MATCH_BLOCKED_BY_SEPARATION")

                field_keys = tuple(
                    sorted(
                        set(rule.required_fields) | set(rule.single_value_fields)
                        if rule is not None
                        else set()
                    )
                )
                coverage = tuple(
                    FieldCoverage(
                        fieldKey=field_key,
                        distinctValueCount=len(_field_values(members, field_key)),
                        present=bool(_field_values(members, field_key)),
                    )
                    for field_key in field_keys
                )
                qualities.append(
                    ClusterQuality(
                        clusterId=cluster.cluster_id,
                        exportEligible=not blockers,
                        blockerCodes=tuple(sorted(blockers)),
                        warningCodes=tuple(sorted(warnings)),
                        fieldCoverage=coverage,
                    )
                )
            return tuple(qualities)


    def _evaluate_rule(
        rule: EntityQualityRule,
        members: tuple[CandidateRecord, ...],
        *,
        blockers: set[str],
        warnings: set[str],
    ) -> None:
        for field_key in rule.required_fields:
            if not _field_values(members, field_key):
                blockers.add(f"REQUIRED_FIELD_MISSING_{field_key.upper()}")
        for field_key in rule.single_value_fields:
            if len(_field_values(members, field_key)) > 1:
                blockers.add(f"SINGLE_VALUE_CONFLICT_{field_key.upper()}")
        source_count = len({source for item in members for source in item.source_keys})
        if source_count < rule.minimum_source_count:
            blockers.add("INSUFFICIENT_DISTINCT_SOURCES")

        coverages = {item.geography.coverage for item in members}
        if "outside" in coverages:
            blockers.add("GEOGRAPHY_OUTSIDE_MARKET")
        if "unknown" in coverages:
            blockers.add("GEOGRAPHY_UNKNOWN")
        if "boundary" in coverages and rule.boundary_requires_review:
            blockers.add("GEOGRAPHY_BOUNDARY_REVIEW_REQUIRED")
        for coverage in coverages:
            if coverage not in rule.allowed_geography:
                blockers.add(f"GEOGRAPHY_{coverage.upper()}_NOT_ALLOWED")

        for field_key in ("phone", "email", "website_url", "address"):
            if field_key not in rule.single_value_fields and len(_field_values(members, field_key)) > 1:
                warnings.add(f"MULTIPLE_VALUES_{field_key.upper()}")


    def _field_values(
        members: tuple[CandidateRecord, ...],
        field_key: str,
    ) -> set[str]:
        attribute = {
            "name": "names",
            "phone": "phones",
            "email": "emails",
            "website_url": "websites",
            "address": "addresses",
        }.get(field_key)
        if attribute is None:
            return set()
        return {
            value
            for member in members
            for value in getattr(member, attribute)
        }


    def _pair_touches(
        left: UUID,
        right: UUID,
        members: tuple[UUID, ...],
    ) -> bool:
        member_set = set(members)
        return left in member_set or right in member_set
    ''',
)

write(
    "packages/quality_core/src/quality_core/__init__.py",
    r'''
    from quality_core.evaluator import QualityEvaluator

    __all__ = ["QualityEvaluator"]
    ''',
)

write(
    "apps/resolution_worker/src/resolution_worker/gateway.py",
    r'''
    from __future__ import annotations

    from resolution_contracts import ResolutionSnapshot
    from source_connector_sdk import SourceWorkerGateway, WorkerLease, WorkFailureKind

    _OUTPUT_CONTRACTS = frozenset({"entity-resolution-snapshot@1"})
    _OUTPUT_ROLE = "resolution_snapshot"
    _OUTPUT_CONTENT_TYPE = "application/vnd.collection.entity-resolution-snapshot+json"


    class SdkResolutionWorkerGateway:
        def __init__(self, client: SourceWorkerGateway) -> None:
            self._client = client
            self._build_identity: str | None = None

        def register(self, *, build_identity: str) -> None:
            self._client.register(
                build_identity=build_identity,
                capabilities={"entity_resolution"},
                supported_output_contracts=_OUTPUT_CONTRACTS,
                max_concurrency=1,
                resource_profile="entity-resolution",
            )
            self._build_identity = build_identity

        def acquire(
            self,
            *,
            lease_duration_seconds: int,
            heartbeat_interval_seconds: int,
        ) -> WorkerLease | None:
            return self._client.acquire_lease(
                capability="entity_resolution",
                lease_duration_seconds=lease_duration_seconds,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
            )

        def read_batch(self, lease: WorkerLease, *, maximum_bytes: int) -> bytes:
            artifact = lease.artifact("resolution_batch")
            return self._client.read_artifact(
                lease,
                artifact_id=artifact.artifact_id,
                maximum_bytes=maximum_bytes,
            )

        def publish_result(self, lease: WorkerLease, *, snapshot: ResolutionSnapshot) -> None:
            payload = snapshot.canonical_bytes()
            upload = self._client.upload_bytes(
                lease,
                content=payload,
                artifact_kind="diagnostic_artifact",
                content_type=_OUTPUT_CONTENT_TYPE,
            )
            if upload.content_digest != snapshot.digest():
                raise RuntimeError("resolution snapshot upload digest does not match canonical snapshot")
            self._client.complete(
                lease,
                output_contract=lease.expected_output_contract,
                output_digest=snapshot.digest(),
                worker_build_identity=self._required_build_identity(),
                output_artifacts=((upload.upload_id, _OUTPUT_ROLE),),
            )

        def fail(
            self,
            lease: WorkerLease,
            *,
            failure_kind: WorkFailureKind,
            error_code: str,
            message: str,
            required_action: str,
        ) -> None:
            self._client.fail(
                lease,
                failure_kind=failure_kind,
                code=error_code,
                owner="ResolutionWorker.EntityResolution",
                message=message,
                required_action=required_action,
                worker_build_identity=self._required_build_identity(),
            )

        def _required_build_identity(self) -> str:
            if self._build_identity is None:
                raise RuntimeError("resolution worker must register before processing work")
            return self._build_identity
    ''',
)

write(
    "apps/resolution_worker/src/resolution_worker/worker.py",
    r'''
    from __future__ import annotations

    import time
    from typing import Protocol

    from entity_resolution_core import EntityResolutionEngine
    from quality_core import QualityEvaluator
    from resolution_contracts import (
        EntityResolutionBatch,
        ResolutionGraph,
        ResolutionSnapshot,
    )
    from source_connector_sdk import WorkerLease, WorkFailureKind

    _EXPECTED_OUTPUT_CONTRACT = "entity-resolution-snapshot@1"


    class ResolutionWorkerGateway(Protocol):
        def register(self, *, build_identity: str) -> None: ...

        def acquire(
            self,
            *,
            lease_duration_seconds: int,
            heartbeat_interval_seconds: int,
        ) -> WorkerLease | None: ...

        def read_batch(self, lease: WorkerLease, *, maximum_bytes: int) -> bytes: ...

        def publish_result(
            self,
            lease: WorkerLease,
            *,
            snapshot: ResolutionSnapshot,
        ) -> None: ...

        def fail(
            self,
            lease: WorkerLease,
            *,
            failure_kind: WorkFailureKind,
            error_code: str,
            message: str,
            required_action: str,
        ) -> None: ...


    class ResolutionProcessor(Protocol):
        def resolve(self, batch: EntityResolutionBatch) -> ResolutionGraph: ...


    class QualityProcessor(Protocol):
        def evaluate(
            self,
            batch: EntityResolutionBatch,
            graph: ResolutionGraph,
        ) -> tuple[object, ...]: ...


    class ResolutionWorker:
        def __init__(
            self,
            gateway: ResolutionWorkerGateway,
            processor: ResolutionProcessor | None = None,
            *_: object,
            quality_evaluator: QualityEvaluator | None = None,
            build_identity: str = "resolution-worker-unknown",
            lease_duration_seconds: int = 300,
            heartbeat_interval_seconds: int = 30,
            batch_maximum_bytes: int = 10 * 1024 * 1024,
            executor: ResolutionProcessor | None = None,
            connector: ResolutionProcessor | None = None,
            client: ResolutionProcessor | None = None,
            **__: object,
        ) -> None:
            self._gateway = gateway
            self._processor = processor or executor or connector or client or EntityResolutionEngine()
            self._quality = quality_evaluator or QualityEvaluator()
            self._build_identity = build_identity
            self._lease_duration_seconds = lease_duration_seconds
            self._heartbeat_interval_seconds = heartbeat_interval_seconds
            self._batch_maximum_bytes = batch_maximum_bytes
            self._registered = False

        def run_once(self) -> bool:
            self._ensure_registered()
            lease = self._gateway.acquire(
                lease_duration_seconds=self._lease_duration_seconds,
                heartbeat_interval_seconds=self._heartbeat_interval_seconds,
            )
            if lease is None:
                return False
            try:
                _validate_lease(lease)
                batch = EntityResolutionBatch.from_bytes(
                    self._gateway.read_batch(
                        lease,
                        maximum_bytes=self._batch_maximum_bytes,
                    )
                )
                graph = self._processor.resolve(batch)
                quality = self._quality.evaluate(batch, graph)
                snapshot = ResolutionSnapshot(
                    contract="entity-resolution-snapshot",
                    contractRevision="entity-resolution-snapshot-v1",
                    batchId=batch.batch_id,
                    batchDigest=batch.digest(),
                    campaignKey=batch.campaign_key,
                    marketAreaRevision=batch.market_area.revision,
                    marketAreaDigest=batch.market_area.digest,
                    candidateCount=len(batch.candidates),
                    graph=graph,
                    quality=quality,
                )
                self._gateway.publish_result(lease, snapshot=snapshot)
            except (UnicodeError, ValueError):
                self._gateway.fail(
                    lease,
                    failure_kind="permanent",
                    error_code="RESOLUTION_BATCH_INVALID",
                    message="Entity-resolution batch is invalid",
                    required_action=(
                        "Correct the exact immutable batch and geography identity before "
                        "replacement work."
                    ),
                )
            except Exception:  # noqa: BLE001
                self._gateway.fail(
                    lease,
                    failure_kind="permanent",
                    error_code="RESOLUTION_WORKER_DEFECT",
                    message="Resolution worker encountered an internal defect",
                    required_action=(
                        "Inspect the resolution build and exact batch before replacement work."
                    ),
                )
            return True

        def run_forever(
            self,
            *,
            poll_interval_seconds: float = 1.0,
            maximum_idle_cycles: int | None = None,
            **_: object,
        ) -> None:
            idle_cycles = 0
            while maximum_idle_cycles is None or idle_cycles < maximum_idle_cycles:
                if self.run_once():
                    idle_cycles = 0
                    continue
                idle_cycles += 1
                time.sleep(poll_interval_seconds)

        def _ensure_registered(self) -> None:
            if self._registered:
                return
            self._gateway.register(build_identity=self._build_identity)
            self._registered = True


    def _validate_lease(lease: WorkerLease) -> None:
        if lease.expected_output_contract != _EXPECTED_OUTPUT_CONTRACT:
            raise ValueError("lease output contract is not supported")
        if getattr(lease, "source_key", None) is not None:
            raise ValueError("entity-resolution work must not carry source-capacity ownership")
    ''',
)

write(
    "apps/resolution_worker/src/resolution_worker/__init__.py",
    r'''
    from resolution_worker.gateway import SdkResolutionWorkerGateway
    from resolution_worker.worker import ResolutionWorker

    __all__ = ["ResolutionWorker", "SdkResolutionWorkerGateway"]
    ''',
)

# Synthetic golden dataset. It is not a production Berlin polygon or business dataset.
golden = {
    "datasetContract": "entity-resolution-golden-dataset-v1",
    "batch": {
        "contract": "entity-resolution-batch",
        "contractRevision": "entity-resolution-batch-v1",
        "batchId": "00000000-0000-5000-8000-000000000701",
        "campaignKey": "berlin_recording_services",
        "marketArea": {
            "revision": "berlin-boundary-synthetic-fixture-v1",
            "digest": "sha256:" + "c" * 64,
            "readiness": "ready",
            "blockerCodes": [],
        },
        "candidates": [],
        "decisions": [],
        "priorClusters": [],
        "policy": {
            "thresholds": {
                "nameReviewBasisPoints": 7000,
                "addressReviewBasisPoints": 7000,
                "websiteNameAutoBasisPoints": 8000,
                "websiteAddressAutoBasisPoints": 7000,
                "addressNameAutoBasisPoints": 9000,
                "maximumCandidates": 100,
                "maximumPairs": 1000,
            },
            "qualityRules": [
                {
                    "entityKind": "recording_studio",
                    "requiredFields": ["name", "website_url"],
                    "singleValueFields": ["name"],
                    "minimumSourceCount": 2,
                    "allowedGeography": ["inside"],
                    "boundaryRequiresReview": True,
                    "pendingReviewBlocksExport": True,
                }
            ],
        },
    },
    "expected": {
        "pairDispositions": [
            {
                "leftCandidateId": "00000000-0000-5000-8000-000000000001",
                "rightCandidateId": "00000000-0000-5000-8000-000000000002",
                "disposition": "auto_match",
            },
            {
                "leftCandidateId": "00000000-0000-5000-8000-000000000003",
                "rightCandidateId": "00000000-0000-5000-8000-000000000004",
                "disposition": "review_required",
            },
        ],
        "clusters": [
            [
                "00000000-0000-5000-8000-000000000001",
                "00000000-0000-5000-8000-000000000002",
            ],
            ["00000000-0000-5000-8000-000000000003"],
            ["00000000-0000-5000-8000-000000000004"],
        ],
        "eligibleClusters": [
            [
                "00000000-0000-5000-8000-000000000001",
                "00000000-0000-5000-8000-000000000002",
            ]
        ],
    },
}
for index, candidate_id in enumerate(
    (
        "00000000-0000-5000-8000-000000000001",
        "00000000-0000-5000-8000-000000000002",
        "00000000-0000-5000-8000-000000000003",
        "00000000-0000-5000-8000-000000000004",
    ),
    start=1,
):
    strong = index <= 2
    candidate = {
        "candidateId": candidate_id,
        "entityKind": "recording_studio",
        "names": ["Signal Room"] if strong else [
            "Berlin Audio House" if index == 3 else "Berlin Audio Haus"
        ],
        "phones": ["+493012345678"] if strong else [],
        "emails": [],
        "websites": [
            "https://signal-room.example/"
            if strong
            else f"https://audio-{index}.example/"
        ],
        "addresses": [
            "Street 1, 10115 Berlin"
            if strong
            else ("Audio Street 7, Berlin" if index == 3 else "Audio Strasse 7, Berlin")
        ],
        "observationIds": [f"00000000-0000-5000-9000-{index:012d}"],
        "sourceArtifactIds": [f"artifact-{index}"],
        "sourceKeys": [f"source_{index}"],
        "geography": {
            "coverage": "inside",
            "marketAreaRevision": "berlin-boundary-synthetic-fixture-v1",
            "marketAreaDigest": "sha256:" + "c" * 64,
            "latitude": 52.52 + index / 10000,
            "longitude": 13.405 + index / 10000,
            "evidenceObservationIds": [f"00000000-0000-5000-9000-{index:012d}"],
        },
    }
    golden["batch"]["candidates"].append(candidate)
write(
    "datasets/entity_resolution/berlin-recording-services-golden-v1.json",
    json.dumps(golden, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
)

write(
    "packages/resolution_contracts/tests/test_contracts.py",
    r'''
    from __future__ import annotations

    from uuid import UUID

    import pytest
    from resolution_contracts import (
        ResolutionDecision,
        canonical_pair,
        deterministic_cluster_id,
        deterministic_pair_id,
    )


    def test_pair_and_cluster_identities_are_deterministic() -> None:
        left = UUID("00000000-0000-5000-8000-000000000001")
        right = UUID("00000000-0000-5000-8000-000000000002")

        assert canonical_pair(right, left) == (left, right)
        assert deterministic_pair_id(left, right) == deterministic_pair_id(right, left)
        assert deterministic_cluster_id((right, left)) == deterministic_cluster_id((left, right))


    def test_decision_digest_is_fail_closed() -> None:
        decision = ResolutionDecision.create(
            decision_id=UUID("00000000-0000-5000-8000-000000000101"),
            left_candidate_id=UUID("00000000-0000-5000-8000-000000000001"),
            right_candidate_id=UUID("00000000-0000-5000-8000-000000000002"),
            action="separate",
            revision=1,
            actor_reference="reviewer-1",
            reason_code="CONFIRMED_DISTINCT",
        )
        payload = decision.model_dump(by_alias=True, mode="json")
        payload["reasonCode"] = "MUTATED_REASON"

        with pytest.raises(ValueError):
            ResolutionDecision.model_validate(payload)
    ''',
)

write(
    "packages/entity_resolution_core/tests/test_resolution.py",
    r'''
    from __future__ import annotations

    import json
    from pathlib import Path
    from uuid import UUID

    import pytest
    from entity_resolution_core import EntityResolutionEngine
    from quality_core import QualityEvaluator
    from resolution_contracts import (
        EntityResolutionBatch,
        PriorCluster,
        ResolutionDecision,
        ResolutionSnapshot,
        deterministic_cluster_id,
    )

    ROOT = Path(__file__).resolve().parents[3]
    DATASET = ROOT / "datasets/entity_resolution/berlin-recording-services-golden-v1.json"


    def _golden() -> tuple[EntityResolutionBatch, dict[str, object]]:
        payload = json.loads(DATASET.read_text(encoding="utf-8"))
        return EntityResolutionBatch.model_validate(payload["batch"]), payload["expected"]


    def _snapshot(batch: EntityResolutionBatch) -> ResolutionSnapshot:
        graph = EntityResolutionEngine().resolve(batch)
        quality = QualityEvaluator().evaluate(batch, graph)
        return ResolutionSnapshot(
            contract="entity-resolution-snapshot",
            contractRevision="entity-resolution-snapshot-v1",
            batchId=batch.batch_id,
            batchDigest=batch.digest(),
            campaignKey=batch.campaign_key,
            marketAreaRevision=batch.market_area.revision,
            marketAreaDigest=batch.market_area.digest,
            candidateCount=len(batch.candidates),
            graph=graph,
            quality=quality,
        )


    def _replace_batch(
        batch: EntityResolutionBatch,
        **updates: object,
    ) -> EntityResolutionBatch:
        payload = batch.model_dump(by_alias=True, mode="json")
        payload.update(updates)
        return EntityResolutionBatch.model_validate(payload)


    def test_golden_dataset_proves_pairs_clusters_and_quality() -> None:
        batch, expected = _golden()
        snapshot = _snapshot(batch)
        dispositions = {
            (str(item.left_candidate_id), str(item.right_candidate_id)): item.disposition
            for item in snapshot.graph.pairs
        }
        expected_dispositions = {
            (item["leftCandidateId"], item["rightCandidateId"]): item["disposition"]
            for item in expected["pairDispositions"]
        }
        memberships = {
            tuple(str(member) for member in item.member_candidate_ids)
            for item in snapshot.graph.clusters
        }
        eligible = {
            tuple(str(member) for member in cluster.member_candidate_ids)
            for cluster, quality in zip(snapshot.graph.clusters, snapshot.quality, strict=True)
            if quality.export_eligible
        }

        assert dispositions == expected_dispositions
        assert memberships == {tuple(item) for item in expected["clusters"]}
        assert eligible == {tuple(item) for item in expected["eligibleClusters"]}


    def test_name_only_and_fuzzy_berlin_pair_never_auto_merges() -> None:
        batch, _ = _golden()
        snapshot = _snapshot(batch)
        pair = next(
            item
            for item in snapshot.graph.pairs
            if str(item.left_candidate_id).endswith("000000000003")
        )

        assert pair.features.name_only is True
        assert pair.disposition == "review_required"
        assert "NAME_ONLY_REQUIRES_REVIEW" in pair.reason_codes
        assert "FUZZY_BERLIN_MATCH_REQUIRES_REVIEW" in pair.reason_codes
        assert not any(
            set(cluster.member_candidate_ids)
            == {pair.left_candidate_id, pair.right_candidate_id}
            for cluster in snapshot.graph.clusters
        )


    def test_split_is_immutable_and_reversal_restores_original_cluster_id() -> None:
        batch, _ = _golden()
        first = _snapshot(batch)
        members = tuple(item.candidate_id for item in batch.candidates[:2])
        original_cluster_id = deterministic_cluster_id(members)
        assert any(item.cluster_id == original_cluster_id for item in first.graph.clusters)

        separation = ResolutionDecision.create(
            decision_id=UUID("00000000-0000-5000-8000-000000000201"),
            left_candidate_id=members[0],
            right_candidate_id=members[1],
            action="separate",
            revision=1,
            actor_reference="reviewer-1",
            reason_code="CONFIRMED_DISTINCT",
        )
        split_batch = _replace_batch(
            batch,
            batchId="00000000-0000-5000-8000-000000000702",
            decisions=[separation.model_dump(by_alias=True, mode="json")],
            priorClusters=[
                PriorCluster(
                    clusterId=original_cluster_id,
                    memberCandidateIds=members,
                ).model_dump(by_alias=True, mode="json")
            ],
        )
        split = _snapshot(split_batch)
        split_clusters = [
            item for item in split.graph.clusters if set(item.member_candidate_ids) <= set(members)
        ]
        assert len(split_clusters) == 2
        assert {item.lineage_kind for item in split_clusters} == {"split"}

        match = ResolutionDecision.create(
            decision_id=UUID("00000000-0000-5000-8000-000000000202"),
            left_candidate_id=members[0],
            right_candidate_id=members[1],
            action="match",
            revision=2,
            actor_reference="reviewer-1",
            reason_code="REVERSAL_CONFIRMED_MATCH",
        )
        reversal_batch = _replace_batch(
            batch,
            batchId="00000000-0000-5000-8000-000000000703",
            decisions=[match.model_dump(by_alias=True, mode="json")],
            priorClusters=[
                PriorCluster(
                    clusterId=item.cluster_id,
                    memberCandidateIds=item.member_candidate_ids,
                ).model_dump(by_alias=True, mode="json")
                for item in sorted(split_clusters, key=lambda value: value.cluster_id)
            ],
        )
        reversed_snapshot = _snapshot(reversal_batch)
        restored = next(
            item
            for item in reversed_snapshot.graph.clusters
            if set(item.member_candidate_ids) == set(members)
        )

        assert restored.cluster_id == original_cluster_id
        assert restored.lineage_kind == "merge"


    def test_transitive_match_cannot_bypass_manual_separation() -> None:
        batch, _ = _golden()
        payload = batch.model_dump(by_alias=True, mode="json")
        candidates = payload["candidates"][:3]
        candidates[0]["names"] = ["A"]
        candidates[0]["phones"] = ["+493000000001"]
        candidates[0]["emails"] = []
        candidates[1]["names"] = ["B"]
        candidates[1]["phones"] = ["+493000000001"]
        candidates[1]["emails"] = ["bridge@example.com"]
        candidates[2]["names"] = ["C"]
        candidates[2]["phones"] = []
        candidates[2]["emails"] = ["bridge@example.com"]
        left = UUID(candidates[0]["candidateId"])
        right = UUID(candidates[2]["candidateId"])
        separation = ResolutionDecision.create(
            decision_id=UUID("00000000-0000-5000-8000-000000000203"),
            left_candidate_id=left,
            right_candidate_id=right,
            action="separate",
            revision=1,
            actor_reference="reviewer-1",
            reason_code="CONFIRMED_DISTINCT",
        )
        payload.update(
            {
                "batchId": "00000000-0000-5000-8000-000000000704",
                "candidates": candidates,
                "decisions": [separation.model_dump(by_alias=True, mode="json")],
            }
        )
        constrained = EntityResolutionBatch.model_validate(payload)
        snapshot = _snapshot(constrained)

        assert not any(
            left in cluster.member_candidate_ids and right in cluster.member_candidate_ids
            for cluster in snapshot.graph.clusters
        )
        assert snapshot.graph.blocked_match_pair_ids
        assert any(
            item.code == "MATCH_EDGE_BLOCKED_BY_SEPARATION"
            for item in snapshot.graph.diagnostics
        )


    def test_market_area_readiness_and_geography_identity_fail_closed() -> None:
        batch, _ = _golden()
        payload = batch.model_dump(by_alias=True, mode="json")
        payload["marketArea"] = {
            **payload["marketArea"],
            "readiness": "blocked",
            "blockerCodes": ["BERLIN_BOUNDARY_ARTIFACT_MISSING"],
        }
        with pytest.raises(ValueError):
            EntityResolutionBatch.model_validate(payload)

        payload = batch.model_dump(by_alias=True, mode="json")
        payload["candidates"][0]["geography"]["marketAreaDigest"] = "sha256:" + "d" * 64
        with pytest.raises(ValueError):
            EntityResolutionBatch.model_validate(payload)


    def test_same_batch_produces_byte_identical_snapshot() -> None:
        batch, _ = _golden()

        assert _snapshot(batch).canonical_bytes() == _snapshot(batch).canonical_bytes()
    ''',
)

write(
    "packages/quality_core/tests/test_quality.py",
    r'''
    from __future__ import annotations

    import json
    from pathlib import Path

    from entity_resolution_core import EntityResolutionEngine
    from quality_core import QualityEvaluator
    from resolution_contracts import EntityResolutionBatch

    ROOT = Path(__file__).resolve().parents[3]
    DATASET = ROOT / "datasets/entity_resolution/berlin-recording-services-golden-v1.json"


    def test_quality_is_fail_closed_for_pending_fuzzy_pairs() -> None:
        payload = json.loads(DATASET.read_text(encoding="utf-8"))
        batch = EntityResolutionBatch.model_validate(payload["batch"])
        graph = EntityResolutionEngine().resolve(batch)
        quality = QualityEvaluator().evaluate(batch, graph)
        by_members = {
            tuple(str(item) for item in cluster.member_candidate_ids): assessment
            for cluster, assessment in zip(graph.clusters, quality, strict=True)
        }

        for member in (
            "00000000-0000-5000-8000-000000000003",
            "00000000-0000-5000-8000-000000000004",
        ):
            assessment = by_members[(member,)]
            assert assessment.export_eligible is False
            assert "RESOLUTION_REVIEW_PENDING" in assessment.blocker_codes
            assert "INSUFFICIENT_DISTINCT_SOURCES" in assessment.blocker_codes
    ''',
)

write(
    "apps/resolution_worker/tests/test_worker.py",
    r'''
    from __future__ import annotations

    import json
    from collections import deque
    from dataclasses import dataclass
    from pathlib import Path
    from typing import cast

    from entity_resolution_core import EntityResolutionEngine
    from quality_core import QualityEvaluator
    from resolution_contracts import ResolutionSnapshot
    from resolution_worker.worker import ResolutionWorker
    from source_connector_sdk import WorkerLease, WorkFailureKind

    ROOT = Path(__file__).resolve().parents[3]
    DATASET = ROOT / "datasets/entity_resolution/berlin-recording-services-golden-v1.json"


    @dataclass
    class _Lease:
        expected_output_contract: str = "entity-resolution-snapshot@1"
        source_key: str | None = None


    class _Gateway:
        def __init__(
            self,
            batches: list[bytes],
            *,
            lease_source_key: str | None = None,
        ) -> None:
            self.batches = deque(batches)
            self.lease_source_key = lease_source_key
            self.registered: list[str] = []
            self.published: list[ResolutionSnapshot] = []
            self.failures: list[tuple[WorkFailureKind, str]] = []

        def register(self, *, build_identity: str) -> None:
            self.registered.append(build_identity)

        def acquire(
            self,
            *,
            lease_duration_seconds: int,
            heartbeat_interval_seconds: int,
        ) -> WorkerLease | None:
            del lease_duration_seconds, heartbeat_interval_seconds
            if not self.batches:
                return None
            return cast(WorkerLease, _Lease(source_key=self.lease_source_key))

        def read_batch(self, lease: WorkerLease, *, maximum_bytes: int) -> bytes:
            del lease
            value = self.batches.popleft()
            assert len(value) <= maximum_bytes
            return value

        def publish_result(
            self,
            lease: WorkerLease,
            *,
            snapshot: ResolutionSnapshot,
        ) -> None:
            del lease
            self.published.append(snapshot)

        def fail(
            self,
            lease: WorkerLease,
            *,
            failure_kind: WorkFailureKind,
            error_code: str,
            message: str,
            required_action: str,
        ) -> None:
            del lease, message, required_action
            self.failures.append((failure_kind, error_code))


    def _batch_bytes() -> bytes:
        payload = json.loads(DATASET.read_text(encoding="utf-8"))["batch"]
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


    def test_worker_publishes_resolution_snapshot() -> None:
        gateway = _Gateway([_batch_bytes()])
        worker = ResolutionWorker(
            gateway,
            EntityResolutionEngine(),
            quality_evaluator=QualityEvaluator(),
            build_identity="build-resolution",
        )

        assert worker.run_once() is True
        assert gateway.registered == ["build-resolution"]
        assert gateway.published[0].candidate_count == 4
        assert gateway.failures == []


    def test_worker_rejects_source_bound_resolution_lease() -> None:
        gateway = _Gateway([_batch_bytes()], lease_source_key="source_forbidden")

        assert ResolutionWorker(gateway).run_once() is True
        assert gateway.published == []
        assert gateway.failures == [("permanent", "RESOLUTION_BATCH_INVALID")]


    def test_restart_reacquires_db_owned_resolution_work() -> None:
        gateway = _Gateway([_batch_bytes(), _batch_bytes()])

        assert ResolutionWorker(gateway).run_once() is True
        assert ResolutionWorker(gateway).run_once() is True
        assert len(gateway.published) == 2
        assert len(gateway.registered) == 2
    ''',
)

extraction_dockerfile = transformed(
    "deploy/docker/extraction-worker.Dockerfile",
    (
        ("packages/extraction_core", "packages/entity_resolution_core"),
        ("packages/normalization_core", "packages/quality_core"),
        ("packages/observation_contracts", "packages/resolution_contracts"),
        ("apps/extraction_worker", "apps/resolution_worker"),
        ("extraction-worker", "resolution-worker"),
    ),
)
write("deploy/docker/resolution-worker.Dockerfile", extraction_dockerfile)

checker_path = ROOT / "tools/architecture_checks/check_dependencies.py"
checker = checker_path.read_text(encoding="utf-8")
if '"resolution_worker": OwnerPolicy(' not in checker:
    policies = '''    "resolution_worker": OwnerPolicy(
        project_path="apps/resolution_worker",
        distribution_name="resolution-worker",
        allowed_internal_imports=(
            "entity_resolution_core",
            "quality_core",
            "resolution_contracts",
            "source_connector_sdk",
        ),
        allowed_external_imports=frozenset({"pydantic"}),
    ),
    "entity_resolution_core": OwnerPolicy(
        project_path="packages/entity_resolution_core",
        distribution_name="entity-resolution-core",
        allowed_internal_imports=("resolution_contracts",),
        allowed_external_imports=frozenset(),
    ),
    "quality_core": OwnerPolicy(
        project_path="packages/quality_core",
        distribution_name="quality-core",
        allowed_internal_imports=("resolution_contracts",),
        allowed_external_imports=frozenset(),
    ),
    "resolution_contracts": OwnerPolicy(
        project_path="packages/resolution_contracts",
        distribution_name="resolution-contracts",
        allowed_internal_imports=(),
        allowed_external_imports=frozenset({"pydantic"}),
    ),
'''
    anchor = '    "extraction_worker": OwnerPolicy(\n'
    if anchor not in checker:
        raise RuntimeError("architecture registry extraction owner anchor is missing")
    checker = checker.replace(anchor, policies + anchor, 1)
checker_path.write_text(checker, encoding="utf-8")

subprocess.run([sys.executable, str(checker_path), "--print-policy"], check=True, capture_output=True)
