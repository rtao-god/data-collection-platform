from __future__ import annotations

from uuid import UUID

import pytest

from collection_application import ArtifactKind, PrepareArtifactUpload, PublishOwnedArtifact


def test_worker_cannot_publish_owner_controlled_artifact_kind() -> None:
    with pytest.raises(ValueError, match="owner-controlled"):
        PrepareArtifactUpload(
            upload_id=UUID("00000000-0000-0000-0000-000000000001"),
            work_id=UUID("00000000-0000-0000-0000-000000000002"),
            lease_id=UUID("00000000-0000-0000-0000-000000000003"),
            lease_token=UUID("00000000-0000-0000-0000-000000000004"),
            worker_id="worker-1",
            input_digest="sha256:" + "1" * 64,
            artifact_kind=ArtifactKind.CONFIG_BUNDLE,
            expected_digest="sha256:" + "2" * 64,
            expected_size_bytes=1,
            content_type="application/json",
            expires_in_seconds=300,
            correlation_id="correlation-1",
        )


def test_control_plane_publish_contract_accepts_config_and_export_artifacts() -> None:
    for kind in (ArtifactKind.CONFIG_BUNDLE, ArtifactKind.EXPORT_ARTIFACT):
        command = PublishOwnedArtifact(
            artifact_id=UUID("00000000-0000-0000-0000-000000000005"),
            operation_id=UUID("00000000-0000-0000-0000-000000000006"),
            producer_identity="control-api",
            artifact_kind=kind,
            content=b"{}",
            content_type="application/json",
            source_policy_digest=None,
            correlation_id="correlation-1",
        )
        assert command.artifact_kind is kind
