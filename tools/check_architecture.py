"""Fail when source imports violate the repository-owned layer graph."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "data_collection_platform"
SOURCE_ROOT = Path("src") / PACKAGE

_ALLOWED_INTERNAL_IMPORTS: dict[str, frozenset[str]] = {
    "shared": frozenset({"shared"}),
    "domain": frozenset({"shared", "domain"}),
    "configuration": frozenset({"shared", "configuration"}),
    "application": frozenset({"shared", "domain", "configuration", "application"}),
    "infrastructure": frozenset(
        {"shared", "domain", "configuration", "application", "infrastructure"}
    ),
    "entrypoints": frozenset(
        {
            "shared",
            "domain",
            "configuration",
            "application",
            "infrastructure",
            "entrypoints",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class Violation:
    path: Path
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path.as_posix()}:{self.line}: {self.message}"


def _owner_layer(path: Path) -> str | None:
    relative = path.relative_to(SOURCE_ROOT)
    if len(relative.parts) < 2:
        return None
    return relative.parts[0]


def _imported_layer(module_name: str) -> str | None:
    parts = module_name.split(".")
    if len(parts) < 2 or parts[0] != PACKAGE:
        return None
    return parts[1]


def _check_import(
    *,
    path: Path,
    owner_layer: str,
    imported_module: str,
    line: int,
) -> Violation | None:
    imported_layer = _imported_layer(imported_module)
    if imported_layer is None:
        root_module = imported_module.split(".", maxsplit=1)[0]
        if root_module not in sys.stdlib_module_names:
            return Violation(
                path=path,
                line=line,
                message=(
                    f"production source imports undeclared third-party module {root_module!r}; "
                    "declare and review the dependency owner first"
                ),
            )
        return None

    allowed = _ALLOWED_INTERNAL_IMPORTS.get(owner_layer)
    if allowed is None:
        return Violation(
            path=path,
            line=line,
            message=f"source file belongs to unknown architectural layer {owner_layer!r}",
        )
    if imported_layer not in allowed:
        return Violation(
            path=path,
            line=line,
            message=(
                f"layer {owner_layer!r} must not import {imported_layer!r}; "
                f"allowed internal layers: {', '.join(sorted(allowed))}"
            ),
        )
    return None


def inspect_file(path: Path) -> list[Violation]:
    owner_layer = _owner_layer(path)
    if owner_layer is None:
        return []

    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        return [
            Violation(
                path=path,
                line=error.lineno or 1,
                message=f"source cannot be parsed: {error.msg}",
            )
        ]

    violations: list[Violation] = []
    for node in ast.walk(tree):
        imported_modules: list[str] = []
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                violations.append(
                    Violation(
                        path=path,
                        line=node.lineno,
                        message="relative imports are forbidden in production source",
                    )
                )
                continue
            if node.module is not None:
                imported_modules.append(node.module)
        else:
            continue

        for imported_module in imported_modules:
            violation = _check_import(
                path=path,
                owner_layer=owner_layer,
                imported_module=imported_module,
                line=node.lineno,
            )
            if violation is not None:
                violations.append(violation)
    return violations


def main() -> int:
    if not SOURCE_ROOT.is_dir():
        print(f"architecture root is missing: {SOURCE_ROOT}", file=sys.stderr)
        return 2

    violations = [
        violation
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        for violation in inspect_file(path)
    ]
    if violations:
        for violation in violations:
            print(violation.render(), file=sys.stderr)
        return 1

    print(f"architecture check passed for {SOURCE_ROOT.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
