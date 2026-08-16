from __future__ import annotations

from pathlib import Path

from tools.architecture_checks.check_workflows import find_violations

_READ_ONLY_WORKFLOW = """\
name: Verify

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  verify:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v6
      - run: git diff --check
"""


def _repository(tmp_path: Path, workflow: str = _READ_ONLY_WORKFLOW) -> Path:
    root = tmp_path / "repository"
    workflow_root = root / ".github/workflows"
    workflow_root.mkdir(parents=True)
    (workflow_root / "ci.yml").write_text(workflow, encoding="utf-8")
    registry_root = root / "tools/architecture_checks"
    registry_root.mkdir(parents=True)
    (registry_root / "workflows.toml").write_text(
        'version = 1\npermanent = [".github/workflows/ci.yml"]\n',
        encoding="utf-8",
    )
    return root


def _messages(root: Path) -> tuple[str, ...]:
    return tuple(violation.message for violation in find_violations(root))


def test_current_repository_workflows_are_registered_and_read_only() -> None:
    assert not find_violations(Path.cwd())


def test_unregistered_yaml_workflow_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / ".github/workflows/repair.yaml").write_text(_READ_ONLY_WORKFLOW, encoding="utf-8")

    assert "workflow is not registered as a permanent proof owner" in _messages(root)


def test_write_permission_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path, _READ_ONLY_WORKFLOW.replace("contents: read", "contents: write"))

    messages = _messages(root)
    assert "top-level permissions must declare contents: read" in messages
    assert "top-level permissions grant write access: contents" in messages


def test_job_level_write_permission_is_rejected(tmp_path: Path) -> None:
    workflow = _READ_ONLY_WORKFLOW.replace(
        "    runs-on: ubuntu-24.04\n",
        "    permissions:\n      contents: write\n    runs-on: ubuntu-24.04\n",
    )
    root = _repository(tmp_path, workflow)

    assert "job verify permissions grant write access: contents" in _messages(root)


def test_mutating_git_command_with_global_options_is_rejected(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        _READ_ONLY_WORKFLOW.replace("git diff --check", "git -C . push origin HEAD:main"),
    )

    assert "workflow invokes a mutating Git command" in _messages(root)


def test_controller_trigger_is_rejected(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        _READ_ONLY_WORKFLOW.replace(
            "  push:\n    branches: [main]\n",
            "  workflow_run:\n    workflows: [Verify]\n    types: [completed]\n",
        ),
    )

    assert "workflow uses forbidden controller trigger: workflow_run" in _messages(root)


def test_remote_workflow_dispatch_is_rejected(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        _READ_ONLY_WORKFLOW.replace(
            "git diff --check",
            "gh workflow run repair.yml --ref main",
        ),
    )

    assert "workflow dispatches another workflow remotely" in _messages(root)


def test_registered_missing_workflow_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / ".github/workflows/ci.yml").unlink()

    assert "registered permanent workflow is missing" in _messages(root)
