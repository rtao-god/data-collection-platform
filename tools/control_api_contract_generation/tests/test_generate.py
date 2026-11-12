from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_generator() -> ModuleType:
    path = Path(__file__).parents[1] / "generate.py"
    spec = importlib.util.spec_from_file_location("control_api_contract_generator", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_check_detects_stale_control_api_contract(tmp_path: Path) -> None:
    generator = _load_generator()
    expected = generator.render()
    output = tmp_path / "contracts"
    output.mkdir()
    for name, content in expected.items():
        (output / name).write_text(content, encoding="utf-8")
    first_name = sorted(expected)[0]
    (output / first_name).write_text("{}\n", encoding="utf-8")

    with pytest.raises(SystemExit) as failure:
        generator.check_outputs(output)

    assert first_name in str(failure.value)


def test_check_accepts_current_control_api_contract(tmp_path: Path) -> None:
    generator = _load_generator()
    expected = generator.render()
    output = tmp_path / "contracts"
    output.mkdir()
    for name, content in expected.items():
        (output / name).write_text(content, encoding="utf-8")

    assert generator.check_outputs(output) is None
