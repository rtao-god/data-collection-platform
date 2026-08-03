from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_module() -> ModuleType:
    path = Path(__file__).parents[1] / "commit_message.py"
    spec = importlib.util.spec_from_file_location("commit_message_policy", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_valid_message_is_accepted() -> None:
    module = _load_module()

    assert module.validate("Feature (Campaign Config): add deterministic snapshot service") is None


def test_nonconforming_message_is_rejected() -> None:
    module = _load_module()

    assert module.validate("quick fix") is not None
