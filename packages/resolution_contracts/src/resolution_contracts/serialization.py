from __future__ import annotations

from pydantic import BaseModel

from resolution_contracts.identity import canonical_digest, canonical_json
from resolution_contracts.models import (
    ManualResolutionDecision,
    ManualResolutionDecisionPayload,
    PriorCluster,
    PriorClusterPayload,
    ResolutionBatch,
    ResolutionBatchPayload,
    ResolutionSnapshot,
    ResolutionSnapshotPayload,
)


def seal_manual_decision(
    payload: ManualResolutionDecisionPayload,
) -> ManualResolutionDecision:
    return ManualResolutionDecision(
        **payload.model_dump(mode="python"),
        decision_digest=_digest_model(payload),
    )


def verify_manual_decision(decision: ManualResolutionDecision) -> None:
    payload = ManualResolutionDecisionPayload.model_validate(
        decision.model_dump(mode="python", exclude={"decision_digest"})
    )
    if decision.decision_digest != _digest_model(payload):
        raise ValueError("manual decision digest does not match canonical content")


def seal_prior_cluster(payload: PriorClusterPayload) -> PriorCluster:
    return PriorCluster(
        **payload.model_dump(mode="python"),
        content_digest=_digest_model(payload),
    )


def verify_prior_cluster(cluster: PriorCluster) -> None:
    payload = PriorClusterPayload.model_validate(
        cluster.model_dump(mode="python", exclude={"content_digest"})
    )
    if cluster.content_digest != _digest_model(payload):
        raise ValueError("prior cluster digest does not match canonical content")


def seal_resolution_batch(payload: ResolutionBatchPayload) -> ResolutionBatch:
    for decision in payload.manual_decisions:
        verify_manual_decision(decision)
    for cluster in payload.prior_clusters:
        verify_prior_cluster(cluster)
    return ResolutionBatch(
        **payload.model_dump(mode="python"),
        batch_digest=_digest_model(payload),
    )


def verify_resolution_batch(batch: ResolutionBatch) -> None:
    payload = ResolutionBatchPayload.model_validate(
        batch.model_dump(mode="python", exclude={"batch_digest"})
    )
    for decision in payload.manual_decisions:
        verify_manual_decision(decision)
    for cluster in payload.prior_clusters:
        verify_prior_cluster(cluster)
    if batch.batch_digest != _digest_model(payload):
        raise ValueError("resolution batch digest does not match canonical content")


def canonical_resolution_batch_json(batch: ResolutionBatch) -> str:
    verify_resolution_batch(batch)
    return canonical_json(batch.model_dump(mode="json", by_alias=True)) + "\n"


def decode_resolution_batch(content: bytes) -> ResolutionBatch:
    batch = ResolutionBatch.model_validate_json(content)
    verify_resolution_batch(batch)
    if content != canonical_resolution_batch_json(batch).encode("utf-8"):
        raise ValueError("resolution batch bytes are not canonical")
    return batch


def seal_resolution_snapshot(payload: ResolutionSnapshotPayload) -> ResolutionSnapshot:
    return ResolutionSnapshot(
        **payload.model_dump(mode="python"),
        snapshot_digest=_digest_model(payload),
    )


def verify_resolution_snapshot(snapshot: ResolutionSnapshot) -> None:
    payload = ResolutionSnapshotPayload.model_validate(
        snapshot.model_dump(mode="python", exclude={"snapshot_digest"})
    )
    if snapshot.snapshot_digest != _digest_model(payload):
        raise ValueError("resolution snapshot digest does not match canonical content")


def canonical_resolution_snapshot_json(snapshot: ResolutionSnapshot) -> str:
    verify_resolution_snapshot(snapshot)
    return canonical_json(snapshot.model_dump(mode="json", by_alias=True)) + "\n"


def decode_resolution_snapshot(content: bytes) -> ResolutionSnapshot:
    snapshot = ResolutionSnapshot.model_validate_json(content)
    verify_resolution_snapshot(snapshot)
    if content != canonical_resolution_snapshot_json(snapshot).encode("utf-8"):
        raise ValueError("resolution snapshot bytes are not canonical")
    return snapshot


def _digest_model(model: BaseModel) -> str:
    return canonical_digest(model.model_dump(mode="json", by_alias=True))
