from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

_PRODUCTION_GROUPS = ("apps", "packages", "connectors")
_FORBIDDEN_PRODUCTION_SEGMENTS = frozenset({"common", "helpers", "shared_domain", "utils"})
_DEPENDENCY_POLICY_PATH = Path("docs/architecture/dependency-rules.md")
_DEPENDENCY_POLICY_START = "<!-- dependency-policy:start -->"
_DEPENDENCY_POLICY_END = "<!-- dependency-policy:end -->"
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True, slots=True)
class OwnerPolicy:
    project_path: str
    distribution_name: str
    allowed_internal_imports: tuple[str, ...]
    allowed_external_imports: frozenset[str]


_OWNER_POLICIES: dict[str, OwnerPolicy] = {
    "control_api": OwnerPolicy(
        project_path="apps/control_api",
        distribution_name="control-api",
        allowed_internal_imports=(
            "collection_infrastructure",
            "review_application",
            "review_contracts",
        ),
        allowed_external_imports=frozenset({"fastapi", "pydantic", "sqlalchemy", "uvicorn"}),
    ),
    "collector_cli": OwnerPolicy(
        project_path="apps/collector_cli",
        distribution_name="collector-cli",
        allowed_internal_imports=(
            "collection_application",
            "collection_contracts",
            "collection_infrastructure",
        ),
        allowed_external_imports=frozenset({"boto3", "sqlalchemy"}),
    ),
    "http_worker": OwnerPolicy(
        project_path="apps/http_worker",
        distribution_name="http-worker",
        allowed_internal_imports=(
            "official_http",
            "source_connector_sdk",
        ),
        allowed_external_imports=frozenset(),
    ),
    "processing_worker": OwnerPolicy(
        project_path="apps/processing_worker",
        distribution_name="processing-worker",
        allowed_internal_imports=(
            "collection_contracts",
            "extraction_core",
            "normalization_core",
            "source_connector_sdk",
        ),
        allowed_external_imports=frozenset({"pydantic"}),
    ),
    "resolution_worker": OwnerPolicy(
        project_path="apps/resolution_worker",
        distribution_name="resolution-worker",
        allowed_internal_imports=(
            "entity_resolution_core",
            "quality_core",
            "resolution_contracts",
            "source_connector_sdk",
        ),
        allowed_external_imports=frozenset(),
    ),
    "worker_gateway": OwnerPolicy(
        project_path="apps/worker_gateway",
        distribution_name="worker-gateway",
        allowed_internal_imports=(
            "collection_application",
            "collection_contracts",
            "collection_infrastructure",
        ),
        allowed_external_imports=frozenset({"fastapi", "pydantic", "sqlalchemy", "uvicorn"}),
    ),
    "collection_migration": OwnerPolicy(
        project_path="apps/migration",
        distribution_name="collection-migration",
        allowed_internal_imports=(
            "collection_contracts",
            "collection_infrastructure",
        ),
        allowed_external_imports=frozenset(),
    ),
    "manual_import_worker": OwnerPolicy(
        project_path="apps/manual_import_worker",
        distribution_name="manual-import-worker",
        allowed_internal_imports=(
            "collection_contracts",
            "manual_import_core",
            "source_connector_sdk",
        ),
        allowed_external_imports=frozenset({"httpx"}),
    ),
    "osm_worker": OwnerPolicy(
        project_path="apps/osm_worker",
        distribution_name="osm-worker",
        allowed_internal_imports=(
            "osm_overpass",
            "source_connector_sdk",
        ),
        allowed_external_imports=frozenset(),
    ),
    "official_http": OwnerPolicy(
        project_path="connectors/official_http",
        distribution_name="official-http-connector",
        allowed_internal_imports=("source_connector_sdk",),
        allowed_external_imports=frozenset({"defusedxml", "pydantic", "scrapy"}),
    ),
    "osm_overpass": OwnerPolicy(
        project_path="connectors/osm_overpass",
        distribution_name="osm-overpass-connector",
        allowed_internal_imports=(),
        allowed_external_imports=frozenset({"httpx"}),
    ),
    "review_application": OwnerPolicy(
        project_path="packages/review_application",
        distribution_name="review-application",
        allowed_internal_imports=("review_contracts",),
        allowed_external_imports=frozenset(),
    ),
    "review_contracts": OwnerPolicy(
        project_path="packages/review_contracts",
        distribution_name="review-contracts",
        allowed_internal_imports=(),
        allowed_external_imports=frozenset({"pydantic"}),
    ),
    "review_core": OwnerPolicy(
        project_path="packages/review_core",
        distribution_name="review-core",
        allowed_internal_imports=("review_contracts",),
        allowed_external_imports=frozenset(),
    ),
    "collection_infrastructure": OwnerPolicy(
        project_path="packages/collection_infrastructure",
        distribution_name="collection-infrastructure",
        allowed_internal_imports=(
            "collection_application",
            "collection_contracts",
            "review_application",
            "review_contracts",
            "review_core",
        ),
        allowed_external_imports=frozenset(
            {"alembic", "boto3", "botocore", "psycopg", "sqlalchemy"}
        ),
    ),
    "collection_application": OwnerPolicy(
        project_path="packages/collection_application",
        distribution_name="collection-application",
        allowed_internal_imports=(
            "collection_contracts",
            "collection_domain",
            "manual_import_core",
        ),
        allowed_external_imports=frozenset({"pydantic", "yaml"}),
    ),
    "extraction_core": OwnerPolicy(
        project_path="packages/extraction_core",
        distribution_name="extraction-core",
        allowed_internal_imports=("collection_contracts",),
        allowed_external_imports=frozenset({"extruct", "lxml"}),
    ),
    "normalization_core": OwnerPolicy(
        project_path="packages/normalization_core",
        distribution_name="normalization-core",
        allowed_internal_imports=("collection_contracts",),
        allowed_external_imports=frozenset({"phonenumbers", "tldextract"}),
    ),
    "entity_resolution_core": OwnerPolicy(
        project_path="packages/entity_resolution_core",
        distribution_name="entity-resolution-core",
        allowed_internal_imports=("resolution_contracts",),
        allowed_external_imports=frozenset(),
    ),
    "quality_core": OwnerPolicy(
        project_path="packages/quality_core",
        distribution_name="quality-core",
        allowed_internal_imports=("resolution_contracts",),
        allowed_external_imports=frozenset(),
    ),
    "resolution_contracts": OwnerPolicy(
        project_path="packages/resolution_contracts",
        distribution_name="resolution-contracts",
        allowed_internal_imports=(),
        allowed_external_imports=frozenset({"pydantic"}),
    ),
    "manual_import_core": OwnerPolicy(
        project_path="packages/manual_import_core",
        distribution_name="manual-import-core",
        allowed_internal_imports=("collection_contracts",),
        allowed_external_imports=frozenset({"pydantic"}),
    ),
    "source_connector_sdk": OwnerPolicy(
        project_path="packages/source_connector_sdk",
        distribution_name="source-connector-sdk",
        allowed_internal_imports=("collection_contracts",),
        allowed_external_imports=frozenset({"httpx"}),
    ),
    "collection_domain": OwnerPolicy(
        project_path="packages/collection_domain",
        distribution_name="collection-domain",
        allowed_internal_imports=(),
        allowed_external_imports=frozenset(),
    ),
    "collection_contracts": OwnerPolicy(
        project_path="packages/collection_contracts",
        distribution_name="collection-contracts",
        allowed_internal_imports=(),
        allowed_external_imports=frozenset({"pydantic"}),
    ),
}


def _normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


_INTERNAL_DISTRIBUTIONS = {
    _normalize_distribution_name(policy.distribution_name): owner
    for owner, policy in _OWNER_POLICIES.items()
}


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


@dataclass(frozen=True, slots=True)
class DiscoveredOwner:
    import_root: str
    project_path: str
    source_root: Path
    files: tuple[Path, ...]


def find_violations(repository_root: Path) -> tuple[Violation, ...]:
    root = repository_root.resolve(strict=True)
    discovered, discovery_violations = _discover_owners(root)
    violations = list(discovery_violations)
    violations.extend(_owner_registration_violations(root, discovered))

    production_owners = frozenset(_OWNER_POLICIES).union(discovered)
    for owner, discovered_owner in sorted(discovered.items()):
        policy = _OWNER_POLICIES.get(owner)
        if policy is None or policy.project_path != discovered_owner.project_path:
            continue
        for file_path in discovered_owner.files:
            relative = file_path.relative_to(root)
            source_relative = file_path.relative_to(discovered_owner.source_root)
            if len(source_relative.parts) < 2 or source_relative.parts[0] != owner:
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
            violations.extend(
                _file_import_violations(
                    root,
                    file_path,
                    owner,
                    policy,
                    production_owners,
                )
            )

    violations.extend(_workspace_violations(root, discovered))
    violations.extend(_dependency_documentation_violations(root))
    return tuple(sorted(violations, key=lambda item: (item.path, item.line, item.message)))


def render_dependency_policy() -> str:
    lines = [
        _DEPENDENCY_POLICY_START,
        "| Production owner | Project | Allowed internal owners | Allowed external imports |",
        "|---|---|---|---|",
    ]
    for owner, policy in _OWNER_POLICIES.items():
        internal = ", ".join(f"`{value}`" for value in policy.allowed_internal_imports) or "none"
        external = (
            ", ".join(f"`{value}`" for value in sorted(policy.allowed_external_imports)) or "none"
        )
        lines.append(f"| `{owner}` | `{policy.project_path}` | {internal} | {external} |")
    lines.append(_DEPENDENCY_POLICY_END)
    return "\n".join(lines)


def _discover_owners(root: Path) -> tuple[dict[str, DiscoveredOwner], list[Violation]]:
    discovered: dict[str, DiscoveredOwner] = {}
    violations: list[Violation] = []
    for group in _PRODUCTION_GROUPS:
        group_root = root / group
        if not group_root.is_dir():
            continue
        for project_root in sorted(path for path in group_root.iterdir() if path.is_dir()):
            source_root = project_root / "src"
            if not source_root.is_dir():
                continue
            files = tuple(sorted(source_root.rglob("*.py")))
            if not files:
                continue

            project_path = project_root.relative_to(root).as_posix()
            import_roots: set[str] = set()
            for file_path in files:
                relative = file_path.relative_to(source_root)
                if len(relative.parts) < 2:
                    violations.append(
                        Violation(
                            file_path.relative_to(root).as_posix(),
                            1,
                            (
                                "production Python source must live inside one import-root "
                                "package under src/"
                            ),
                        )
                    )
                    continue
                import_roots.add(relative.parts[0])

            if len(import_roots) != 1:
                violations.append(
                    Violation(
                        f"{project_path}/src",
                        1,
                        (
                            "production project must expose exactly one import-root package; "
                            f"found {sorted(import_roots)}"
                        ),
                    )
                )
                continue

            owner = next(iter(import_roots))
            previous = discovered.get(owner)
            if previous is not None:
                violations.append(
                    Violation(
                        f"{project_path}/src/{owner}",
                        1,
                        (
                            f"production owner {owner} is duplicated by "
                            f"{previous.project_path} and {project_path}"
                        ),
                    )
                )
                continue

            discovered[owner] = DiscoveredOwner(
                import_root=owner,
                project_path=project_path,
                source_root=source_root,
                files=files,
            )
    return discovered, violations


def _owner_registration_violations(
    root: Path,
    discovered: dict[str, DiscoveredOwner],
) -> list[Violation]:
    violations: list[Violation] = []
    for owner, discovered_owner in sorted(discovered.items()):
        policy = _OWNER_POLICIES.get(owner)
        first_path = discovered_owner.files[0].relative_to(root).as_posix()
        if policy is None:
            violations.append(
                Violation(
                    first_path,
                    1,
                    (
                        f"unregistered production owner {owner}; add an explicit OwnerPolicy "
                        "before adding production source"
                    ),
                )
            )
            continue
        if policy.project_path != discovered_owner.project_path:
            violations.append(
                Violation(
                    first_path,
                    1,
                    (
                        f"registered production owner {owner} must live at "
                        f"{policy.project_path}, found {discovered_owner.project_path}"
                    ),
                )
            )
    return violations


def _workspace_violations(
    root: Path,
    discovered: dict[str, DiscoveredOwner],
) -> list[Violation]:
    workspace_path = root / "pyproject.toml"
    if not workspace_path.is_file():
        return []

    document, parse_violation = _read_toml(root, workspace_path)
    if parse_violation is not None:
        return [parse_violation]
    if document is None:
        return [Violation("pyproject.toml", 1, "TOML parser returned no document")]

    tool = document.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    workspace = uv.get("workspace") if isinstance(uv, dict) else None
    members = workspace.get("members") if isinstance(workspace, dict) else None
    if not isinstance(members, list) or not all(isinstance(value, str) for value in members):
        return [
            Violation(
                "pyproject.toml",
                1,
                "uv workspace members must be an explicit list of project paths",
            )
        ]

    production_members = {
        Path(member).as_posix()
        for member in members
        if Path(member).parts and Path(member).parts[0] in _PRODUCTION_GROUPS
    }
    discovered_projects = {owner.project_path for owner in discovered.values()}
    violations: list[Violation] = []

    for project_path in sorted(discovered_projects.difference(production_members)):
        violations.append(
            Violation(
                f"{project_path}/pyproject.toml",
                1,
                "production project is not registered in tool.uv.workspace.members",
            )
        )
    for project_path in sorted(production_members.difference(discovered_projects)):
        violations.append(
            Violation(
                "pyproject.toml",
                1,
                f"workspace production member has no Python owner source: {project_path}",
            )
        )

    for owner, discovered_owner in sorted(discovered.items()):
        policy = _OWNER_POLICIES.get(owner)
        if policy is None or policy.project_path != discovered_owner.project_path:
            continue
        violations.extend(_project_dependency_violations(root, owner, policy))
    return violations


def _project_dependency_violations(
    root: Path,
    owner: str,
    policy: OwnerPolicy,
) -> list[Violation]:
    project_file = root / policy.project_path / "pyproject.toml"
    relative_path = project_file.relative_to(root).as_posix()
    if not project_file.is_file():
        return [
            Violation(
                relative_path,
                1,
                f"registered production owner {owner} is missing pyproject.toml",
            )
        ]

    document, parse_violation = _read_toml(root, project_file)
    if parse_violation is not None:
        return [parse_violation]
    if document is None:
        return [Violation(relative_path, 1, "TOML parser returned no document")]

    project = document.get("project")
    if not isinstance(project, dict):
        return [Violation(relative_path, 1, "project table is required")]

    actual_name = project.get("name")
    if not isinstance(actual_name, str):
        return [Violation(relative_path, 1, "project.name must be an explicit string")]
    if _normalize_distribution_name(actual_name) != _normalize_distribution_name(
        policy.distribution_name
    ):
        return [
            Violation(
                relative_path,
                1,
                (
                    f"{owner} must use distribution name {policy.distribution_name}, "
                    f"found {actual_name}"
                ),
            )
        ]

    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(value, str) for value in dependencies
    ):
        return [
            Violation(
                relative_path,
                1,
                "project.dependencies must be an explicit list of requirement strings",
            )
        ]

    declared_internal: set[str] = set()
    for requirement in dependencies:
        match = _REQUIREMENT_NAME.match(requirement.strip())
        if match is None:
            return [
                Violation(
                    relative_path,
                    1,
                    f"dependency requirement has no parseable distribution name: {requirement}",
                )
            ]
        dependency_owner = _INTERNAL_DISTRIBUTIONS.get(_normalize_distribution_name(match.group(0)))
        if dependency_owner is not None:
            declared_internal.add(dependency_owner)

    expected_internal = set(policy.allowed_internal_imports)
    violations: list[Violation] = []
    for imported in sorted(declared_internal.difference(expected_internal)):
        violations.append(
            Violation(
                relative_path,
                1,
                f"{owner} must not declare internal dependency {imported}",
            )
        )
    for imported in sorted(expected_internal.difference(declared_internal)):
        violations.append(
            Violation(
                relative_path,
                1,
                f"{owner} architecture allowance is missing declared dependency {imported}",
            )
        )
    return violations


def _dependency_documentation_violations(root: Path) -> list[Violation]:
    workspace_path = root / "pyproject.toml"
    if not workspace_path.is_file():
        return []

    document_path = root / _DEPENDENCY_POLICY_PATH
    relative_path = _DEPENDENCY_POLICY_PATH.as_posix()
    if not document_path.is_file():
        return [
            Violation(
                relative_path,
                1,
                "dependency policy documentation is required for the repository workspace",
            )
        ]

    try:
        content = document_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [Violation(relative_path, 1, f"dependency policy is not UTF-8: {exc}")]

    expected = render_dependency_policy()
    start = content.find(_DEPENDENCY_POLICY_START)
    end = content.find(_DEPENDENCY_POLICY_END)
    if start < 0 or end < start:
        return [
            Violation(
                relative_path,
                1,
                "dependency policy documentation is missing generated policy markers",
            )
        ]
    actual = content[start : end + len(_DEPENDENCY_POLICY_END)]
    if actual != expected:
        return [
            Violation(
                relative_path,
                1,
                (
                    "dependency policy documentation has drifted from the canonical "
                    "OwnerPolicy registry"
                ),
            )
        ]
    return []


def _file_import_violations(
    root: Path,
    file_path: Path,
    owner: str,
    policy: OwnerPolicy,
    production_owners: frozenset[str],
) -> list[Violation]:
    relative = file_path.relative_to(root).as_posix()
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=relative)
    except (SyntaxError, UnicodeDecodeError) as exc:
        line = exc.lineno if isinstance(exc, SyntaxError) and exc.lineno is not None else 1
        return [Violation(relative, line, f"source cannot be parsed: {exc}")]

    violations: list[Violation] = []
    allowed_internal = frozenset(policy.allowed_internal_imports)
    for node in ast.walk(tree):
        for imported in _import_roots(node):
            if imported == owner:
                continue
            if imported in production_owners:
                if imported not in allowed_internal:
                    violations.append(
                        Violation(
                            relative,
                            node.lineno,
                            f"{owner} must not import production owner {imported}",
                        )
                    )
                continue
            if imported in sys.stdlib_module_names or imported == "__future__":
                continue
            if imported not in policy.allowed_external_imports:
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


def _read_toml(
    root: Path,
    path: Path,
) -> tuple[dict[str, object] | None, Violation | None]:
    relative = path.relative_to(root).as_posix()
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return None, Violation(relative, 1, f"TOML cannot be parsed: {exc}")
    return document, None


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
    parser.add_argument(
        "--print-policy",
        action="store_true",
        help="Print the canonical dependency-policy documentation block.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.print_policy:
        print(render_dependency_policy())
        return 0

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
