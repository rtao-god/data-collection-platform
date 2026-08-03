from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_INTERNAL_OWNERS = frozenset(
    {
        "collector_cli",
        "collection_application",
        "collection_contracts",
        "collection_domain",
        "collection_infrastructure",
    }
)
_ALLOWED_INTERNAL_IMPORTS: dict[str, frozenset[str]] = {
    "collector_cli": frozenset(
        {
            "collection_application",
            "collection_contracts",
            "collection_domain",
            "collection_infrastructure",
        }
    ),
    "collection_infrastructure": frozenset(
        {"collection_application", "collection_contracts", "collection_domain"}
    ),
    "collection_application": frozenset({"collection_contracts", "collection_domain"}),
    "collection_domain": frozenset(),
    "collection_contracts": frozenset(),
}
_ALLOWED_EXTERNAL_IMPORTS: dict[str, frozenset[str]] = {
    "collector_cli": frozenset(),
    "collection_infrastructure": frozenset(),
    "collection_application": frozenset({"pydantic", "yaml"}),
    "collection_domain": frozenset(),
    "collection_contracts": frozenset({"pydantic"}),
}
_FORBIDDEN_PRODUCTION_SEGMENTS = frozenset({"common", "helpers", "shared_domain", "utils"})


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def find_violations(repository_root: Path) -> tuple[Violation, ...]:
    root = repository_root.resolve(strict=True)
    violations: list[Violation] = []
    for source_root in _source_roots(root):
        for file_path in sorted(source_root.rglob("*.py")):
            relative = file_path.relative_to(root)
            owner = _owner_for(relative)
            if owner is None:
                continue
            forbidden_parts = _FORBIDDEN_PRODUCTION_SEGMENTS.intersection(relative.parts)
            if forbidden_parts:
                violations.append(
                    Violation(
                        relative.as_posix(),
                        1,
                        f"forbidden generic production path segment: {sorted(forbidden_parts)}",
                    )
                )
            violations.extend(_file_import_violations(root, file_path, owner))
    return tuple(violations)


def _source_roots(root: Path) -> tuple[Path, ...]:
    candidates = tuple((root / group) for group in ("apps", "packages"))
    return tuple(path for path in candidates if path.is_dir())


def _owner_for(relative: Path) -> str | None:
    parts = relative.parts
    try:
        src_index = parts.index("src")
    except ValueError:
        return None
    if src_index + 1 >= len(parts):
        return None
    owner = parts[src_index + 1]
    return owner if owner in _INTERNAL_OWNERS else None


def _file_import_violations(root: Path, file_path: Path, owner: str) -> list[Violation]:
    relative = file_path.relative_to(root).as_posix()
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=relative)
    except (SyntaxError, UnicodeDecodeError) as exc:
        line = exc.lineno if isinstance(exc, SyntaxError) and exc.lineno is not None else 1
        return [Violation(relative, line, f"source cannot be parsed: {exc}")]

    violations: list[Violation] = []
    for node in ast.walk(tree):
        imports = _import_roots(node)
        for imported in imports:
            if imported == owner:
                continue
            if imported in _INTERNAL_OWNERS:
                if imported not in _ALLOWED_INTERNAL_IMPORTS[owner]:
                    violations.append(
                        Violation(
                            relative,
                            node.lineno,
                            f"{owner} must not import internal owner {imported}",
                        )
                    )
                continue
            if imported in sys.stdlib_module_names or imported == "__future__":
                continue
            if imported not in _ALLOWED_EXTERNAL_IMPORTS[owner]:
                violations.append(
                    Violation(
                        relative,
                        node.lineno,
                        (
                            f"{owner} has no declared architecture allowance for "
                            f"external import {imported}"
                        ),
                    )
                )
    return violations


def _import_roots(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.level > 0 or node.module is None:
            return ()
        return (node.module.split(".", maxsplit=1)[0],)
    return ()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository_root", nargs="?", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    violations = find_violations(args.repository_root)
    if not violations:
        print("Architecture dependency check passed.")
        return 0
    print("Architecture dependency check failed:")
    for violation in violations:
        print(f"- {violation.render()}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
