from __future__ import annotations

from entity_resolution_core import ResolutionCoreResult, resolve_entities
from quality_core import evaluate_cluster_quality
from resolution_contracts import (
    ResolutionBatch,
    ResolutionDiagnostic,
    ResolutionSnapshot,
    ResolutionSnapshotPayload,
    seal_resolution_snapshot,
)


def build_resolution_snapshot(batch: ResolutionBatch) -> ResolutionSnapshot:
    core = resolve_entities(batch)
    quality = evaluate_cluster_quality(
        batch,
        core.clusters,
        core.pair_resolutions,
        core.pending_review_pair_ids,
    )
    return seal_resolution_snapshot(
        ResolutionSnapshotPayload(
            batch_id=batch.batch_id,
            batch_digest=batch.batch_digest,
            market_area_revision=batch.market_area_revision,
            market_area_digest=batch.market_area_digest,
            pair_resolutions=core.pair_resolutions,
            blocked_match_edges=core.blocked_match_edges,
            clusters=core.clusters,
            quality_assessments=quality,
            pending_review_pair_ids=core.pending_review_pair_ids,
            diagnostics=_diagnostics(batch, core),
        )
    )


def _diagnostics(
    batch: ResolutionBatch,
    core: ResolutionCoreResult,
) -> tuple[ResolutionDiagnostic, ...]:
    return tuple(
        sorted(
            (
                ResolutionDiagnostic(
                    code="BLOCKED_MATCH_EDGE_COUNT", count=len(core.blocked_match_edges)
                ),
                ResolutionDiagnostic(code="CANDIDATE_COUNT", count=len(batch.candidates)),
                ResolutionDiagnostic(code="CLUSTER_COUNT", count=len(core.clusters)),
                ResolutionDiagnostic(code="PAIR_COUNT", count=len(core.pair_resolutions)),
                ResolutionDiagnostic(
                    code="PENDING_REVIEW_PAIR_COUNT",
                    count=len(core.pending_review_pair_ids),
                ),
            ),
            key=lambda item: item.code,
        )
    )
