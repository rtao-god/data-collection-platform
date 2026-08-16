from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_checker() -> ModuleType:
    path = Path(__file__).parents[1] / "check_dependencies.py"
    spec = importlib.util.spec_from_file_location("architecture_checker", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_source(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _write_workspace(
    root: Path,
    checker: ModuleType,
    members: tuple[str, ...],
    *,
    documented_policy: str | None = None,
) -> None:
    member_lines = "\n".join(f'  "{member}",' for member in members)
    _write_source(
        root,
        "pyproject.toml",
        (
            '[project]\nname = "architecture-check-test"\nversion = "0.0.0"\n'
            'requires-python = ">=3.13,<3.14"\ndependencies = []\n\n'
            "[tool.uv]\npackage = false\n\n"
            "[tool.uv.workspace]\nmembers = [\n"
            f"{member_lines}\n"
            "]\n"
        ),
    )
    policy = checker.render_dependency_policy() if documented_policy is None else documented_policy
    _write_source(
        root,
        "docs/architecture/dependency-rules.md",
        f"# Dependency rules\n\n{policy}\n",
    )


def _write_project(
    root: Path,
    project_path: str,
    *,
    name: str,
    dependencies: tuple[str, ...],
) -> None:
    dependency_lines = "\n".join(f'  "{dependency}",' for dependency in dependencies)
    _write_source(
        root,
        f"{project_path}/pyproject.toml",
        (
            "[project]\n"
            f'name = "{name}"\n'
            'version = "0.0.0"\n'
            'requires-python = ">=3.13,<3.14"\n'
            "dependencies = [\n"
            f"{dependency_lines}\n"
            "]\n"
        ),
    )


def test_domain_rejects_framework_import(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "packages/collection_domain/src/collection_domain/model.py",
        "import sqlalchemy\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "external import sqlalchemy" in violations[0].message


def test_application_rejects_infrastructure_dependency(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "packages/collection_application/src/collection_application/service.py",
        "from collection_infrastructure import Adapter\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "must not import production owner collection_infrastructure" in violations[0].message


def test_cli_rejects_direct_domain_dependency(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "apps/collector_cli/src/collector_cli/app.py",
        "from collection_domain import WorkUnit\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "must not import production owner collection_domain" in violations[0].message


def test_infrastructure_rejects_direct_domain_dependency(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "packages/collection_infrastructure/src/collection_infrastructure/adapter.py",
        "from collection_domain import WorkUnit\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "must not import production owner collection_domain" in violations[0].message


def test_collection_infrastructure_rejects_review_dependency(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "packages/collection_infrastructure/src/collection_infrastructure/adapter.py",
        "from review_application import ReviewService\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "must not import production owner review_application" in violations[0].message


def test_review_infrastructure_accepts_only_review_owner_chain(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "packages/review_infrastructure/src/review_infrastructure/postgres.py",
        (
            "import sqlalchemy\n"
            "from review_application import ReviewRepository\n"
            "from review_contracts import ReviewCase\n"
            "from review_core import decide_review_case\n"
        ),
    )

    assert checker.find_violations(tmp_path) == ()


def test_worker_gateway_rejects_review_infrastructure_dependency(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "apps/worker_gateway/src/worker_gateway/app.py",
        "from review_infrastructure import PostgresReviewRepository\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "must not import production owner review_infrastructure" in violations[0].message


def test_runtime_closure_rejects_review_owner_in_worker_gateway(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "uv.lock",
        (
            'version = 1\nrevision = 3\nrequires-python = ">=3.13,<3.14"\n\n'
            '[[package]]\nname = "worker-gateway"\n'
            'dependencies = [{ name = "collection-application" }]\n\n'
            '[[package]]\nname = "collection-application"\n'
            'dependencies = [{ name = "review-application" }]\n\n'
            '[[package]]\nname = "review-application"\n'
        ),
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "worker-gateway runtime dependency closure contains forbidden review owners" in (
        violations[0].message
    )


def test_runtime_closure_requires_complete_review_chain_in_control_api(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "uv.lock",
        (
            'version = 1\nrevision = 3\nrequires-python = ">=3.13,<3.14"\n\n'
            '[[package]]\nname = "control-api"\n'
            'dependencies = [{ name = "review-infrastructure" }]\n\n'
            '[[package]]\nname = "review-infrastructure"\n'
            "dependencies = [\n"
            '  { name = "review-application" },\n'
            '  { name = "review-contracts" },\n'
            "]\n\n"
            '[[package]]\nname = "review-application"\n'
            'dependencies = [{ name = "review-contracts" }]\n\n'
            '[[package]]\nname = "review-contracts"\n'
        ),
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].message == (
        "control-api runtime dependency closure is missing: review-core"
    )


def test_runtime_closure_accepts_isolated_review_composition(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "uv.lock",
        (
            'version = 1\nrevision = 3\nrequires-python = ">=3.13,<3.14"\n\n'
            '[[package]]\nname = "control-api"\n'
            'dependencies = [{ name = "review-infrastructure" }]\n\n'
            '[[package]]\nname = "review-infrastructure"\n'
            "dependencies = [\n"
            '  { name = "review-application" },\n'
            '  { name = "review-contracts" },\n'
            '  { name = "review-core" },\n'
            "]\n\n"
            '[[package]]\nname = "review-application"\n'
            'dependencies = [{ name = "review-contracts" }]\n\n'
            '[[package]]\nname = "review-contracts"\n\n'
            '[[package]]\nname = "review-core"\n'
            'dependencies = [{ name = "review-contracts" }]\n\n'
            '[[package]]\nname = "worker-gateway"\n'
            'dependencies = [{ name = "collection-infrastructure" }]\n\n'
            '[[package]]\nname = "collection-infrastructure"\n'
        ),
    )

    assert checker.find_violations(tmp_path) == ()


def test_migration_may_compose_infrastructure_without_owning_sql(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "apps/migration/src/collection_migration/app.py",
        "from collection_infrastructure.postgres import upgrade_database\n",
    )

    assert checker.find_violations(tmp_path) == ()


def test_migration_rejects_direct_sqlalchemy_dependency(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "apps/migration/src/collection_migration/app.py",
        "import sqlalchemy\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "external import sqlalchemy" in violations[0].message


def test_allowed_dependency_graph_passes(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "packages/collection_application/src/collection_application/service.py",
        "from collection_contracts import ErrorEnvelope\nimport json\n",
    )

    assert checker.find_violations(tmp_path) == ()


def test_generic_production_path_is_rejected(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "packages/collection_domain/src/collection_domain/utils/value.py",
        "VALUE = 1\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "forbidden generic production path segment" in violations[0].message


def test_unregistered_production_owner_is_rejected(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "packages/new_owner/src/new_owner/service.py",
        "VALUE = 1\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "unregistered production owner new_owner" in violations[0].message


def test_registered_owner_at_wrong_project_path_is_rejected(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "packages/renamed_domain/src/collection_domain/model.py",
        "VALUE = 1\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "must live at packages/collection_domain" in violations[0].message


def test_workspace_rejects_production_project_outside_members(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "packages/collection_domain/src/collection_domain/model.py",
        "VALUE = 1\n",
    )
    _write_project(
        tmp_path,
        "packages/collection_domain",
        name="collection-domain",
        dependencies=(),
    )
    _write_workspace(tmp_path, checker, ())

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "not registered in tool.uv.workspace.members" in violations[0].message


def test_project_dependencies_must_equal_owner_policy(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "apps/collector_cli/src/collector_cli/app.py",
        "import json\n",
    )
    _write_project(
        tmp_path,
        "apps/collector_cli",
        name="collector-cli",
        dependencies=("collection-domain",),
    )
    _write_workspace(tmp_path, checker, ("apps/collector_cli",))

    messages = {violation.message for violation in checker.find_violations(tmp_path)}

    assert "collector_cli must not declare internal dependency collection_domain" in messages
    assert (
        "collector_cli architecture allowance is missing declared dependency collection_application"
    ) in messages
    assert (
        "collector_cli architecture allowance is missing declared dependency collection_contracts"
    ) in messages
    assert (
        "collector_cli architecture allowance is missing declared dependency "
        "collection_infrastructure"
    ) in messages


def test_dependency_documentation_drift_is_rejected(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_workspace(
        tmp_path,
        checker,
        (),
        documented_policy=(
            "<!-- dependency-policy:start -->\noutdated\n<!-- dependency-policy:end -->"
        ),
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "documentation has drifted" in violations[0].message


def test_canonical_dependency_documentation_passes(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_workspace(tmp_path, checker, ())

    assert checker.find_violations(tmp_path) == ()


def test_worker_gateway_rejects_direct_domain_dependency(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_source(
        tmp_path,
        "apps/worker_gateway/src/worker_gateway/app.py",
        "from collection_domain import WorkLease\n",
    )

    violations = checker.find_violations(tmp_path)

    assert len(violations) == 1
    assert "must not import production owner collection_domain" in violations[0].message
