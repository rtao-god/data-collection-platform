from __future__ import annotations

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
    _replace_once(
        Path("tools/architecture_checks/check_dependencies.py"),
        '        allowed_external_imports=frozenset(),\n'
        '    ),\n'
        '    "worker_gateway": OwnerPolicy(\n',
        '        allowed_external_imports=frozenset({"boto3", "sqlalchemy"}),\n'
        '    ),\n'
        '    "worker_gateway": OwnerPolicy(\n',
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
