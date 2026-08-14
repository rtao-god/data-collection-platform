from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from collection_contracts import CampaignSnapshot
from collection_infrastructure.postgres.metadata import (
    config_bundle_artifacts,
    config_bundle_blockers,
    config_bundle_components,
    config_bundles,
)


class CampaignSnapshotStorageConflict(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: Mapping[str, object],
        required_action: str,
    ) -> None:
        self.code = code
        self.context = dict(context)
        self.required_action = required_action
        super().__init__(message)


class PostgresCampaignSnapshotStore:
    """Seals one snapshot metadata graph around its exact verified artifact."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def publish(
        self,
        snapshot: CampaignSnapshot,
        *,
        artifact_id: UUID,
        recorded_at_utc: datetime,
        correlation_id: str,
    ) -> None:
        try:
            with self._engine.begin() as connection:
                self._publish(
                    connection,
                    snapshot,
                    artifact_id=artifact_id,
                    recorded_at_utc=recorded_at_utc,
                    correlation_id=correlation_id,
                )
        except CampaignSnapshotStorageConflict:
            raise
        except SQLAlchemyError as exc:
            raise CampaignSnapshotStorageConflict(
                code="CAMPAIGN_SNAPSHOT_STORAGE_FAILED",
                message="The campaign snapshot metadata transaction did not complete.",
                context={"causeType": type(exc).__name__},
                required_action="Inspect the immutable config tables and retry the exact snapshot.",
            ) from exc

    def _publish(
        self,
        connection: Connection,
        snapshot: CampaignSnapshot,
        *,
        artifact_id: UUID,
        recorded_at_utc: datetime,
        correlation_id: str,
    ) -> None:
        connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": snapshot.bundle_digest},
        )
        existing = (
            connection.execute(
                sa.select(config_bundles).where(
                    config_bundles.c.bundle_digest == snapshot.bundle_digest
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            self._require_existing(
                connection,
                snapshot,
                artifact_id=artifact_id,
                existing=existing,
            )
            return
        connection.execute(
            sa.insert(config_bundle_components),
            [
                {
                    "bundle_digest": snapshot.bundle_digest,
                    "position": position,
                    "path": component.path,
                    "component_digest": component.digest,
                }
                for position, component in enumerate(snapshot.components)
            ],
        )
        if snapshot.blockers:
            connection.execute(
                sa.insert(config_bundle_blockers),
                [
                    {
                        "bundle_digest": snapshot.bundle_digest,
                        "position": position,
                        "code": blocker.code,
                        "owner": blocker.owner,
                        "message": blocker.message,
                        "required_action": blocker.required_action,
                    }
                    for position, blocker in enumerate(snapshot.blockers)
                ],
            )
        connection.execute(
            sa.insert(config_bundle_artifacts).values(
                bundle_digest=snapshot.bundle_digest,
                artifact_id=artifact_id,
                recorded_at_utc=recorded_at_utc,
                correlation_id=correlation_id,
            )
        )
        connection.execute(
            sa.insert(config_bundles).values(
                bundle_digest=snapshot.bundle_digest,
                campaign_key=snapshot.campaign_key,
                contract=snapshot.contract,
                contract_revision=snapshot.contract_revision,
                readiness=snapshot.readiness,
                recorded_at_utc=recorded_at_utc,
            )
        )

    def _require_existing(
        self,
        connection: Connection,
        snapshot: CampaignSnapshot,
        *,
        artifact_id: UUID,
        existing: RowMapping,
    ) -> None:
        mismatches: list[str] = []
        expected = {
            "campaign_key": snapshot.campaign_key,
            "contract": snapshot.contract,
            "contract_revision": snapshot.contract_revision,
            "readiness": snapshot.readiness,
        }
        for key, value in expected.items():
            if existing[key] != value:
                mismatches.append(key)
        components = tuple(
            connection.execute(
                sa.select(
                    config_bundle_components.c.path,
                    config_bundle_components.c.component_digest,
                )
                .where(config_bundle_components.c.bundle_digest == snapshot.bundle_digest)
                .order_by(config_bundle_components.c.position)
            ).all()
        )
        expected_components = tuple((item.path, item.digest) for item in snapshot.components)
        if components != expected_components:
            mismatches.append("components")
        blockers = tuple(
            connection.execute(
                sa.select(
                    config_bundle_blockers.c.code,
                    config_bundle_blockers.c.owner,
                    config_bundle_blockers.c.message,
                    config_bundle_blockers.c.required_action,
                )
                .where(config_bundle_blockers.c.bundle_digest == snapshot.bundle_digest)
                .order_by(config_bundle_blockers.c.position)
            ).all()
        )
        expected_blockers = tuple(
            (item.code, item.owner, item.message, item.required_action)
            for item in snapshot.blockers
        )
        if blockers != expected_blockers:
            mismatches.append("blockers")
        existing_artifact = connection.execute(
            sa.select(config_bundle_artifacts.c.artifact_id).where(
                config_bundle_artifacts.c.bundle_digest == snapshot.bundle_digest
            )
        ).scalar_one_or_none()
        if existing_artifact != artifact_id:
            mismatches.append("artifact_id")
        if mismatches:
            raise CampaignSnapshotStorageConflict(
                code="CAMPAIGN_SNAPSHOT_IDENTITY_CONFLICT",
                message="The snapshot digest is already bound to different immutable metadata.",
                context={
                    "bundleDigest": snapshot.bundle_digest,
                    "mismatches": mismatches,
                },
                required_action="Publish a new snapshot identity for changed campaign content.",
            )
