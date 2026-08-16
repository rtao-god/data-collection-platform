from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

_REGISTRY_PATH = Path("tools/architecture_checks/workflows.toml")
_WORKFLOW_ROOT = Path(".github/workflows")
_WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})
_FORBIDDEN_TRIGGERS = frozenset({"pull_request_target", "repository_dispatch", "workflow_run"})
_MUTATING_GIT_COMMAND = re.compile(
    r"(?im)(?:^|[;&|]\s*|\$\(\s*)"
    r"(?:sudo\s+|command\s+|env(?:\s+[A-Za-z_][A-Za-z0-9_]*=[^\s]+)*\s+)?"
    r"(?:[A-Za-z0-9_./-]+/)?git"
    r"(?:\s+(?:-C\s+\S+|-c\s+\S+|--git-dir(?:=\S+|\s+\S+)|"
    r"--work-tree(?:=\S+|\s+\S+)|--[A-Za-z0-9-]+|-[A-Za-z][^\s]*))*\s+"
    r"(?:add|am|apply|cherry-pick|clean|commit|merge|mv|push|rebase|reset|restore|"
    r"revert|rm|switch|tag|update-ref)\b"
)
_REMOTE_DISPATCH = re.compile(
    r"(?im)(?:^|[;&|]\s*|\$\(\s*)"
    r"(?:[A-Za-z0-9_./-]+/)?gh\s+"
    r"(?:workflow\s+run\b|api\b[^\n]*?/dispatches\b)"
)


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    message: str

    def render(self) -> str:
        return f"{self.path}: {self.message}"


def _registered_workflows(root: Path, registry_path: Path) -> frozenset[str]:
    absolute_registry = root / registry_path
    with absolute_registry.open("rb") as handle:
        payload = tomllib.load(handle)
    if payload.get("version") != 1:
        raise ValueError("workflow registry version must be 1")
    values = payload.get("permanent")
    if not isinstance(values, list) or not values:
        raise ValueError("workflow registry permanent list must be non-empty")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("workflow registry paths must be strings")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"workflow registry path escapes repository: {value}")
        if path.suffix not in _WORKFLOW_SUFFIXES:
            raise ValueError(f"workflow registry path has unsupported suffix: {value}")
        normalized.append(path.as_posix())
    if len(normalized) != len(set(normalized)):
        raise ValueError("workflow registry contains duplicate paths")
    return frozenset(normalized)


def _load_workflow(path: Path) -> Mapping[str, object]:
    payload: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"workflow root must be a mapping: {path.as_posix()}")
    normalized: dict[str, object] = {}
    for key, value in payload.items():
        normalized_key = "on" if key is True else str(key)
        normalized[normalized_key] = value
    return normalized


def _permission_violations(value: object, *, scope: str) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return (f"{scope} permissions must declare contents: read",)
    normalized = {str(key): str(permission).casefold() for key, permission in value.items()}
    violations: list[str] = []
    if normalized.get("contents") != "read":
        violations.append(f"{scope} permissions must declare contents: read")
    write_permissions = sorted(
        key for key, permission in normalized.items() if permission == "write"
    )
    if write_permissions:
        violations.append(
            f"{scope} permissions grant write access: {', '.join(write_permissions)}"
        )
    return tuple(violations)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _run_scripts(payload: Mapping[str, object]) -> Iterator[str]:
    jobs = _mapping(payload.get("jobs"))
    for job in jobs.values():
        job_mapping = _mapping(job)
        steps = job_mapping.get("steps")
        if not isinstance(steps, Sequence) or isinstance(steps, str):
            continue
        for step in steps:
            step_mapping = _mapping(step)
            script = step_mapping.get("run")
            if isinstance(script, str):
                yield script


def _workflow_violations(payload: Mapping[str, object]) -> tuple[str, ...]:
    violations = list(_permission_violations(payload.get("permissions"), scope="top-level"))
    jobs = _mapping(payload.get("jobs"))
    for job_name, job in jobs.items():
        job_mapping = _mapping(job)
        if "permissions" in job_mapping:
            violations.extend(
                _permission_violations(
                    job_mapping.get("permissions"),
                    scope=f"job {job_name}",
                )
            )

    triggers = payload.get("on")
    trigger_names: set[str] = set()
    if isinstance(triggers, str):
        trigger_names.add(triggers)
    elif isinstance(triggers, Sequence):
        trigger_names.update(str(value) for value in triggers)
    elif isinstance(triggers, Mapping):
        trigger_names.update(str(value) for value in triggers)
    forbidden = sorted(trigger_names & _FORBIDDEN_TRIGGERS)
    if forbidden:
        violations.append(f"workflow uses forbidden controller trigger: {', '.join(forbidden)}")

    for script in _run_scripts(payload):
        if _MUTATING_GIT_COMMAND.search(script):
            violations.append("workflow invokes a mutating Git command")
        if _REMOTE_DISPATCH.search(script):
            violations.append("workflow dispatches another workflow remotely")
    return tuple(dict.fromkeys(violations))


def find_violations(
    repository_root: Path,
    *,
    registry_path: Path = _REGISTRY_PATH,
) -> tuple[Violation, ...]:
    root = repository_root.resolve(strict=True)
    registered = _registered_workflows(root, registry_path)
    workflow_root = root / _WORKFLOW_ROOT
    discovered = frozenset(
        path.relative_to(root).as_posix()
        for path in workflow_root.iterdir()
        if path.is_file() and path.suffix in _WORKFLOW_SUFFIXES
    )

    violations: list[Violation] = []
    for path in sorted(registered - discovered):
        violations.append(Violation(path, "registered permanent workflow is missing"))
    for path in sorted(discovered - registered):
        violations.append(Violation(path, "workflow is not registered as a permanent proof owner"))

    for relative_path in sorted(discovered):
        payload = _load_workflow(root / relative_path)
        violations.extend(
            Violation(relative_path, message) for message in _workflow_violations(payload)
        )
    return tuple(violations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify permanent read-only GitHub Actions owners")
    parser.add_argument("repository_root", nargs="?", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    try:
        violations = find_violations(arguments.repository_root)
    except (OSError, ValueError, tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
        print(f"workflow architecture check failed: {exc}", file=sys.stderr)
        return 2
    if violations:
        for violation in violations:
            print(violation.render(), file=sys.stderr)
        return 1
    print("Workflow architecture check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
