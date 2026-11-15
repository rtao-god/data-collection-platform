from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ALLOWED_COMMANDS = frozenset({"dagster-webserver", "dagster-daemon"})


def main() -> int:
    command = sys.argv[1:]
    if not command or command[0] not in _ALLOWED_COMMANDS:
        return _fail(
            "DAGSTER_COMMAND_INVALID",
            "The orchestrator entrypoint only permits Dagster webserver or daemon commands.",
        )
    secret_path = os.environ.get("DAGSTER_POSTGRES_PASSWORD_FILE", "")
    if not secret_path:
        return _fail(
            "DAGSTER_METADATA_CREDENTIALS_MISSING",
            "The Dagster metadata database password file is not configured.",
        )
    try:
        password = Path(secret_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        return _fail(
            "DAGSTER_METADATA_CREDENTIALS_UNAVAILABLE",
            "The Dagster metadata database password file could not be read.",
            cause_type=type(exc).__name__,
        )
    if not password:
        return _fail(
            "DAGSTER_METADATA_CREDENTIALS_INVALID",
            "The Dagster metadata database password is empty.",
        )
    os.environ["DAGSTER_POSTGRES_PASSWORD"] = password
    try:
        # The executable is allowlisted above and argv is passed without a shell.
        os.execvp(command[0], command)  # noqa: S606
    except OSError as exc:
        return _fail(
            "DAGSTER_COMMAND_UNAVAILABLE",
            "The configured Dagster command is not installed in the runtime image.",
            cause_type=type(exc).__name__,
        )
    return 2


def _fail(code: str, message: str, *, cause_type: str | None = None) -> int:
    context = {"causeType": cause_type} if cause_type is not None else {}
    print(
        json.dumps(
            {
                "owner": "CollectorDagsterOrchestrator",
                "code": code,
                "message": message,
                "context": context,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
