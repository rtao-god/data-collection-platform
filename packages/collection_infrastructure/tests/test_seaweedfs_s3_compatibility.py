from __future__ import annotations

import os
from hashlib import sha256
from uuid import uuid4

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError

from collection_infrastructure.artifact_cleanup_object_store import (
    S3ArtifactCleanupObjectStore,
)

pytestmark = pytest.mark.object_store_integration


def test_seaweedfs_supports_platform_object_lifecycle() -> None:
    endpoint = _required("COLLECTOR_TEST_S3_ENDPOINT_URL")
    bucket = _required("COLLECTOR_TEST_S3_BUCKET")
    access_key = _required("COLLECTOR_TEST_S3_ACCESS_KEY_ID")
    secret_key = _required("COLLECTOR_TEST_S3_SECRET_ACCESS_KEY")
    region = _required("COLLECTOR_TEST_S3_REGION")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )
    _ensure_bucket(client, bucket)

    body = b"data-collection-platform-seaweedfs-compatibility"
    digest = sha256(body).hexdigest()
    prefix = f"compatibility/{uuid4()}"
    staging_key = f"{prefix}/staging"
    promoted_key = f"objects/sha256/{digest[:2]}/{digest}"
    try:
        put_url = client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": staging_key,
                "ContentType": "application/octet-stream",
            },
            ExpiresIn=300,
            HttpMethod="PUT",
        )
        put_response = httpx.put(
            put_url,
            content=body,
            headers={"Content-Type": "application/octet-stream"},
            timeout=30,
            follow_redirects=False,
        )
        put_response.raise_for_status()

        streamed = client.get_object(Bucket=bucket, Key=staging_key)["Body"]
        observed = sha256()
        while chunk := streamed.read(8):
            observed.update(chunk)
        assert observed.hexdigest() == digest

        client.copy_object(
            Bucket=bucket,
            Key=promoted_key,
            CopySource={"Bucket": bucket, "Key": staging_key},
            ContentType="application/octet-stream",
            MetadataDirective="REPLACE",
        )
        read_url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": promoted_key},
            ExpiresIn=300,
            HttpMethod="GET",
        )
        read_response = httpx.get(read_url, timeout=30, follow_redirects=False)
        read_response.raise_for_status()
        assert read_response.content == body

        S3ArtifactCleanupObjectStore(
            bucket=bucket,
            endpoint_url=endpoint,
            access_key_id=access_key,
            secret_access_key=secret_key,
            region=region,
            client=client,
        ).delete(staging_key)
        with pytest.raises(ClientError) as deleted:
            client.head_object(Bucket=bucket, Key=staging_key)
        assert deleted.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
        assert client.get_object(Bucket=bucket, Key=promoted_key)["Body"].read() == body
    finally:
        client.delete_object(Bucket=bucket, Key=staging_key)
        client.delete_object(Bucket=bucket, Key=promoted_key)


def _ensure_bucket(client: object, bucket: str) -> None:
    s3 = client
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        status = exc.response["ResponseMetadata"]["HTTPStatusCode"]
        if status != 404:
            raise
        s3.create_bucket(Bucket=bucket)


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value:
        raise RuntimeError(f"required SeaweedFS test setting {name} is missing")
    return value
