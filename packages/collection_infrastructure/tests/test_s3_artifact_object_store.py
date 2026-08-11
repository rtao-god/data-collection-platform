from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast
from uuid import UUID

import pytest
from botocore.exceptions import ClientError

from collection_application import ArtifactKind
from collection_infrastructure.object_store.s3 import (
    ArtifactObjectStoreError,
    S3ArtifactObjectStore,
    S3Client,
)

_UPLOAD_ID = UUID("019c0000-0000-7000-8000-000000000001")
_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
_BODY = b"verified artifact body"
_DIGEST = f"sha256:{sha256(_BODY).hexdigest()}"


class Body:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._offset = 0
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._content) - self._offset
        chunk = self._content[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class StoredObject:
    content: bytes
    content_type: str
    metadata: dict[str, str]


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.presign_calls: list[tuple[str, Mapping[str, object], int, str | None]] = []
        self.copy_calls: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: Mapping[str, object],
        ExpiresIn: int,
        HttpMethod: str | None = None,
    ) -> str:
        self.presign_calls.append((client_method, Params, ExpiresIn, HttpMethod))
        return f"https://object-store.invalid/{Params['Key']}"

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        key = str(kwargs["Key"])
        stored = self.objects.get(key)
        if stored is None:
            raise _missing_key(key)
        return {
            "Body": Body(stored.content),
            "ContentLength": len(stored.content),
            "ContentType": stored.content_type,
            "Metadata": dict(stored.metadata),
        }

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        key = str(kwargs["Key"])
        stored = self.objects.get(key)
        if stored is None:
            raise _missing_key(key)
        return {
            "ContentLength": len(stored.content),
            "ContentType": stored.content_type,
            "Metadata": dict(stored.metadata),
        }

    def copy_object(self, **kwargs: object) -> Mapping[str, object]:
        key = str(kwargs["Key"])
        source = cast(Mapping[str, object], kwargs["CopySource"])
        source_key = str(source["Key"])
        stored = self.objects[source_key]
        metadata = cast(Mapping[str, str], kwargs["Metadata"])
        self.objects[key] = StoredObject(
            content=stored.content,
            content_type=str(kwargs["ContentType"]),
            metadata=dict(metadata),
        )
        self.copy_calls.append((source_key, key))
        return {}

    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        key = str(kwargs["Key"])
        self.objects.pop(key, None)
        self.deleted.append(key)
        return {}


def _store(client: FakeS3Client) -> S3ArtifactObjectStore:
    return S3ArtifactObjectStore(cast(S3Client, client), bucket="collector-artifacts")


def _staging_key() -> str:
    return f"raw-artifacts/staging/{_UPLOAD_ID}"


def _final_key() -> str:
    value = _DIGEST.removeprefix("sha256:")
    return f"raw-artifacts/sha256/{value[:2]}/{value[2:4]}/{value}"


def _uploaded(content: bytes = _BODY, digest: str = _DIGEST) -> StoredObject:
    return StoredObject(
        content=content,
        content_type="text/html",
        metadata={"sha256": digest.removeprefix("sha256:")},
    )


def test_prepare_upload_signs_exact_content_identity() -> None:
    client = FakeS3Client()

    prepared = _store(client).prepare_upload(
        upload_id=_UPLOAD_ID,
        artifact_kind=ArtifactKind.RAW_ARTIFACT,
        expected_digest=_DIGEST,
        expected_size_bytes=len(_BODY),
        content_type="text/html",
        expires_in_seconds=300,
        now_utc=_NOW,
    )

    assert prepared.staging_reference == _staging_key()
    assert prepared.required_headers == {
        "content-length": str(len(_BODY)),
        "content-type": "text/html",
        "x-amz-meta-sha256": _DIGEST.removeprefix("sha256:"),
    }
    method, parameters, expiry, http_method = client.presign_calls[0]
    assert method == "put_object"
    assert parameters["Key"] == _staging_key()
    assert parameters["ContentLength"] == len(_BODY)
    assert expiry == 300
    assert http_method == "PUT"


def test_verify_streams_promotes_checks_and_deletes_staging() -> None:
    client = FakeS3Client()
    client.objects[_staging_key()] = _uploaded()

    verified = _store(client).verify_and_promote(
        staging_reference=_staging_key(),
        artifact_kind=ArtifactKind.RAW_ARTIFACT,
        expected_digest=_DIGEST,
        expected_size_bytes=len(_BODY),
        expected_content_type="text/html",
        now_utc=_NOW,
    )

    assert verified.final_reference == _final_key()
    assert verified.content_digest == _DIGEST
    assert verified.size_bytes == len(_BODY)
    assert client.copy_calls == [(_staging_key(), _final_key())]
    assert client.deleted == [_staging_key()]
    assert client.objects[_final_key()].content == _BODY
    assert client.objects[_final_key()].metadata == {
        "sha256": _DIGEST.removeprefix("sha256:")
    }


def test_digest_mismatch_never_promotes_or_deletes() -> None:
    client = FakeS3Client()
    client.objects[_staging_key()] = _uploaded(content=b"different bytes")

    with pytest.raises(ArtifactObjectStoreError) as raised:
        _store(client).verify_and_promote(
            staging_reference=_staging_key(),
            artifact_kind=ArtifactKind.RAW_ARTIFACT,
            expected_digest=_DIGEST,
            expected_size_bytes=len(b"different bytes"),
            expected_content_type="text/html",
            now_utc=_NOW,
        )

    assert raised.value.code == "ARTIFACT_INTEGRITY_FAILED"
    assert raised.value.context["reason"] == "content_digest_mismatch"
    assert client.copy_calls == []
    assert client.deleted == []
    assert _staging_key() in client.objects


def test_existing_verified_content_is_reused_without_copy() -> None:
    client = FakeS3Client()
    client.objects[_staging_key()] = _uploaded()
    client.objects[_final_key()] = _uploaded()

    _store(client).verify_and_promote(
        staging_reference=_staging_key(),
        artifact_kind=ArtifactKind.RAW_ARTIFACT,
        expected_digest=_DIGEST,
        expected_size_bytes=len(_BODY),
        expected_content_type="text/html",
        now_utc=_NOW,
    )

    assert client.copy_calls == []
    assert client.deleted == [_staging_key()]


def test_prepare_read_signs_only_the_exact_storage_reference() -> None:
    client = FakeS3Client()

    prepared = _store(client).prepare_read(
        storage_reference=_final_key(),
        expires_in_seconds=120,
        now_utc=_NOW,
    )

    assert prepared.url.endswith(_final_key())
    method, parameters, expiry, http_method = client.presign_calls[0]
    assert method == "get_object"
    assert parameters == {"Bucket": "collector-artifacts", "Key": _final_key()}
    assert expiry == 120
    assert http_method == "GET"


def _missing_key(key: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "NoSuchKey", "Message": f"missing {key}"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadObject",
    )
