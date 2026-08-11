from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from collection_application import (
    ArtifactKind,
    ArtifactTransferConflict,
    ArtifactTransferService,
    PrepareArtifactRead,
    PrepareArtifactUpload,
    PreparedArtifactRead,
    PreparedArtifactUpload,
    VerifiedArtifactUpload,
    VerifyArtifactUpload,
)
from collection_contracts import OwnerContextError

_UPLOAD_ID = UUID("019c0000-0000-7000-8000-000000000001")
_WORK_ID = UUID("019c0000-0000-7000-8000-000000000002")
_LEASE_ID = UUID("019c0000-0000-7000-8000-000000000003")
_LEASE_TOKEN = UUID("019c0000-0000-7000-8000-000000000004")
_ARTIFACT_ID = UUID("019c0000-0000-7000-8000-000000000005")
_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
_DIGEST = "sha256:" + ("a" * 64)


def _prepare_upload(**changes: object) -> PrepareArtifactUpload:
    values: dict[str, object] = {
        "upload_id": _UPLOAD_ID,
        "work_id": _WORK_ID,
        "lease_id": _LEASE_ID,
        "lease_token": _LEASE_TOKEN,
        "worker_id": "worker-1",
        "input_digest": _DIGEST,
        "artifact_kind": ArtifactKind.RAW_ARTIFACT,
        "expected_digest": _DIGEST,
        "expected_size_bytes": 128,
        "content_type": "text/html",
        "expires_in_seconds": 300,
        "correlation_id": "correlation-1",
    }
    values.update(changes)
    return PrepareArtifactUpload(**values)  # type: ignore[arg-type]


class FakePort:
    conflict: ArtifactTransferConflict | None = None

    def prepare_upload(self, command: PrepareArtifactUpload) -> PreparedArtifactUpload:
        if self.conflict is not None:
            raise self.conflict
        return PreparedArtifactUpload(
            upload_id=command.upload_id,
            method="PUT",
            url="https://object-store.invalid/upload",
            required_headers={
                "content-type": command.content_type,
                "x-amz-meta-sha256": command.expected_digest,
            },
            expires_at_utc=_NOW + timedelta(minutes=5),
        )

    def verify_upload(self, command: VerifyArtifactUpload) -> VerifiedArtifactUpload:
        del command
        raise AssertionError("verification was not expected")

    def prepare_read(self, command: PrepareArtifactRead) -> PreparedArtifactRead:
        del command
        raise AssertionError("read preparation was not expected")


def test_prepare_upload_requires_canonical_digest() -> None:
    with pytest.raises(ValueError, match="canonical SHA-256"):
        _prepare_upload(expected_digest="not-a-digest")


def test_prepare_upload_rejects_zero_and_oversized_body() -> None:
    with pytest.raises(ValueError, match="outside the supported"):
        _prepare_upload(expected_size_bytes=0)
    with pytest.raises(ValueError, match="outside the supported"):
        _prepare_upload(expected_size_bytes=(5 * 1024 * 1024 * 1024) + 1)


def test_prepare_upload_requires_bounded_expiry_and_content_type() -> None:
    with pytest.raises(ValueError, match="between 60 and 3600"):
        _prepare_upload(expires_in_seconds=30)
    with pytest.raises(ValueError, match="content type"):
        _prepare_upload(content_type="not a content type")


def test_prepared_upload_freezes_required_headers() -> None:
    headers = {"content-type": "text/html"}
    result = PreparedArtifactUpload(
        upload_id=_UPLOAD_ID,
        method="PUT",
        url="https://object-store.invalid/upload",
        required_headers=headers,
        expires_at_utc=_NOW + timedelta(minutes=5),
    )
    headers["content-type"] = "application/json"

    assert result.required_headers == {"content-type": "text/html"}
    with pytest.raises(TypeError):
        result.required_headers["content-type"] = "application/json"  # type: ignore[index]


def test_result_methods_are_not_tolerant_aliases() -> None:
    with pytest.raises(ValueError, match="must be PUT"):
        PreparedArtifactUpload(
            upload_id=_UPLOAD_ID,
            method="POST",
            url="https://object-store.invalid/upload",
            required_headers={},
            expires_at_utc=_NOW + timedelta(minutes=5),
        )
    with pytest.raises(ValueError, match="must be GET"):
        PreparedArtifactRead(
            artifact_id=_ARTIFACT_ID,
            method="POST",
            url="https://object-store.invalid/read",
            expires_at_utc=_NOW + timedelta(minutes=5),
        )


def test_transfer_conflict_becomes_artifact_owner_context() -> None:
    port = FakePort()
    port.conflict = ArtifactTransferConflict(
        code="ARTIFACT_LEASE_STALE",
        message="The upload no longer belongs to an active lease.",
        context={"uploadId": str(_UPLOAD_ID), "workId": str(_WORK_ID)},
        required_action="Discard the upload and acquire a new lease.",
    )

    with pytest.raises(OwnerContextError) as raised:
        ArtifactTransferService(port).prepare_upload(_prepare_upload())

    assert raised.value.envelope.owner == "ArtifactTransfer"
    assert raised.value.envelope.code == "ARTIFACT_LEASE_STALE"
    assert raised.value.envelope.correlation_id == "correlation-1"
    assert raised.value.envelope.context["uploadId"] == str(_UPLOAD_ID)
