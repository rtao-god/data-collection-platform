from __future__ import annotations

import json
import os
import time
from typing import NoReturn, Protocol, cast

import boto3


class S3BootstrapClient(Protocol):
    def head_bucket(self, *, Bucket: str) -> object: ...

    def create_bucket(self, *, Bucket: str) -> object: ...


class ClientErrorLike(Exception):
    response: dict[str, object]


def main() -> int:
    bucket = _required("ARTIFACT_S3_BUCKET")
    client = cast(
        S3BootstrapClient,
        boto3.client(
            "s3",
            endpoint_url=_required("ARTIFACT_S3_ENDPOINT_URL"),
            aws_access_key_id=_required("ARTIFACT_S3_ACCESS_KEY_ID"),
            aws_secret_access_key=_required("ARTIFACT_S3_SECRET_ACCESS_KEY"),
            region_name=_required("ARTIFACT_S3_REGION"),
        ),
    )
    deadline = time.monotonic() + _positive_integer("OBJECT_STORE_BOOTSTRAP_TIMEOUT_SECONDS", 90)
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client.head_bucket(Bucket=bucket)
        except Exception as exc:
            last_error = exc
            try:
                client.create_bucket(Bucket=bucket)
            except Exception as create_error:
                last_error = create_error
                time.sleep(1)
                continue
        print(json.dumps({"bucket": bucket, "status": "ready"}, sort_keys=True))
        return 0
    error_type = type(last_error).__name__ if last_error is not None else "UnknownError"
    raise RuntimeError(f"object-store bucket bootstrap failed: {error_type}")


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        _configuration_error(name)
    return value.strip()


def _positive_integer(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        result = int(value)
    except ValueError:
        _configuration_error(name)
    if result <= 0:
        _configuration_error(name)
    return result


def _configuration_error(name: str) -> NoReturn:
    raise RuntimeError(f"required object-store bootstrap setting {name} is missing or invalid")


if __name__ == "__main__":
    raise SystemExit(main())
