from __future__ import annotations

import pytest

from collection_infrastructure.artifact_cleanup_object_store import (
    S3ArtifactCleanupObjectStore,
)


class Client:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def delete_object(self, *, Bucket: str, Key: str) -> object:
        self.deleted.append((Bucket, Key))
        return {}


def store(client: Client) -> S3ArtifactCleanupObjectStore:
    return S3ArtifactCleanupObjectStore(
        bucket="collection",
        endpoint_url="http://object-store:8333",
        access_key_id="key",
        secret_access_key="secret",
        region="us-east-1",
        client=client,
    )


@pytest.mark.parametrize(
    ("reference", "expected_key"),
    [
        ("staging/upload-1", "staging/upload-1"),
        ("collection/staging/upload-1", "staging/upload-1"),
        ("s3://collection/staging/upload-1", "staging/upload-1"),
    ],
)
def test_cleanup_deletes_only_the_configured_bucket(
    reference: str,
    expected_key: str,
) -> None:
    client = Client()
    store(client).delete(reference)
    assert client.deleted == [("collection", expected_key)]


@pytest.mark.parametrize(
    "reference",
    [
        "s3://other/staging/upload-1",
        "https://example.test/object",
        "../upload-1",
        "staging//upload-1",
    ],
)
def test_cleanup_rejects_unsafe_storage_references(reference: str) -> None:
    with pytest.raises(ValueError):
        store(Client()).delete(reference)
