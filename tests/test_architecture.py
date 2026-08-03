from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = PROJECT_ROOT / "tools" / "check_architecture.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("architecture_checker", CHECKER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("architecture checker could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_current_source_graph_passes_executable_checker(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")

        completed = subprocess.run(
            [sys.executable, str(CHECKER_PATH)],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("architecture check passed", completed.stdout)

    def test_domain_to_infrastructure_import_is_rejected(self) -> None:
        checker = _load_checker()

        violation = checker._check_import(
            path=Path("src/data_collection_platform/domain/example.py"),
            owner_layer="domain",
            imported_module="data_collection_platform.infrastructure.database",
            line=7,
        )

        self.assertIsNotNone(violation)
        self.assertIn("must not import 'infrastructure'", violation.message)

    def test_undeclared_third_party_import_is_rejected(self) -> None:
        checker = _load_checker()

        violation = checker._check_import(
            path=Path("src/data_collection_platform/domain/example.py"),
            owner_layer="domain",
            imported_module="unowned_dependency.client",
            line=3,
        )

        self.assertIsNotNone(violation)
        self.assertIn("undeclared third-party module", violation.message)


if __name__ == "__main__":
    unittest.main()
