from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected source fragment is missing")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> int:
    _replace_once(
        Path("apps/collector_cli/pyproject.toml"),
        '  "collection-infrastructure",\n]\n',
        '  "collection-infrastructure",\n'
        '  "boto3==1.43.54",\n'
        '  "sqlalchemy==2.0.51",\n'
        ']\n',
    )
    checker = Path("tools/architecture_checks/check_dependencies.py")
    _replace_once(
        checker,
        '        allowed_external_imports=frozenset(),\n'
        '    ),\n'
        '    "worker_gateway": OwnerPolicy(\n',
        '        allowed_external_imports=frozenset({"boto3", "sqlalchemy"}),\n'
        '    ),\n'
        '    "worker_gateway": OwnerPolicy(\n',
    )

    policy = subprocess.check_output(
        [sys.executable, str(checker), "--print-policy"],
        text=True,
    ).strip()
    policy_path = Path("docs/architecture/dependency-rules.md")
    text = policy_path.read_text(encoding="utf-8")
    start_marker = "<!-- dependency-policy:start -->"
    end_marker = "<!-- dependency-policy:end -->"
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    policy_path.write_text(text[:start] + policy + text[end:], encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
