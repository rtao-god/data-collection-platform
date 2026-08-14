from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_control_api_contract_artifacts_are_current() -> None:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [sys.executable, "tools/control_api_contract_generation/generate.py", "--check"],
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
