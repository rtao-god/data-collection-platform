from __future__ import annotations

from typing import Protocol, cast
from urllib.parse import unquote, urlsplit

import boto3


class S3DeleteClient(Protocol):
    def delete_object(self, *, Bucket: str, Key: str) -> object: ...


class S3ArtifactCleanupObjectStore:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        client: S3DeleteClient | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("artifact cleanup bucket is required")
        self._bucket = bucket
        if client is None:
            client = cast(
                S3DeleteClient,
                boto3.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                    region_name=region,
                ),
            )
        self._client = client

    def delete(self, storage_reference: str) -> None:
        key = _object_key(storage_reference, expected_bucket=self._bucket)
        self._client.delete_object(Bucket=self._bucket, Key=key)


def _object_key(storage_reference: str, *, expected_bucket: str) -> str:
    if not storage_reference or len(storage_reference) > 1_024:
        raise ValueError("artifact cleanup storage reference is invalid")
    parsed = urlsplit(storage_reference)
    if parsed.scheme:
        if parsed.scheme != "s3":
            raise ValueError("artifact cleanup supports only S3 storage references")
        if parsed.netloc != expected_bucket:
            raise ValueError("artifact cleanup storage reference names another bucket")
        key = unquote(parsed.path.lstrip("/"))
    else:
        key = storage_reference.lstrip("/")
        bucket_prefix = f"{expected_bucket}/"
        if key.startswith(bucket_prefix):
            key = key[len(bucket_prefix) :]
    if not key or key in {".", ".."} or "\x00" in key:
        raise ValueError("artifact cleanup object key is invalid")
    if any(part in {"", ".", ".."} for part in key.split("/")):
        raise ValueError("artifact cleanup object key contains an invalid segment")
    return key
