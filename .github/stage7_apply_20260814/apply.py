from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected source fragment is missing")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def add_list_item(text: str, *, section_start: int, assignment: str, item: str) -> str:
    assignment_start = text.index(assignment, section_start)
    list_start = text.index("[", assignment_start)
    list_end = text.index("\n]", list_start)
    rendered = f'  "{item}",'
    if rendered in text[list_start:list_end]:
        return text
    return text[:list_end] + f"\n{rendered}" + text[list_end:]


def clone_project(
    source: str,
    target: str,
    *,
    source_distribution: str,
    target_distribution: str,
    source_module: str,
    target_module: str,
    dependencies: tuple[str, ...],
) -> None:
    text = (ROOT / source / "pyproject.toml").read_text(encoding="utf-8")
    text = text.replace(source_distribution, target_distribution)
    text = text.replace(source_module, target_module)
    rendered_dependencies = "dependencies = [\n" + "".join(
        f'  "{dependency}",\n' for dependency in dependencies
    ) + "]"
    text, count = re.subn(
        r"(?ms)^dependencies = \[.*?^\]",
        rendered_dependencies,
        text,
        count=1,
    )
    if count != 1:
        text, count = re.subn(
            r"(?m)^dependencies = \[[^\n]*\]$",
            rendered_dependencies,
            text,
            count=1,
        )
    if count != 1:
        raise RuntimeError(f"{source}: dependencies declaration was not found")
    target_path = ROOT / target / "pyproject.toml"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(text, encoding="utf-8")


def update_workspace() -> None:
    path = ROOT / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    workspace_start = text.index("[tool.uv.workspace]")
    for member in (
        "packages/resolution_contracts",
        "packages/entity_resolution_core",
        "packages/quality_core",
    ):
        text = add_list_item(
            text,
            section_start=workspace_start,
            assignment="members = [",
            item=member,
        )
    mypy_start = text.index("[tool.mypy]")
    for file_path in (
        "packages/resolution_contracts/src/resolution_contracts",
        "packages/entity_resolution_core/src/entity_resolution_core",
        "packages/quality_core/src/quality_core",
        "tools/resolution_contract_generation/generate.py",
    ):
        text = add_list_item(
            text,
            section_start=mypy_start,
            assignment="files = [",
            item=file_path,
        )
    path.write_text(text, encoding="utf-8")


def update_architecture_policy() -> None:
    path = ROOT / "tools/architecture_checks/check_dependencies.py"
    text = path.read_text(encoding="utf-8")
    if '"resolution_contracts": OwnerPolicy(' not in text:
        marker = '    "collection_infrastructure": OwnerPolicy(\n'
        policies = '''    "resolution_contracts": OwnerPolicy(
        project_path="packages/resolution_contracts",
        distribution_name="resolution-contracts",
        allowed_internal_imports=(),
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
'''
        if marker not in text:
            raise RuntimeError("architecture owner insertion point is missing")
        text = text.replace(marker, policies + marker, 1)
        path.write_text(text, encoding="utf-8")

    policy = subprocess.check_output(
        [sys.executable, str(path), "--print-policy"],
        text=True,
    ).strip()
    doc = ROOT / "docs/architecture/dependency-rules.md"
    doc_text = doc.read_text(encoding="utf-8")
    start_marker = "<!-- dependency-policy:start -->"
    end_marker = "<!-- dependency-policy:end -->"
    start = doc_text.index(start_marker)
    end = doc_text.index(end_marker, start) + len(end_marker)
    doc.write_text(doc_text[:start] + policy + doc_text[end:], encoding="utf-8")


def update_status() -> None:
    path = ROOT / "docs/implementation-status.md"
    text = path.read_text(encoding="utf-8")
    marker = "## Stage 7 — entity resolution, geography, and quality"
    if marker not in text:
        text = text.rstrip() + f'''\n\n{marker}\n\nStatus: **core owners implemented; runtime orchestration remains separate work**.\n\n- `resolution_contracts` owns versioned pair, cluster, batch, and quality wire contracts.\n- `entity_resolution_core` owns deterministic pair decisions, transitivity-safe clustering, and reversible split lineage.\n- `quality_core` owns fail-closed quality and export-eligibility evaluation.\n- Name equality alone cannot produce an automatic merge.\n- Campaign configuration supplies fuzzy-geography review reasons; Berlin-specific vocabulary is absent from core code.\n- PostgreSQL persistence, review workflow, and operator UI are not claimed by this core block.\n'''
        path.write_text(text, encoding="utf-8")


def main() -> int:
    clone_project(
        "packages/collection_contracts",
        "packages/resolution_contracts",
        source_distribution="collection-contracts",
        target_distribution="resolution-contracts",
        source_module="collection_contracts",
        target_module="resolution_contracts",
        dependencies=("pydantic==2.13.4",),
    )
    clone_project(
        "packages/manual_import_core",
        "packages/entity_resolution_core",
        source_distribution="manual-import-core",
        target_distribution="entity-resolution-core",
        source_module="manual_import_core",
        target_module="entity_resolution_core",
        dependencies=("resolution-contracts",),
    )
    clone_project(
        "packages/manual_import_core",
        "packages/quality_core",
        source_distribution="manual-import-core",
        target_distribution="quality-core",
        source_module="manual_import_core",
        target_module="quality_core",
        dependencies=("resolution-contracts",),
    )

    write(
        "packages/resolution_contracts/src/resolution_contracts/contracts.py",
        '''from __future__ import annotations

import json
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
EntityKind = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")]
ReasonCode = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")]
GeographyCoverage = Literal["inside", "boundary", "outside", "unknown"]
GeographyRelation = Literal["same", "compatible", "fuzzy", "conflict", "unknown"]
PairDecisionState = Literal["auto_match", "review", "separate"]
ClusterOperation = Literal["created", "merged", "split"]
FieldState = Literal["observed", "missing", "conflicted", "unsupported"]
ResolutionState = Literal["resolved", "review", "blocked"]

_PAIR_NAMESPACE = UUID("d696e24d-b2a8-4ae4-a919-d3d2075c3ef0")
_CLUSTER_NAMESPACE = UUID("d1c58999-ac41-488e-bf17-22ae9ec8eefe")


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return f"sha256:{sha256(payload).hexdigest()}"


def deterministic_pair_id(left_candidate_id: UUID, right_candidate_id: UUID) -> UUID:
    if left_candidate_id == right_candidate_id:
        raise ValueError("a resolution pair requires two distinct candidates")
    left, right = sorted((str(left_candidate_id), str(right_candidate_id)))
    return uuid5(_PAIR_NAMESPACE, f"{left}:{right}")


def deterministic_cluster_id(
    member_candidate_ids: tuple[UUID, ...],
    *,
    lineage_key: str,
    revision: int,
) -> UUID:
    if not member_candidate_ids:
        raise ValueError("a cluster requires at least one candidate")
    canonical_members = tuple(sorted(set(member_candidate_ids), key=str))
    material = ":".join(str(value) for value in canonical_members)
    return uuid5(_CLUSTER_NAMESPACE, f"{lineage_key}:{revision}:{material}")


class CandidateSnapshot(StrictContract):
    candidate_id: UUID
    entity_kind: EntityKind
    normalized_name: Annotated[str, Field(min_length=1, max_length=500)]
    domains: tuple[Annotated[str, Field(min_length=1, max_length=253)], ...] = ()
    phones: tuple[Annotated[str, Field(min_length=3, max_length=40)], ...] = ()
    geography_coverage: GeographyCoverage = "unknown"
    evidence_references: tuple[Digest, ...] = ()

    @field_validator("domains", "phones", "evidence_references")
    @classmethod
    def require_canonical_strings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if value != canonical:
            raise ValueError("collection values must be unique and canonically ordered")
        return value


class ResolutionPair(StrictContract):
    pair_id: UUID
    left_candidate_id: UUID
    right_candidate_id: UUID
    geography_relation: GeographyRelation

    @model_validator(mode="after")
    def require_deterministic_identity(self) -> Self:
        if str(self.left_candidate_id) >= str(self.right_candidate_id):
            raise ValueError("pair candidate identities must be canonically ordered")
        expected = deterministic_pair_id(self.left_candidate_id, self.right_candidate_id)
        if self.pair_id != expected:
            raise ValueError("pair_id does not match the deterministic pair identity")
        return self


class ResolutionPolicy(StrictContract):
    automatic_match_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.75
    review_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.20
    minimum_strong_features: Annotated[int, Field(ge=1, le=10)] = 1
    fuzzy_geography_review_reason: ReasonCode
    unresolved_geography_review_reason: ReasonCode

    @model_validator(mode="after")
    def require_threshold_order(self) -> Self:
        if self.automatic_match_threshold <= self.review_threshold:
            raise ValueError("automatic_match_threshold must exceed review_threshold")
        return self


class ResolutionBatchRequest(StrictContract):
    batch_id: UUID
    candidates: tuple[CandidateSnapshot, ...]
    pairs: tuple[ResolutionPair, ...]
    policy: ResolutionPolicy

    @model_validator(mode="after")
    def require_complete_canonical_references(self) -> Self:
        candidate_ids = tuple(candidate.candidate_id for candidate in self.candidates)
        if candidate_ids != tuple(sorted(set(candidate_ids), key=str)):
            raise ValueError("candidates must be unique and canonically ordered")
        pair_ids = tuple(pair.pair_id for pair in self.pairs)
        if pair_ids != tuple(sorted(set(pair_ids), key=str)):
            raise ValueError("pairs must be unique and canonically ordered")
        known = set(candidate_ids)
        for pair in self.pairs:
            if pair.left_candidate_id not in known or pair.right_candidate_id not in known:
                raise ValueError("every pair must reference candidates from the same batch")
        return self


class PairDecision(StrictContract):
    pair_id: UUID
    state: PairDecisionState
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    strong_feature_count: Annotated[int, Field(ge=0, le=10)]
    reasons: tuple[ReasonCode, ...]

    @field_validator("reasons")
    @classmethod
    def require_canonical_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical or value != canonical:
            raise ValueError("decision reasons must be non-empty, unique, and ordered")
        return value


class ClusterRevision(StrictContract):
    cluster_id: UUID
    lineage_key: Annotated[str, Field(min_length=1, max_length=200)]
    revision: Annotated[int, Field(ge=0)]
    operation: ClusterOperation
    member_candidate_ids: tuple[UUID, ...]
    previous_cluster_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def require_deterministic_identity(self) -> Self:
        canonical_members = tuple(sorted(set(self.member_candidate_ids), key=str))
        if not canonical_members or self.member_candidate_ids != canonical_members:
            raise ValueError("cluster members must be non-empty, unique, and ordered")
        canonical_previous = tuple(sorted(set(self.previous_cluster_ids), key=str))
        if self.previous_cluster_ids != canonical_previous:
            raise ValueError("previous cluster identities must be unique and ordered")
        expected = deterministic_cluster_id(
            self.member_candidate_ids,
            lineage_key=self.lineage_key,
            revision=self.revision,
        )
        if self.cluster_id != expected:
            raise ValueError("cluster_id does not match deterministic cluster identity")
        return self


class ResolutionBatchResult(StrictContract):
    batch_id: UUID
    decisions: tuple[PairDecision, ...]
    clusters: tuple[ClusterRevision, ...]
    result_digest: Digest

    @model_validator(mode="after")
    def require_canonical_result(self) -> Self:
        if self.decisions != tuple(sorted(self.decisions, key=lambda item: str(item.pair_id))):
            raise ValueError("pair decisions must be canonically ordered")
        if self.clusters != tuple(sorted(self.clusters, key=lambda item: str(item.cluster_id))):
            raise ValueError("cluster revisions must be canonically ordered")
        expected = resolution_result_digest(self.batch_id, self.decisions, self.clusters)
        if self.result_digest != expected:
            raise ValueError("result_digest does not match canonical result content")
        return self


class QualityField(StrictContract):
    field_key: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")]
    state: FieldState


class QualitySubject(StrictContract):
    subject_id: UUID
    cluster_id: UUID
    geography_coverage: GeographyCoverage
    resolution_state: ResolutionState
    required_fields: tuple[QualityField, ...]
    evidence_count: Annotated[int, Field(ge=0)]
    unresolved_review_count: Annotated[int, Field(ge=0)]
    suppressed: bool
    source_policy_active: bool

    @field_validator("required_fields")
    @classmethod
    def require_canonical_fields(
        cls,
        value: tuple[QualityField, ...],
    ) -> tuple[QualityField, ...]:
        keys = tuple(field.field_key for field in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("required quality fields must be unique and ordered")
        return value


class QualityPolicy(StrictContract):
    accepted_geography: tuple[GeographyCoverage, ...] = ("boundary", "inside")
    minimum_evidence_count: Annotated[int, Field(ge=1)] = 1
    require_all_fields_observed: bool = True

    @field_validator("accepted_geography")
    @classmethod
    def require_canonical_geography(
        cls,
        value: tuple[GeographyCoverage, ...],
    ) -> tuple[GeographyCoverage, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical or value != canonical:
            raise ValueError("accepted geography values must be unique and ordered")
        return value


class QualityEvaluation(StrictContract):
    subject_id: UUID
    export_eligible: bool
    blockers: tuple[ReasonCode, ...]
    evaluation_digest: Digest

    @model_validator(mode="after")
    def require_canonical_evaluation(self) -> Self:
        canonical = tuple(sorted(set(self.blockers)))
        if self.blockers != canonical:
            raise ValueError("quality blockers must be unique and ordered")
        if self.export_eligible == bool(self.blockers):
            raise ValueError("export eligibility must be the inverse of blocker presence")
        expected = quality_evaluation_digest(
            self.subject_id,
            self.export_eligible,
            self.blockers,
        )
        if self.evaluation_digest != expected:
            raise ValueError("evaluation_digest does not match canonical evaluation")
        return self


def resolution_result_digest(
    batch_id: UUID,
    decisions: tuple[PairDecision, ...],
    clusters: tuple[ClusterRevision, ...],
) -> str:
    return canonical_digest(
        {
            "batchId": str(batch_id),
            "clusters": [cluster.model_dump(mode="json") for cluster in clusters],
            "decisions": [decision.model_dump(mode="json") for decision in decisions],
        }
    )


def quality_evaluation_digest(
    subject_id: UUID,
    export_eligible: bool,
    blockers: tuple[str, ...],
) -> str:
    return canonical_digest(
        {
            "blockers": list(blockers),
            "exportEligible": export_eligible,
            "subjectId": str(subject_id),
        }
    )
''',
    )
    write(
        "packages/resolution_contracts/src/resolution_contracts/__init__.py",
        '''from resolution_contracts.contracts import (
    CandidateSnapshot,
    ClusterRevision,
    GeographyCoverage,
    GeographyRelation,
    PairDecision,
    QualityEvaluation,
    QualityField,
    QualityPolicy,
    QualitySubject,
    ResolutionBatchRequest,
    ResolutionBatchResult,
    ResolutionPair,
    ResolutionPolicy,
    canonical_digest,
    deterministic_cluster_id,
    deterministic_pair_id,
    quality_evaluation_digest,
    resolution_result_digest,
)

__all__ = [
    "CandidateSnapshot",
    "ClusterRevision",
    "GeographyCoverage",
    "GeographyRelation",
    "PairDecision",
    "QualityEvaluation",
    "QualityField",
    "QualityPolicy",
    "QualitySubject",
    "ResolutionBatchRequest",
    "ResolutionBatchResult",
    "ResolutionPair",
    "ResolutionPolicy",
    "canonical_digest",
    "deterministic_cluster_id",
    "deterministic_pair_id",
    "quality_evaluation_digest",
    "resolution_result_digest",
]
''',
    )

    write(
        "packages/entity_resolution_core/src/entity_resolution_core/engine.py",
        '''from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from resolution_contracts import (
    CandidateSnapshot,
    ClusterRevision,
    PairDecision,
    ResolutionBatchRequest,
    ResolutionBatchResult,
    ResolutionPair,
    deterministic_cluster_id,
    resolution_result_digest,
)


def resolve_batch(request: ResolutionBatchRequest) -> ResolutionBatchResult:
    candidates = {candidate.candidate_id: candidate for candidate in request.candidates}
    decisions = tuple(
        sorted(
            (
                _decide_pair(
                    candidates[pair.left_candidate_id],
                    candidates[pair.right_candidate_id],
                    pair,
                    request,
                )
                for pair in request.pairs
            ),
            key=lambda item: str(item.pair_id),
        )
    )
    clusters = _build_clusters(request, decisions)
    digest = resolution_result_digest(request.batch_id, decisions, clusters)
    return ResolutionBatchResult(
        batch_id=request.batch_id,
        decisions=decisions,
        clusters=clusters,
        result_digest=digest,
    )


def split_cluster(
    cluster: ClusterRevision,
    partitions: tuple[tuple[UUID, ...], ...],
) -> tuple[ClusterRevision, ...]:
    if len(partitions) < 2:
        raise ValueError("a split requires at least two partitions")
    canonical_partitions = tuple(
        sorted(
            (tuple(sorted(set(partition), key=str)) for partition in partitions),
            key=lambda values: tuple(str(value) for value in values),
        )
    )
    if any(not partition for partition in canonical_partitions):
        raise ValueError("split partitions cannot be empty")
    flattened = tuple(value for partition in canonical_partitions for value in partition)
    if len(flattened) != len(set(flattened)):
        raise ValueError("split partitions must be disjoint")
    if set(flattened) != set(cluster.member_candidate_ids):
        raise ValueError("split partitions must exactly cover the source cluster")
    revision = cluster.revision + 1
    lineage_key = str(cluster.cluster_id)
    return tuple(
        sorted(
            (
                ClusterRevision(
                    cluster_id=deterministic_cluster_id(
                        partition,
                        lineage_key=lineage_key,
                        revision=revision,
                    ),
                    lineage_key=lineage_key,
                    revision=revision,
                    operation="split",
                    member_candidate_ids=partition,
                    previous_cluster_ids=(cluster.cluster_id,),
                )
                for partition in canonical_partitions
            ),
            key=lambda item: str(item.cluster_id),
        )
    )


def _decide_pair(
    left: CandidateSnapshot,
    right: CandidateSnapshot,
    pair: ResolutionPair,
    request: ResolutionBatchRequest,
) -> PairDecision:
    score = 0.0
    strong_features = 0
    reasons: set[str] = set()

    if left.entity_kind != right.entity_kind:
        return _decision(pair, "separate", 0.0, 0, {"ENTITY_KIND_CONFLICT"})
    if pair.geography_relation == "conflict":
        return _decision(pair, "separate", 0.0, 0, {"GEOGRAPHY_CONFLICT"})

    if set(left.phones) & set(right.phones):
        score += 0.45
        strong_features += 1
        reasons.add("PHONE_EXACT_MATCH")
    if set(left.domains) & set(right.domains):
        score += 0.35
        strong_features += 1
        reasons.add("DOMAIN_EXACT_MATCH")
    if left.normalized_name == right.normalized_name:
        score += 0.20
        reasons.add("NAME_EXACT_MATCH")

    score = min(round(score, 6), 1.0)
    policy = request.policy
    if pair.geography_relation == "fuzzy":
        reasons.add(policy.fuzzy_geography_review_reason)
        return _decision(pair, "review", score, strong_features, reasons)
    if pair.geography_relation == "unknown":
        reasons.add(policy.unresolved_geography_review_reason)
        return _decision(pair, "review", score, strong_features, reasons)
    if (
        score >= policy.automatic_match_threshold
        and strong_features >= policy.minimum_strong_features
    ):
        reasons.add("AUTOMATIC_MATCH_THRESHOLD_MET")
        return _decision(pair, "auto_match", score, strong_features, reasons)
    if score >= policy.review_threshold:
        if strong_features < policy.minimum_strong_features:
            reasons.add("INSUFFICIENT_STRONG_EVIDENCE")
        reasons.add("SCORE_REQUIRES_REVIEW")
        return _decision(pair, "review", score, strong_features, reasons)
    reasons.add("SCORE_BELOW_REVIEW_THRESHOLD")
    return _decision(pair, "separate", score, strong_features, reasons)


def _decision(
    pair: ResolutionPair,
    state: str,
    score: float,
    strong_features: int,
    reasons: set[str],
) -> PairDecision:
    return PairDecision(
        pair_id=pair.pair_id,
        state=state,  # type: ignore[arg-type]
        score=score,
        strong_feature_count=strong_features,
        reasons=tuple(sorted(reasons)),
    )


def _build_clusters(
    request: ResolutionBatchRequest,
    decisions: tuple[PairDecision, ...],
) -> tuple[ClusterRevision, ...]:
    parent = {candidate.candidate_id: candidate.candidate_id for candidate in request.candidates}
    decision_by_pair = {
        frozenset((pair.left_candidate_id, pair.right_candidate_id)): decision
        for pair, decision in zip(request.pairs, decisions, strict=True)
    }

    def find(value: UUID) -> UUID:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def members(root: UUID) -> set[UUID]:
        return {value for value in parent if find(value) == root}

    for pair, decision in sorted(
        zip(request.pairs, decisions, strict=True),
        key=lambda item: str(item[0].pair_id),
    ):
        if decision.state != "auto_match":
            continue
        left_root = find(pair.left_candidate_id)
        right_root = find(pair.right_candidate_id)
        if left_root == right_root:
            continue
        left_members = members(left_root)
        right_members = members(right_root)
        if not _all_cross_pairs_match(left_members, right_members, decision_by_pair):
            continue
        parent[right_root] = left_root

    groups: dict[UUID, set[UUID]] = {}
    for candidate_id in parent:
        groups.setdefault(find(candidate_id), set()).add(candidate_id)

    lineage_key = str(request.batch_id)
    clusters = tuple(
        sorted(
            (
                ClusterRevision(
                    cluster_id=deterministic_cluster_id(
                        canonical_members,
                        lineage_key=lineage_key,
                        revision=0,
                    ),
                    lineage_key=lineage_key,
                    revision=0,
                    operation="created",
                    member_candidate_ids=canonical_members,
                )
                for values in groups.values()
                for canonical_members in (tuple(sorted(values, key=str)),)
            ),
            key=lambda item: str(item.cluster_id),
        )
    )
    return clusters


def _all_cross_pairs_match(
    left_members: Iterable[UUID],
    right_members: Iterable[UUID],
    decisions: dict[frozenset[UUID], PairDecision],
) -> bool:
    for left in left_members:
        for right in right_members:
            decision = decisions.get(frozenset((left, right)))
            if decision is None or decision.state != "auto_match":
                return False
    return True
''',
    )
    write(
        "packages/entity_resolution_core/src/entity_resolution_core/__init__.py",
        '''from entity_resolution_core.engine import resolve_batch, split_cluster

__all__ = ["resolve_batch", "split_cluster"]
''',
    )

    write(
        "packages/quality_core/src/quality_core/evaluation.py",
        '''from __future__ import annotations

from resolution_contracts import (
    QualityEvaluation,
    QualityPolicy,
    QualitySubject,
    quality_evaluation_digest,
)


def evaluate_quality(
    subject: QualitySubject,
    policy: QualityPolicy,
) -> QualityEvaluation:
    blockers: set[str] = set()
    if subject.geography_coverage not in policy.accepted_geography:
        blockers.add("GEOGRAPHY_NOT_EXPORTABLE")
    if subject.resolution_state != "resolved":
        blockers.add("RESOLUTION_UNRESOLVED")
    if policy.require_all_fields_observed and any(
        field.state != "observed" for field in subject.required_fields
    ):
        blockers.add("REQUIRED_FIELDS_UNRESOLVED")
    if subject.evidence_count < policy.minimum_evidence_count:
        blockers.add("INSUFFICIENT_EVIDENCE")
    if subject.unresolved_review_count:
        blockers.add("UNRESOLVED_REVIEW")
    if subject.suppressed:
        blockers.add("SUPPRESSED")
    if not subject.source_policy_active:
        blockers.add("SOURCE_POLICY_INACTIVE")

    canonical_blockers = tuple(sorted(blockers))
    export_eligible = not canonical_blockers
    digest = quality_evaluation_digest(
        subject.subject_id,
        export_eligible,
        canonical_blockers,
    )
    return QualityEvaluation(
        subject_id=subject.subject_id,
        export_eligible=export_eligible,
        blockers=canonical_blockers,
        evaluation_digest=digest,
    )
''',
    )
    write(
        "packages/quality_core/src/quality_core/__init__.py",
        '''from quality_core.evaluation import evaluate_quality

__all__ = ["evaluate_quality"]
''',
    )

    write(
        "packages/resolution_contracts/tests/test_contracts.py",
        '''from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError
from resolution_contracts import (
    CandidateSnapshot,
    ClusterRevision,
    ResolutionPair,
    deterministic_cluster_id,
    deterministic_pair_id,
)


def test_pair_identity_is_deterministic_and_wire_validated() -> None:
    left, right = sorted((uuid4(), uuid4()), key=str)
    pair_id = deterministic_pair_id(left, right)
    pair = ResolutionPair(
        pair_id=pair_id,
        left_candidate_id=left,
        right_candidate_id=right,
        geography_relation="same",
    )
    assert pair.pair_id == pair_id
    with pytest.raises(ValidationError):
        ResolutionPair(
            pair_id=uuid4(),
            left_candidate_id=left,
            right_candidate_id=right,
            geography_relation="same",
        )


def test_pair_requires_canonical_candidate_order() -> None:
    left, right = sorted((uuid4(), uuid4()), key=str)
    with pytest.raises(ValidationError):
        ResolutionPair(
            pair_id=deterministic_pair_id(left, right),
            left_candidate_id=right,
            right_candidate_id=left,
            geography_relation="same",
        )


def test_cluster_identity_is_deterministic_and_wire_validated() -> None:
    members = tuple(sorted((uuid4(), uuid4()), key=str))
    cluster_id = deterministic_cluster_id(members, lineage_key="batch", revision=0)
    cluster = ClusterRevision(
        cluster_id=cluster_id,
        lineage_key="batch",
        revision=0,
        operation="created",
        member_candidate_ids=members,
    )
    assert cluster.cluster_id == cluster_id
    with pytest.raises(ValidationError):
        cluster.model_copy(update={"cluster_id": uuid4()}).model_validate(
            cluster.model_copy(update={"cluster_id": uuid4()}).model_dump()
        )


def test_candidate_evidence_must_be_canonical() -> None:
    candidate_id = uuid4()
    with pytest.raises(ValidationError):
        CandidateSnapshot(
            candidate_id=candidate_id,
            entity_kind="place",
            normalized_name="studio",
            evidence_references=("sha256:" + "b" * 64, "sha256:" + "a" * 64),
        )
''',
    )

    write(
        "packages/entity_resolution_core/tests/test_engine.py",
        '''from __future__ import annotations

from uuid import uuid4

import pytest
from entity_resolution_core import resolve_batch, split_cluster
from resolution_contracts import (
    CandidateSnapshot,
    ResolutionBatchRequest,
    ResolutionPair,
    ResolutionPolicy,
    deterministic_pair_id,
)


def candidate(
    *,
    name: str,
    kind: str = "place",
    phone: str | None = None,
    domain: str | None = None,
) -> CandidateSnapshot:
    return CandidateSnapshot(
        candidate_id=uuid4(),
        entity_kind=kind,
        normalized_name=name,
        phones=(phone,) if phone else (),
        domains=(domain,) if domain else (),
    )


def request(
    candidates: tuple[CandidateSnapshot, ...],
    relations: dict[frozenset[object], str],
) -> ResolutionBatchRequest:
    ordered = tuple(sorted(candidates, key=lambda item: str(item.candidate_id)))
    pairs: list[ResolutionPair] = []
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            relation = relations.get(frozenset((left.candidate_id, right.candidate_id)), "same")
            pairs.append(
                ResolutionPair(
                    pair_id=deterministic_pair_id(left.candidate_id, right.candidate_id),
                    left_candidate_id=left.candidate_id,
                    right_candidate_id=right.candidate_id,
                    geography_relation=relation,  # type: ignore[arg-type]
                )
            )
    return ResolutionBatchRequest(
        batch_id=uuid4(),
        candidates=ordered,
        pairs=tuple(sorted(pairs, key=lambda item: str(item.pair_id))),
        policy=ResolutionPolicy(
            fuzzy_geography_review_reason="CAMPAIGN_FUZZY_GEOGRAPHY_REVIEW",
            unresolved_geography_review_reason="CAMPAIGN_GEOGRAPHY_UNRESOLVED",
        ),
    )


def test_name_only_equality_never_auto_merges() -> None:
    left = candidate(name="same studio")
    right = candidate(name="same studio")
    result = resolve_batch(request((left, right), {}))
    assert result.decisions[0].state == "review"
    assert "INSUFFICIENT_STRONG_EVIDENCE" in result.decisions[0].reasons


def test_phone_and_domain_evidence_can_auto_match() -> None:
    left = candidate(name="alpha", phone="+4930123", domain="studio.example")
    right = candidate(name="alpha berlin", phone="+4930123", domain="studio.example")
    result = resolve_batch(request((left, right), {}))
    assert result.decisions[0].state == "auto_match"
    assert len(result.clusters) == 1


def test_entity_kind_conflict_forces_separation() -> None:
    left = candidate(name="alpha", kind="place", phone="+4930123")
    right = candidate(name="alpha", kind="provider", phone="+4930123")
    result = resolve_batch(request((left, right), {}))
    assert result.decisions[0].state == "separate"
    assert result.decisions[0].reasons == ("ENTITY_KIND_CONFLICT",)


def test_fuzzy_geography_uses_campaign_policy_reason() -> None:
    left = candidate(name="alpha", phone="+4930123", domain="studio.example")
    right = candidate(name="alpha", phone="+4930123", domain="studio.example")
    relations = {frozenset((left.candidate_id, right.candidate_id)): "fuzzy"}
    result = resolve_batch(request((left, right), relations))
    assert result.decisions[0].state == "review"
    assert "CAMPAIGN_FUZZY_GEOGRAPHY_REVIEW" in result.decisions[0].reasons


def test_transitive_match_cannot_bypass_explicit_separation() -> None:
    first = candidate(name="one", phone="+4930111", domain="one.example")
    second = candidate(name="two", phone="+4930111", domain="two.example")
    third = candidate(name="three", phone="+4930333", domain="two.example")
    relations = {frozenset((first.candidate_id, third.candidate_id)): "conflict"}
    result = resolve_batch(request((first, second, third), relations))
    member_sets = {frozenset(cluster.member_candidate_ids) for cluster in result.clusters}
    assert frozenset((first.candidate_id, second.candidate_id, third.candidate_id)) not in member_sets
    assert len(result.clusters) == 2


def test_missing_cross_pair_is_fail_closed_for_transitive_union() -> None:
    first = candidate(name="one", phone="+4930111", domain="one.example")
    second = candidate(name="two", phone="+4930111", domain="two.example")
    third = candidate(name="three", phone="+4930333", domain="two.example")
    batch = request((first, second, third), {})
    incomplete = batch.model_copy(update={"pairs": batch.pairs[:2]})
    result = resolve_batch(incomplete)
    assert len(result.clusters) >= 2


def test_split_preserves_previous_cluster_lineage() -> None:
    left = candidate(name="alpha", phone="+4930123", domain="studio.example")
    right = candidate(name="alpha", phone="+4930123", domain="studio.example")
    cluster = resolve_batch(request((left, right), {})).clusters[0]
    revisions = split_cluster(cluster, ((left.candidate_id,), (right.candidate_id,)))
    assert len(revisions) == 2
    assert all(item.previous_cluster_ids == (cluster.cluster_id,) for item in revisions)
    assert all(item.revision == cluster.revision + 1 for item in revisions)


def test_split_requires_exact_disjoint_coverage() -> None:
    left = candidate(name="alpha", phone="+4930123", domain="studio.example")
    right = candidate(name="alpha", phone="+4930123", domain="studio.example")
    cluster = resolve_batch(request((left, right), {})).clusters[0]
    with pytest.raises(ValueError):
        split_cluster(cluster, ((left.candidate_id,), (left.candidate_id,)))


def test_resolution_result_is_deterministic_for_same_request() -> None:
    left = candidate(name="alpha", phone="+4930123", domain="studio.example")
    right = candidate(name="alpha", phone="+4930123", domain="studio.example")
    batch = request((left, right), {})
    assert resolve_batch(batch) == resolve_batch(batch)
''',
    )

    write(
        "packages/quality_core/tests/test_evaluation.py",
        '''from __future__ import annotations

from uuid import uuid4

from quality_core import evaluate_quality
from resolution_contracts import QualityField, QualityPolicy, QualitySubject


def subject(**changes: object) -> QualitySubject:
    values: dict[str, object] = {
        "subject_id": uuid4(),
        "cluster_id": uuid4(),
        "geography_coverage": "inside",
        "resolution_state": "resolved",
        "required_fields": (
            QualityField(field_key="display_name", state="observed"),
            QualityField(field_key="website", state="observed"),
        ),
        "evidence_count": 2,
        "unresolved_review_count": 0,
        "suppressed": False,
        "source_policy_active": True,
    }
    values.update(changes)
    return QualitySubject.model_validate(values)


def test_complete_subject_is_export_eligible() -> None:
    evaluation = evaluate_quality(subject(), QualityPolicy())
    assert evaluation.export_eligible is True
    assert evaluation.blockers == ()


def test_unknown_geography_is_fail_closed() -> None:
    evaluation = evaluate_quality(
        subject(geography_coverage="unknown"),
        QualityPolicy(),
    )
    assert evaluation.export_eligible is False
    assert evaluation.blockers == ("GEOGRAPHY_NOT_EXPORTABLE",)


def test_missing_required_field_blocks_export() -> None:
    evaluation = evaluate_quality(
        subject(
            required_fields=(
                QualityField(field_key="display_name", state="observed"),
                QualityField(field_key="website", state="missing"),
            )
        ),
        QualityPolicy(),
    )
    assert "REQUIRED_FIELDS_UNRESOLVED" in evaluation.blockers


def test_unresolved_review_and_resolution_block_export() -> None:
    evaluation = evaluate_quality(
        subject(resolution_state="review", unresolved_review_count=1),
        QualityPolicy(),
    )
    assert evaluation.blockers == ("RESOLUTION_UNRESOLVED", "UNRESOLVED_REVIEW")


def test_suppression_and_inactive_policy_block_export() -> None:
    evaluation = evaluate_quality(
        subject(suppressed=True, source_policy_active=False),
        QualityPolicy(),
    )
    assert evaluation.blockers == ("SOURCE_POLICY_INACTIVE", "SUPPRESSED")


def test_evaluation_digest_is_deterministic() -> None:
    value = subject()
    policy = QualityPolicy()
    assert evaluate_quality(value, policy) == evaluate_quality(value, policy)
''',
    )

    write(
        "tools/resolution_contract_generation/generate.py",
        '''from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

from resolution_contracts import (
    QualityEvaluation,
    QualitySubject,
    ResolutionBatchRequest,
    ResolutionBatchResult,
)

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "contracts/resolution"
MODELS = {
    "quality-evaluation.schema.json": QualityEvaluation,
    "quality-subject.schema.json": QualitySubject,
    "resolution-batch-request.schema.json": ResolutionBatchRequest,
    "resolution-batch-result.schema.json": ResolutionBatchResult,
}


def render() -> dict[str, str]:
    rendered = {
        name: json.dumps(
            model.model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
        for name, model in MODELS.items()
    }
    manifest = {
        "contract": "collector-resolution-contract-manifest",
        "contractRevision": "resolution-contract-manifest-v1",
        "files": {
            name: f"sha256:{sha256(content.encode('utf-8')).hexdigest()}"
            for name, content in sorted(rendered.items())
        },
    }
    rendered["manifest.json"] = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.check:
        problems = [
            name
            for name, content in expected.items()
            if not (OUTPUT / name).exists()
            or (OUTPUT / name).read_text(encoding="utf-8") != content
        ]
        if problems:
            raise SystemExit("resolution contract drift: " + ", ".join(problems))
        return 0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, content in expected.items():
        (OUTPUT / name).write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
    )
    write(
        "tools/resolution_contract_generation/tests/test_generate.py",
        '''from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_resolution_contract_artifacts_are_current() -> None:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [
            sys.executable,
            "tools/resolution_contract_generation/generate.py",
            "--check",
        ],
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
''',
    )

    write(
        "docs/specifications/stage7-resolution-quality.md",
        '''# Stage 7 — deterministic entity resolution and quality

## Owner boundaries

`resolution_contracts` owns versioned pair, cluster, batch-result, quality-subject, and quality-evaluation wire contracts. It also owns deterministic pair and cluster identity functions.

`entity_resolution_core` owns deterministic pair decisions, transitivity-safe cluster construction, and reversible split lineage. It has no connector, database, object-store, worker, or campaign-specific dependency.

`quality_core` owns fail-closed quality and export-eligibility evaluation. It does not publish, persist, or review candidates.

## Invariants

- Normalized-name equality is weak evidence and cannot independently auto-merge candidates.
- Subject-kind conflict forces separation.
- Fuzzy and unresolved geography produce campaign-configured review reasons; Berlin-specific reasons are configuration, not core code.
- A transitive union requires every cross-pair decision to be `auto_match`; a missing, review, or separate edge blocks the union.
- Pair IDs and cluster IDs are deterministic contract invariants and are validated on deserialization.
- Split operations create new immutable cluster revisions that reference the previous cluster.
- Export eligibility is false when geography, resolution, required fields, evidence, review state, suppression, or source policy is unresolved.

## Deferred owners

PostgreSQL candidate persistence, review cases, operator UI, orchestration, and sealed collector export are not part of this core block and must not be hidden inside these packages.

## Proof

The block is accepted only when exact lock restore, generated-schema drift, Ruff, strict mypy, all non-integration tests, architecture checks, and Python compilation pass on one commit.
''',
    )
    write(
        ".codex/modules/resolution-quality.md",
        '''# Resolution and quality module

- Wire owner: `packages/resolution_contracts`.
- Deterministic matching owner: `packages/entity_resolution_core`.
- Fail-closed eligibility owner: `packages/quality_core`.
- No connector, SQL, object-store, HTTP, or worker imports are allowed in these packages.
- Campaign-specific geography review reason codes enter through `ResolutionPolicy`.
- Durable candidates, reviews, and exports belong to later owners.
''',
    )

    update_workspace()
    update_architecture_policy()
    update_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
