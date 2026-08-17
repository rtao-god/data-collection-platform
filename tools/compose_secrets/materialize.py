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

_ENVIRONMENT_FILE_MAXIMUM_BYTES = 1_048_576
_SECRET_DIRECTORY_MODE = 0o700
_SECRET_SPECS = (
    (
        "COLLECTOR_OBJECT_STORE_ACCESS_KEY_ID",
        "COLLECTOR_OBJECT_STORE_ACCESS_KEY_SOURCE_FILE",
        "collector-object-store-access-key",
        4_096,
    ),
    (
        "COLLECTOR_OBJECT_STORE_SECRET_ACCESS_KEY",
        "COLLECTOR_OBJECT_STORE_SECRET_KEY_SOURCE_FILE",
        "collector-object-store-secret-key",
        4_096,
    ),
    (
        "WORKER_GATEWAY_CREDENTIALS_JSON",
        "WORKER_GATEWAY_CREDENTIALS_SOURCE_FILE",
        "worker-gateway-credentials",
        1_048_576,
    ),
)
_SECRET_SOURCE_VARIABLES = frozenset(spec[0] for spec in _SECRET_SPECS)


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


def _load_secret_sources(environment_file: Path) -> dict[str, str]:
    candidate = environment_file.expanduser()
    if candidate.is_symlink():
        raise ComposeSecretMaterializationError(
            code="COMPOSE_SECRET_ENVIRONMENT_FILE_UNSAFE",
            message="The Compose environment file cannot be a symbolic link.",
            context={"environmentFile": str(candidate)},
            required_action="Use an explicit project-owned environment file.",
        )
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        raise ComposeSecretMaterializationError(
            code="COMPOSE_SECRET_ENVIRONMENT_FILE_UNAVAILABLE",
            message="The Compose environment file could not be read.",
            context={
                "environmentFile": str(candidate),
                "causeType": type(exc).__name__,
            },
            required_action="Correct the environment file path or permissions and retry.",
        ) from exc
    if len(content) > _ENVIRONMENT_FILE_MAXIMUM_BYTES:
        raise ComposeSecretMaterializationError(
            code="COMPOSE_SECRET_ENVIRONMENT_FILE_TOO_LARGE",
            message="The Compose environment file exceeds its owner limit.",
            context={
                "environmentFile": str(candidate),
                "actualBytes": len(content),
                "maximumBytes": _ENVIRONMENT_FILE_MAXIMUM_BYTES,
            },
            required_action="Remove unrelated or malformed content from the environment file.",
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ComposeSecretMaterializationError(
            code="COMPOSE_SECRET_ENVIRONMENT_FILE_INVALID_ENCODING",
            message="The Compose environment file is not valid UTF-8.",
            context={
                "environmentFile": str(candidate),
                "startByte": exc.start,
            },
            required_action="Save the environment file as UTF-8 and retry.",
        ) from exc

    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in raw_line:
            continue
        raw_key, raw_value = raw_line.split("=", maxsplit=1)
        key = raw_key.strip()
        if key not in _SECRET_SOURCE_VARIABLES:
            continue
        if key in result:
            raise ComposeSecretMaterializationError(
                code="COMPOSE_SECRET_ENVIRONMENT_SOURCE_DUPLICATED",
                message="A Compose secret source is declared more than once.",
                context={
                    "environmentFile": str(candidate),
                    "sourceVariable": key,
                    "lineNumber": line_number,
                },
                required_action="Keep one exact declaration for each secret source.",
            )
        result[key] = _decode_environment_value(
            raw_value.strip(),
            environment_file=candidate,
            source_variable=key,
            line_number=line_number,
        )
    return result


def _decode_environment_value(
    raw_value: str,
    *,
    environment_file: Path,
    source_variable: str,
    line_number: int,
) -> str:
    if raw_value == "":
        return ""
    if raw_value.startswith("'"):
        if len(raw_value) < 2 or not raw_value.endswith("'"):
            raise _environment_value_error(
                environment_file,
                source_variable,
                line_number,
            )
        return raw_value[1:-1]
    if raw_value.startswith('"'):
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise _environment_value_error(
                environment_file,
                source_variable,
                line_number,
            ) from exc
        if not isinstance(decoded, str):
            raise _environment_value_error(
                environment_file,
                source_variable,
                line_number,
            )
        return decoded
    if raw_value.endswith(("'", '"')):
        raise _environment_value_error(
            environment_file,
            source_variable,
            line_number,
        )
    return raw_value


def _environment_value_error(
    environment_file: Path,
    source_variable: str,
    line_number: int,
) -> ComposeSecretMaterializationError:
    return ComposeSecretMaterializationError(
        code="COMPOSE_SECRET_ENVIRONMENT_VALUE_INVALID",
        message="A Compose secret source has invalid quoting.",
        context={
            "environmentFile": str(environment_file),
            "sourceVariable": source_variable,
            "lineNumber": line_number,
        },
        required_action="Use one unquoted, single-quoted, or JSON double-quoted value.",
    )


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
        candidate.chmod(_SECRET_DIRECTORY_MODE)
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


def _mounted_secret_mode() -> int:
    # Local Compose bind-mounts file secrets without remapping host ownership. The 0700
    # parent directory protects host access; read-only file bits let fixed non-root UIDs
    # read only the secret files explicitly mounted into their containers.
    return 0o600 if os.name == "nt" else 0o444


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
        temporary_path.chmod(_mounted_secret_mode())
        temporary_path.replace(target)
        temporary_path = None
        target.chmod(_mounted_secret_mode())
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
        "--environment-file",
        type=Path,
        help="Optional project-owned Compose environment file containing secret sources.",
    )
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
        process_environment = dict(os.environ if environment is None else environment)
        if args.environment_file is None:
            secret_environment = process_environment
        else:
            secret_environment = _load_secret_sources(args.environment_file)
            secret_environment.update(
                {
                    key: value
                    for key, value in process_environment.items()
                    if key in _SECRET_SOURCE_VARIABLES
                }
            )
        paths = materialize(args.output_directory, secret_environment)
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
