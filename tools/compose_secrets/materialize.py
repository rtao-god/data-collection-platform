from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_SECRET_SPECS = (
    (
        "COLLECTOR_OBJECT_STORE_ACCESS_KEY_ID",
        "COLLECTOR_OBJECT_STORE_ACCESS_KEY_SECRET_FILE",
        "collector-object-store-access-key",
        4_096,
    ),
    (
        "COLLECTOR_OBJECT_STORE_SECRET_ACCESS_KEY",
        "COLLECTOR_OBJECT_STORE_SECRET_KEY_SECRET_FILE",
        "collector-object-store-secret-key",
        4_096,
    ),
    (
        "WORKER_GATEWAY_CREDENTIALS_JSON",
        "WORKER_GATEWAY_CREDENTIALS_SECRET_FILE",
        "worker-gateway-credentials.json",
        1_048_576,
    ),
)


@dataclass(frozen=True, slots=True)
class ComposeSecretMaterializationError(Exception):
    code: str
    message: str
    context: Mapping[str, object]
    required_action: str

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)


def materialize(
    output_directory: Path,
    environment: Mapping[str, str],
) -> dict[str, Path]:
    directory = _prepare_directory(output_directory)
    result: dict[str, Path] = {}
    for source_variable, path_variable, filename, maximum_bytes in _SECRET_SPECS:
        value = environment.get(source_variable)
        if value is None or value == "":
            raise ComposeSecretMaterializationError(
                code="COMPOSE_SECRET_SOURCE_MISSING",
                message="A required Compose secret source is unavailable.",
                context={"sourceVariable": source_variable},
                required_action=(
                    "Set the required source variable and materialize the Compose secrets again."
                ),
            )
        encoded = value.encode("utf-8")
        if len(encoded) > maximum_bytes:
            raise ComposeSecretMaterializationError(
                code="COMPOSE_SECRET_SOURCE_TOO_LARGE",
                message="A required Compose secret source exceeds its owner limit.",
                context={
                    "sourceVariable": source_variable,
                    "actualBytes": len(encoded),
                    "maximumBytes": maximum_bytes,
                },
                required_action=(
                    "Correct the source value instead of weakening the mounted-secret limit."
                ),
            )
        target = directory / filename
        _write_atomic_secret(target, encoded)
        result[path_variable] = target.resolve()
    return result


def _prepare_directory(output_directory: Path) -> Path:
    candidate = output_directory.expanduser()
    if candidate.is_symlink():
        raise ComposeSecretMaterializationError(
            code="COMPOSE_SECRET_DIRECTORY_UNSAFE",
            message="The Compose secret directory cannot be a symbolic link.",
            context={"outputDirectory": str(candidate)},
            required_action="Choose a project-owned, non-symlink secret directory.",
        )
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ComposeSecretMaterializationError(
            code="COMPOSE_SECRET_DIRECTORY_UNAVAILABLE",
            message="The Compose secret directory could not be created.",
            context={
                "outputDirectory": str(candidate),
                "causeType": type(exc).__name__,
            },
            required_action="Correct the directory path or filesystem permissions and retry.",
        ) from exc
    if not candidate.is_dir() or candidate.is_symlink():
        raise ComposeSecretMaterializationError(
            code="COMPOSE_SECRET_DIRECTORY_UNSAFE",
            message="The Compose secret output path is not a safe directory.",
            context={"outputDirectory": str(candidate)},
            required_action="Choose a project-owned, non-symlink secret directory.",
        )
    try:
        candidate.chmod(0o700)
    except OSError as exc:
        raise ComposeSecretMaterializationError(
            code="COMPOSE_SECRET_DIRECTORY_PERMISSIONS_FAILED",
            message="The Compose secret directory permissions could not be restricted.",
            context={
                "outputDirectory": str(candidate),
                "causeType": type(exc).__name__,
            },
            required_action="Use a filesystem that supports owner-only directory permissions.",
        ) from exc
    return candidate


def _write_atomic_secret(target: Path, content: bytes) -> None:
    if target.is_symlink():
        raise ComposeSecretMaterializationError(
            code="COMPOSE_SECRET_TARGET_UNSAFE",
            message="A Compose secret target cannot be a symbolic link.",
            context={"target": str(target)},
            required_action="Remove the symbolic link and materialize into an owned regular file.",
        )
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            dir=target.parent,
        )
        temporary_path = Path(temporary_name)
        temporary_path.chmod(0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        target.chmod(0o600)
    except OSError as exc:
        raise ComposeSecretMaterializationError(
            code="COMPOSE_SECRET_WRITE_FAILED",
            message="A Compose secret file could not be written atomically.",
            context={
                "target": str(target),
                "causeType": type(exc).__name__,
            },
            required_action="Correct the filesystem permissions or target path and retry.",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="materialize-compose-secrets")
    parser.add_argument(
        "--output-directory",
        required=True,
        type=Path,
        help="Project-owned directory for file-backed Compose secrets.",
    )
    return parser


def run(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    correlation_id = str(uuid4())
    try:
        paths = materialize(
            args.output_directory,
            os.environ if environment is None else environment,
        )
    except ComposeSecretMaterializationError as exc:
        print(
            json.dumps(
                {
                    "type": "collection/application-compose-secret-materialization-failed",
                    "owner": "ApplicationComposeSecrets",
                    "code": exc.code,
                    "message": exc.message,
                    "context": dict(exc.context),
                    "requiredAction": exc.required_action,
                    "correlationId": correlation_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            {
                "owner": "ApplicationComposeSecrets",
                "status": "materialized",
                "files": {name: str(path) for name, path in sorted(paths.items())},
                "correlationId": correlation_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
