from __future__ import annotations

import re
import sys
from pathlib import Path

HEADER_PATTERN = re.compile(
    r"^(Build|Docs|Feature|Fix|Migration|Refactor|Test|Tooling) "
    r"\([A-Z][A-Za-z0-9 -]{1,39}\): [A-Za-z0-9][A-Za-z0-9 ,.'/:+_()-]{7,71}$"
)


def validate(message: str) -> str | None:
    header = next((line.strip() for line in message.splitlines() if line.strip()), "")
    if not header:
        return "commit message is empty"
    if not HEADER_PATTERN.fullmatch(header):
        return (
            "header must use '<Tag> (Scope): <English technical summary>' and be 80 characters "
            "or fewer"
        )
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: commit_message.py <commit-message-file>")
        return 2
    message = Path(argv[1]).read_text(encoding="utf-8")
    error = validate(message)
    if error is None:
        return 0
    print(f"Git policy rejected commit message: {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
