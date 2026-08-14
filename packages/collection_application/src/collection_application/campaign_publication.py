from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid5

from collection_application.artifacts import ArtifactKind
from collection_application.campaign_snapshot_service import CampaignSnapshotService
from collection_application.compiled_campaign import CompiledCampaignBundle
from collection_application.owned_artifacts import (
    OwnedArtifactPublisherService,
    PublishOwnedArtifact,
)
from collection_contracts import CampaignSnapshot

_PUBLICATION_NAMESPACE = UUID("811259da-3329-57e3-a776-a3072d30fa48")


@dataclass(frozen=True, slots=True)
class PublishCampaignSnapshot:
    campaign_key: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class PublishedCampaignSnapshot:
    compiled: CompiledCampaignBundle
    artifact_id: UUID
    recorded_at_utc: datetime

    def __post_init__(self) -> None:
        if self.recorded_at_utc.tzinfo is None or self.recorded_at_utc.utcoffset() != timedelta(0):
            raise ValueError("campaign snapshot publication timestamp must be UTC")


class CampaignSnapshotStore(Protocol):
    def publish(
        self,
        snapshot: CampaignSnapshot,
        *,
        artifact_id: UUID,
        recorded_at_utc: datetime,
        correlation_id: str,
    ) -> None: ...


class CampaignSnapshotPublicationService:
    def __init__(
        self,
        compiler: CampaignSnapshotService,
        artifact_publisher: OwnedArtifactPublisherService,
        store: CampaignSnapshotStore,
    ) -> None:
        self._compiler = compiler
        self._artifact_publisher = artifact_publisher
        self._store = store

    def publish(self, command: PublishCampaignSnapshot) -> PublishedCampaignSnapshot:
        compiled = self._compiler.compile(command.campaign_key, command.correlation_id)
        snapshot = compiled.snapshot
        artifact_id = uuid5(_PUBLICATION_NAMESPACE, f"artifact:{snapshot.bundle_digest}")
        operation_id = uuid5(_PUBLICATION_NAMESPACE, f"operation:{snapshot.bundle_digest}")
        published = self._artifact_publisher.publish(
            PublishOwnedArtifact(
                artifact_id=artifact_id,
                operation_id=operation_id,
                producer_identity="campaign-snapshot",
                artifact_kind=ArtifactKind.CONFIG_BUNDLE,
                content=compiled.canonical_content,
                content_type="application/vnd.collection.campaign-bundle+json",
                source_policy_digest=None,
                correlation_id=command.correlation_id,
            )
        )
        expected_digest = f"sha256:{sha256(compiled.canonical_content).hexdigest()}"
        if (
            published.content_digest != snapshot.bundle_digest
            or expected_digest != snapshot.bundle_digest
        ):
            raise RuntimeError("campaign snapshot artifact digest diverges from bundle identity")
        self._store.publish(
            snapshot,
            artifact_id=published.artifact_id,
            recorded_at_utc=published.recorded_at_utc,
            correlation_id=command.correlation_id,
        )
        return PublishedCampaignSnapshot(
            compiled=compiled,
            artifact_id=published.artifact_id,
            recorded_at_utc=published.recorded_at_utc,
        )
