from __future__ import annotations

import shutil
import subprocess
from pathlib import PurePosixPath

FORBIDDEN_PATH_PARTS = {
    ".codegraph",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".venv",
    "__pycache__",
    "tmp",
}
FORBIDDEN_NAMES = {".env", ".env.local"}
FORBIDDEN_SUFFIXES = {".key", ".log", ".pem", ".pyc"}


def staged_paths() -> tuple[str, ...]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is unavailable")
    result = subprocess.run(
        [git, "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def violations(paths: tuple[str, ...]) -> tuple[str, ...]:
    invalid: list[str] = []
    for path_text in paths:
        path = PurePosixPath(path_text)
        if (
            FORBIDDEN_PATH_PARTS.intersection(path.parts)
            or path.name in FORBIDDEN_NAMES
            or path.suffix.lower() in FORBIDDEN_SUFFIXES
        ):
            invalid.append(path_text)
    return tuple(invalid)


def main() -> int:
    invalid = violations(staged_paths())
    if not invalid:
        return 0

    print("Git policy rejected forbidden temporary, cache, log, or secret paths:")
    for path in invalid:
        print(f"- {path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
