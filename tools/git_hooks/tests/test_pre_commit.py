from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_module() -> ModuleType:
    path = Path(__file__).parents[1] / "pre_commit.py"
    spec = importlib.util.spec_from_file_location("pre_commit_policy", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cache_and_secret_paths_are_rejected() -> None:
    module = _load_module()

    assert module.violations(("src/app.py", ".tmp/probe.json", "secrets/server.key")) == (
        ".tmp/probe.json",
        "secrets/server.key",
    )


def test_normal_source_path_is_allowed() -> None:
    module = _load_module()

    assert module.violations(("packages/collection_domain/src/collection_domain/model.py",)) == ()
