"""An in-process S3 stand-in for integration suites that must not need object storage.

The suite exercises the real `S3ArtifactObjectStore`, the real presigning contract and the
real worker upload protocol. Only the S3 service itself is replaced: the client below keeps
objects in memory and the transport serves the presigned URLs it issued, so a test can follow
one artifact from a worker's upload through verification to the catalogue read without an
external endpoint.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import cast

import httpx
from botocore.exceptions import ClientError

from collection_infrastructure import S3ArtifactObjectStore

_BUCKET = "collector-integration"
_OBJECT_HOST = "https://object-store.invalid"


@dataclass(slots=True)
class StoredObject:
    content: bytes
    content_type: str
    metadata: dict[str, str] = field(default_factory=dict)


class Body:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._offset = 0

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._content) - self._offset
        chunk = self._content[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        return None


def _missing(key: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "NoSuchKey", "Message": f"missing {key}"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadObject",
    )


class FakeS3Client:
    """The subset of the boto3 S3 client the artifact store actually uses."""

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.presigned_methods: list[str] = []
        self.last_prepared_key: str | None = None

    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: Mapping[str, object],
        ExpiresIn: int,
        HttpMethod: str | None = None,
    ) -> str:
        del ExpiresIn, HttpMethod
        self.presigned_methods.append(client_method)
        key = str(Params["Key"])
        if client_method == "put_object" or client_method == "get_object":
            self.last_prepared_key = key
        return f"{_OBJECT_HOST}/{key}"

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        content = body if isinstance(body, bytes) else bytes(body)  # type: ignore[arg-type]
        metadata = cast("Mapping[str, str]", kwargs.get("Metadata") or {})
        self.objects[key] = StoredObject(
            content=content,
            content_type=str(kwargs.get("ContentType") or "application/octet-stream"),
            metadata=dict(metadata),
        )
        return {}

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        key = str(kwargs["Key"])
        stored = self.objects.get(key)
        if stored is None:
            raise _missing(key)
        return {
            "Body": Body(stored.content),
            "ContentLength": len(stored.content),
            "ContentType": stored.content_type,
            "Metadata": dict(stored.metadata),
        }

    def head_bucket(self, **kwargs: object) -> Mapping[str, object]:
        del kwargs
        return {}

    def head_object(self, **kwargs: object) -> Mapping[str, object]:
        key = str(kwargs["Key"])
        stored = self.objects.get(key)
        if stored is None:
            raise _missing(key)
        return {
            "ContentLength": len(stored.content),
            "ContentType": stored.content_type,
            "Metadata": dict(stored.metadata),
        }

    def copy_object(self, **kwargs: object) -> Mapping[str, object]:
        target_key = str(kwargs["Key"])
        source = cast("Mapping[str, object]", kwargs["CopySource"])
        stored = self.objects[str(source["Key"])]
        metadata = cast("Mapping[str, str]", kwargs["Metadata"])
        self.objects[target_key] = StoredObject(
            content=stored.content,
            content_type=str(kwargs["ContentType"]),
            metadata=dict(metadata),
        )
        return {}

    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        self.objects.pop(str(kwargs["Key"]), None)
        return {}


class S3Transport(httpx.BaseTransport):
    """Serves the presigned URLs the fake client issues, for the worker upload protocol."""

    def __init__(self, client: FakeS3Client) -> None:
        self._client = client
        self.uploads: list[str] = []
        self.reads: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        if request.method == "PUT":
            content = request.read()
            self.uploads.append(key)
            content_type = request.headers.get("content-type") or "application/octet-stream"
            self._client.objects[key] = StoredObject(
                content=content,
                content_type=content_type,
                metadata={"sha256": sha256(content).hexdigest()},
            )
            return httpx.Response(200, request=request)
        if request.method == "GET":
            self.reads.append(key)
            stored = self._client.objects.get(key)
            if stored is None:
                return httpx.Response(404, request=request)
            return httpx.Response(
                200,
                headers={"content-type": stored.content_type},
                content=stored.content,
                request=request,
            )
        return httpx.Response(404, request=request)


def object_store(client: FakeS3Client) -> S3ArtifactObjectStore:
    return S3ArtifactObjectStore(client, bucket=_BUCKET)  # type: ignore[arg-type]
