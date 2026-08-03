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
    assert "must not import internal owner collection_infrastructure" in violations[0].message


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
