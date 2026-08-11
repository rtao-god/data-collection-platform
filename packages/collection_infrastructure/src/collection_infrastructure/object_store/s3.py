from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Protocol, cast
from uuid import UUID

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from collection_application import ArtifactKind

_CHUNK_BYTES = 1024 * 1024


class StreamingBody(Protocol):
    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


class S3Client(Protocol):
    def generate_presigned_url(
        self,
        client_method: str,
        *,
        Params: Mapping[str, object],
        ExpiresIn: int,
        HttpMethod: str | None = None,
    ) -> str: ...

    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def head_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def copy_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def delete_object(self, **kwargs: object) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class PreparedObjectUpload:
    staging_reference: str
    url: str
    required_headers: Mapping[str, str]
    expires_at_utc: datetime


@dataclass(frozen=True, slots=True)
class VerifiedObject:
    staging_reference: str
    final_reference: str
    content_digest: str
    size_bytes: int
    content_type: str
    verified_at_utc: datetime


@dataclass(frozen=True, slots=True)
class PreparedObjectRead:
    url: str
    expires_at_utc: datetime


class ArtifactObjectStoreError(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: Mapping[str, object],
        required_action: str,
    ) -> None:
        self.code = code
        self.message = message
        self.context = dict(context)
        self.required_action = required_action
        super().__init__(message)


class S3ArtifactObjectStore:
    """Streams and verifies artifacts before exposing content-addressed S3 keys."""

    def __init__(self, client: S3Client, *, bucket: str) -> None:
        if not bucket or len(bucket) > 255:
            raise ValueError("artifact bucket has an invalid format")
        self._client = client
        self._bucket = bucket

    @classmethod
    def create(
        cls,
        *,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region_name: str,
    ) -> S3ArtifactObjectStore:
        if not endpoint_url.startswith(("http://", "https://")):
            raise ValueError("S3 endpoint URL must use HTTP or HTTPS")
        if not access_key_id or not secret_access_key or not region_name:
            raise ValueError("S3 credentials and region are required")
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region_name,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return cls(cast(S3Client, client), bucket=bucket)

    def prepare_upload(
        self,
        *,
        upload_id: UUID,
        artifact_kind: ArtifactKind,
        expected_digest: str,
        expected_size_bytes: int,
        content_type: str,
        expires_in_seconds: int,
        now_utc: datetime,
    ) -> PreparedObjectUpload:
        staging_reference = _staging_reference(artifact_kind, upload_id)
        digest_hex = expected_digest.removeprefix("sha256:")
        parameters: dict[str, object] = {
            "Bucket": self._bucket,
            "Key": staging_reference,
            "ContentType": content_type,
            "ContentLength": expected_size_bytes,
            "Metadata": {"sha256": digest_hex},
        }
        try:
            url = self._client.generate_presigned_url(
                "put_object",
                Params=parameters,
                ExpiresIn=expires_in_seconds,
                HttpMethod="PUT",
            )
        except Exception as exc:
            raise ArtifactObjectStoreError(
                code="ARTIFACT_UPLOAD_PREPARATION_FAILED",
                message="The object store could not prepare the artifact upload.",
                context={"causeType": type(exc).__name__},
                required_action="Restore the object store and retry the exact upload request.",
            ) from exc
        return PreparedObjectUpload(
            staging_reference=staging_reference,
            url=url,
            required_headers={
                "content-length": str(expected_size_bytes),
                "content-type": content_type,
                "x-amz-meta-sha256": digest_hex,
            },
            expires_at_utc=now_utc + timedelta(seconds=expires_in_seconds),
        )

    def verify_and_promote(
        self,
        *,
        staging_reference: str,
        artifact_kind: ArtifactKind,
        expected_digest: str,
        expected_size_bytes: int,
        expected_content_type: str,
        now_utc: datetime,
    ) -> VerifiedObject:
        response = self._get_staging(staging_reference)
        content_length = _required_int(response, "ContentLength")
        content_type = _required_string(response, "ContentType")
        metadata = response.get("Metadata")
        if not isinstance(metadata, Mapping):
            raise _integrity_error(staging_reference, "metadata_missing")
        metadata_digest = metadata.get("sha256")
        if metadata_digest != expected_digest.removeprefix("sha256:"):
            raise _integrity_error(staging_reference, "metadata_digest_mismatch")
        if content_length != expected_size_bytes:
            raise _integrity_error(staging_reference, "size_mismatch")
        if content_type != expected_content_type:
            raise _integrity_error(staging_reference, "content_type_mismatch")
        body = response.get("Body")
        if body is None or not hasattr(body, "read") or not hasattr(body, "close"):
            raise _integrity_error(staging_reference, "body_missing")
        digest, streamed_size = _stream_digest(cast(StreamingBody, body))
        if streamed_size != expected_size_bytes:
            raise _integrity_error(staging_reference, "streamed_size_mismatch")
        if digest != expected_digest:
            raise _integrity_error(staging_reference, "content_digest_mismatch")

        final_reference = _final_reference(artifact_kind, expected_digest)
        if not self._verified_final_exists(
            final_reference=final_reference,
            expected_digest=expected_digest,
            expected_size_bytes=expected_size_bytes,
        ):
            try:
                self._client.copy_object(
                    Bucket=self._bucket,
                    Key=final_reference,
                    CopySource={"Bucket": self._bucket, "Key": staging_reference},
                    MetadataDirective="REPLACE",
                    Metadata={"sha256": expected_digest.removeprefix("sha256:")},
                    ContentType=expected_content_type,
                )
            except Exception as exc:
                raise ArtifactObjectStoreError(
                    code="ARTIFACT_PROMOTION_FAILED",
                    message="The verified artifact could not be promoted to its content key.",
                    context={
                        "stagingReference": staging_reference,
                        "finalReference": final_reference,
                        "causeType": type(exc).__name__,
                    },
                    required_action=(
                        "Restore object-store copy support and retry verification of the exact upload."
                    ),
                ) from exc
            if not self._verified_final_exists(
                final_reference=final_reference,
                expected_digest=expected_digest,
                expected_size_bytes=expected_size_bytes,
            ):
                raise ArtifactObjectStoreError(
                    code="ARTIFACT_PROMOTION_UNVERIFIED",
                    message="The promoted artifact does not satisfy its content identity.",
                    context={"finalReference": final_reference},
                    required_action="Inspect the object store before retrying artifact verification.",
                )
        try:
            self._client.delete_object(Bucket=self._bucket, Key=staging_reference)
        except Exception as exc:
            raise ArtifactObjectStoreError(
                code="ARTIFACT_STAGING_CLEANUP_FAILED",
                message="The verified artifact was promoted but its staging object remains.",
                context={
                    "stagingReference": staging_reference,
                    "finalReference": final_reference,
                    "causeType": type(exc).__name__,
                },
                required_action=(
                    "Retry verification or remove only this staging object through the artifact owner."
                ),
            ) from exc
        return VerifiedObject(
            staging_reference=staging_reference,
            final_reference=final_reference,
            content_digest=expected_digest,
            size_bytes=expected_size_bytes,
            content_type=expected_content_type,
            verified_at_utc=now_utc,
        )

    def prepare_read(
        self,
        *,
        storage_reference: str,
        expires_in_seconds: int,
        now_utc: datetime,
    ) -> PreparedObjectRead:
        try:
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": storage_reference},
                ExpiresIn=expires_in_seconds,
                HttpMethod="GET",
            )
        except Exception as exc:
            raise ArtifactObjectStoreError(
                code="ARTIFACT_READ_PREPARATION_FAILED",
                message="The object store could not prepare the scoped artifact read.",
                context={
                    "storageReference": storage_reference,
                    "causeType": type(exc).__name__,
                },
                required_action="Restore the object store and retry the exact read request.",
            ) from exc
        return PreparedObjectRead(
            url=url,
            expires_at_utc=now_utc + timedelta(seconds=expires_in_seconds),
        )

    def _get_staging(self, staging_reference: str) -> Mapping[str, object]:
        try:
            return self._client.get_object(Bucket=self._bucket, Key=staging_reference)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "unknown"))
            raise ArtifactObjectStoreError(
                code="ARTIFACT_UPLOAD_NOT_FOUND",
                message="The prepared artifact upload is not available for verification.",
                context={"stagingReference": staging_reference, "storageCode": code},
                required_action="Upload the exact body to the prepared URL before verification.",
            ) from exc
        except Exception as exc:
            raise ArtifactObjectStoreError(
                code="ARTIFACT_UPLOAD_READ_FAILED",
                message="The object store could not read the prepared artifact upload.",
                context={
                    "stagingReference": staging_reference,
                    "causeType": type(exc).__name__,
                },
                required_action="Restore the object store and retry artifact verification.",
            ) from exc

    def _verified_final_exists(
        self,
        *,
        final_reference: str,
        expected_digest: str,
        expected_size_bytes: int,
    ) -> bool:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=final_reference)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = str(exc.response.get("Error", {}).get("Code", "unknown"))
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise ArtifactObjectStoreError(
                code="ARTIFACT_FINAL_HEAD_FAILED",
                message="The content-addressed artifact could not be inspected.",
                context={"finalReference": final_reference, "storageCode": code},
                required_action="Restore the object store and retry artifact verification.",
            ) from exc
        except Exception as exc:
            raise ArtifactObjectStoreError(
                code="ARTIFACT_FINAL_HEAD_FAILED",
                message="The content-addressed artifact could not be inspected.",
                context={
                    "finalReference": final_reference,
                    "causeType": type(exc).__name__,
                },
                required_action="Restore the object store and retry artifact verification.",
            ) from exc
        metadata = response.get("Metadata")
        return (
            _required_int(response, "ContentLength") == expected_size_bytes
            and isinstance(metadata, Mapping)
            and metadata.get("sha256") == expected_digest.removeprefix("sha256:")
        )


def _staging_reference(artifact_kind: ArtifactKind, upload_id: UUID) -> str:
    namespace = _namespace(artifact_kind)
    return f"{namespace}/staging/{upload_id}"


def _final_reference(artifact_kind: ArtifactKind, digest: str) -> str:
    namespace = _namespace(artifact_kind)
    value = digest.removeprefix("sha256:")
    return f"{namespace}/sha256/{value[:2]}/{value[2:4]}/{value}"


def _namespace(artifact_kind: ArtifactKind) -> str:
    if artifact_kind is ArtifactKind.RAW_ARTIFACT:
        return "raw-artifacts"
    if artifact_kind is ArtifactKind.DIAGNOSTIC_ARTIFACT:
        return "diagnostic-artifacts"
    raise ValueError(f"unsupported artifact kind: {artifact_kind}")


def _stream_digest(body: StreamingBody) -> tuple[str, int]:
    digest = sha256()
    size = 0
    try:
        while True:
            chunk = body.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    finally:
        body.close()
    return f"sha256:{digest.hexdigest()}", size


def _required_int(response: Mapping[str, object], key: str) -> int:
    value = response.get(key)
    if not isinstance(value, int):
        raise ValueError(f"S3 response {key} must be an integer")
    return value


def _required_string(response: Mapping[str, object], key: str) -> str:
    value = response.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"S3 response {key} must be a non-empty string")
    return value


def _integrity_error(staging_reference: str, reason: str) -> ArtifactObjectStoreError:
    return ArtifactObjectStoreError(
        code="ARTIFACT_INTEGRITY_FAILED",
        message="The uploaded artifact does not match its declared content identity.",
        context={"stagingReference": staging_reference, "reason": reason},
        required_action="Discard the upload and prepare a new upload for the exact body.",
    )
