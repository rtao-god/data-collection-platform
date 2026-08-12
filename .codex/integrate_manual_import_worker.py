from __future__ import annotations

import ast
import copy
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    content = read(path)
    if new in content:
        return
    if old not in content:
        raise RuntimeError(f"missing integration anchor in {path}: {old!r}")
    write(path, content.replace(old, new, 1))


def register_workspace() -> None:
    path = "pyproject.toml"
    content = read(path)
    member = '  "apps/manual_import_worker",\n'
    if member not in content:
        anchors = (
            '  "apps/worker_gateway",\n',
            '  "apps/migration",\n',
        )
        for anchor in anchors:
            if anchor in content:
                content = content.replace(anchor, anchor + member, 1)
                break
        else:
            raise RuntimeError("workspace member list anchor was not found")
    write(path, content)


def extend_artifact_kind() -> None:
    replacements = {
        'Literal["raw_artifact", "diagnostic_artifact"]': (
            'Literal["raw_artifact", "diagnostic_artifact", "derived_artifact"]'
        ),
        '{"raw_artifact", "diagnostic_artifact"}': (
            '{"raw_artifact", "diagnostic_artifact", "derived_artifact"}'
        ),
        "{'raw_artifact', 'diagnostic_artifact'}": (
            "{'raw_artifact', 'diagnostic_artifact', 'derived_artifact'}"
        ),
        "IN ('raw_artifact', 'diagnostic_artifact')": (
            "IN ('raw_artifact', 'diagnostic_artifact', 'derived_artifact')"
        ),
    }
    for path in sorted(ROOT.rglob("*.py")) + sorted(ROOT.rglob("*.sql")):
        if ".venv" in path.parts or path.name == Path(__file__).name:
            continue
        content = path.read_text(encoding="utf-8")
        updated = content
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        if (
            'DIAGNOSTIC_ARTIFACT = "diagnostic_artifact"' in updated
            and 'DERIVED_ARTIFACT = "derived_artifact"' not in updated
        ):
            updated = updated.replace(
                'DIAGNOSTIC_ARTIFACT = "diagnostic_artifact"',
                'DIAGNOSTIC_ARTIFACT = "diagnostic_artifact"\n'
                '    DERIVED_ARTIFACT = "derived_artifact"',
            )
        if updated != content:
            path.write_text(updated, encoding="utf-8")


def add_worker_owner() -> None:
    candidates = sorted((ROOT / "tools/architecture_checks").rglob("*.py"))
    for path in candidates:
        content = path.read_text(encoding="utf-8")
        if "manual_import_worker" in content:
            return
        if "source_connector_sdk" not in content or "OwnerRule" not in content:
            continue
        tree = ast.parse(content)
        parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        source_call: ast.Call | None = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            segment = ast.get_source_segment(content, node) or ""
            if "source_connector_sdk" in segment and "OwnerRule" in segment:
                source_call = node
                break
        if source_call is None:
            continue
        clone = copy.deepcopy(source_call)
        for node in ast.walk(clone):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                value = value.replace("source-connector-sdk", "manual-import-worker")
                value = value.replace("source_connector_sdk", "manual_import_worker")
                value = value.replace(
                    "packages/manual_import_worker", "apps/manual_import_worker"
                )
                node.value = value
        for keyword in clone.keywords:
            name = keyword.arg or ""
            if "internal" in name:
                keyword.value = ast.Call(
                    func=ast.Name(id="frozenset", ctx=ast.Load()),
                    args=[
                        ast.Set(
                            elts=[
                                ast.Constant("collection_contracts"),
                                ast.Constant("manual_import_core"),
                                ast.Constant("source_connector_sdk"),
                            ]
                        )
                    ],
                    keywords=[],
                )
            elif "external" in name:
                keyword.value = ast.Call(
                    func=ast.Name(id="frozenset", ctx=ast.Load()),
                    args=[ast.Set(elts=[ast.Constant("httpx")])],
                    keywords=[],
                )
        ast.fix_missing_locations(clone)
        new_call = ast.unparse(clone)
        original = ast.get_source_segment(content, source_call)
        if original is None:
            raise RuntimeError("could not recover source connector owner rule")
        insertion = original + ",\n    " + new_call
        updated = content.replace(original, insertion, 1)
        path.write_text(updated, encoding="utf-8")
        return
    raise RuntimeError("architecture owner registry was not found")


def add_ci_image() -> None:
    path = ".github/workflows/ci.yml"
    content = read(path)
    if "manual-import-worker.Dockerfile" in content:
        return
    match = re.search(
        r"(?ms)^(\s*)- name: Build Worker Gateway image\n"
        r"\1  run: docker build -f deploy/docker/worker-gateway\.Dockerfile \.\n",
        content,
    )
    if match is None:
        raise RuntimeError("Worker Gateway image step was not found")
    indent = match.group(1)
    addition = (
        match.group(0)
        + f"\n{indent}- name: Build manual import worker image\n"
        + f"{indent}  run: docker build -f deploy/docker/manual-import-worker.Dockerfile .\n"
    )
    write(path, content[: match.start()] + addition + content[match.end() :])


def fix_test_types() -> None:
    path = "apps/manual_import_worker/tests/test_worker.py"
    content = read(path)
    content = content.replace(
        "from source_connector_sdk import LeaseArtifact, WorkerLease",
        "from source_connector_sdk import (\n"
        "    LeaseArtifact,\n"
        "    WorkCapability,\n"
        "    WorkStage,\n"
        "    WorkerLease,\n"
        ")",
    )
    content = content.replace('stage=cast("object", "acquisition")', 'stage=cast(WorkStage, "acquisition")')
    content = content.replace(
        'capability=cast("object", "manual_import")',
        'capability=cast(WorkCapability, "manual_import")',
    )
    write(path, content)


def update_status() -> None:
    path = "docs/implementation-status.md"
    content = read(path)
    line = (
        "| Manual import worker | Isolated lease-scoped worker reads one exact source artifact, "
        "publishes a verified deterministic plan, heartbeats, and never receives SQL credentials |\n"
    )
    if line not in content:
        marker = "| Manual import planning |"
        index = content.find(marker)
        if index < 0:
            raise RuntimeError("implementation status table anchor was not found")
        end = content.find("\n", index) + 1
        content = content[:end] + line + content[end:]
    content = content.replace(
        "runtime intake still does not preserve the exact source as a raw artifact or schedule one work unit per accepted row",
        "runtime worker intake preserves the exact source binding and publishes a verified plan, but does not yet schedule one work unit per accepted row",
    )
    write(path, content)


def main() -> None:
    register_workspace()
    extend_artifact_kind()
    add_worker_owner()
    add_ci_image()
    fix_test_types()
    update_status()


if __name__ == "__main__":
    main()
