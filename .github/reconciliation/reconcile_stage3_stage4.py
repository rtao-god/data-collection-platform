from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from repair_stage3_stage4_part1 import apply as repair_part1
from repair_stage3_stage4_part2a import apply as repair_part2a
from repair_stage3_stage4_part2b1 import apply as repair_part2b1
from repair_stage3_stage4_part2b2 import apply as repair_part2b2
from repair_stage3_stage4_part3 import apply as repair_part3
from repair_stage3_stage4_part4 import apply as repair_part4


def _replace_once(text: str, old: str, new: str, owner: str) -> str:
    if old not in text:
        raise RuntimeError(f"{owner}: expected source fragment is missing")
    return text.replace(old, new, 1)


def main() -> int:
    root = Path.cwd()

    workspace = root / "pyproject.toml"
    text = workspace.read_text(encoding="utf-8")
    members = '''members = [
  "apps/collector_cli",
  "apps/manual_import_worker",
  "apps/migration",
  "apps/osm_worker",
  "apps/worker_gateway",
  "connectors/osm_overpass",
  "packages/collection_application",
  "packages/collection_contracts",
  "packages/collection_domain",
  "packages/collection_infrastructure",
  "packages/manual_import_core",
  "packages/source_connector_sdk",
]'''
    text, count = re.subn(r"(?ms)^members = \[\n.*?^\]", members, text, count=1)
    if count != 1:
        raise RuntimeError("Workspace: members block was not found exactly once")
    text = _replace_once(
        text,
        'testpaths = ["apps", "database", "packages", "tools"]',
        'testpaths = ["apps", "connectors", "database", "packages", "tools"]',
        "Pytest",
    )
    integration_marker = '  "integration: requires explicit PostgreSQL/PostGIS infrastructure",\n'
    object_store_marker = (
        '  "object_store_integration: requires explicit SeaweedFS S3 infrastructure",\n'
    )
    if object_store_marker not in text:
        text = _replace_once(
            text,
            integration_marker,
            integration_marker + object_store_marker,
            "Pytest markers",
        )
    text = _replace_once(
        text,
        'src = ["apps", "database", "packages", "tools"]',
        'src = ["apps", "connectors", "database", "packages", "tools"]',
        "Ruff",
    )
    mypy_start = text.index("[tool.mypy]")
    files_start = text.index("files = [", mypy_start)
    files_end = text.index("\n]", files_start) + 2
    mypy_files = '''files = [
  "apps/collector_cli/src/collector_cli",
  "apps/manual_import_worker/src/manual_import_worker",
  "apps/migration/src/collection_migration",
  "apps/osm_worker/src/osm_worker",
  "apps/worker_gateway/src/worker_gateway",
  "connectors/osm_overpass/src/osm_overpass",
  "packages/collection_application/src/collection_application",
  "packages/collection_contracts/src/collection_contracts",
  "packages/collection_domain/src/collection_domain",
  "packages/collection_infrastructure/src/collection_infrastructure",
  "packages/manual_import_core/src/manual_import_core",
  "packages/source_connector_sdk/src/source_connector_sdk",
  "database/migrations/env.py",
  "database/migrations/versions",
  "tools/contract_generation/generate.py",
]'''
    text = text[:files_start] + mypy_files + text[files_end:]
    workspace.write_text(text, encoding="utf-8")

    osm_project = root / "connectors/osm_overpass/pyproject.toml"
    text = osm_project.read_text(encoding="utf-8")
    if 'dependencies = ["httpx==0.28.1"]' not in text:
        text = _replace_once(
            text,
            "dependencies = []",
            'dependencies = ["httpx==0.28.1"]',
            "OSM connector dependencies",
        )
    osm_project.write_text(text, encoding="utf-8")

    checker = root / "tools/architecture_checks/check_dependencies.py"
    text = checker.read_text(encoding="utf-8")
    policies = '''    "manual_import_worker": OwnerPolicy(
        project_path="apps/manual_import_worker",
        distribution_name="manual-import-worker",
        allowed_internal_imports=(
            "collection_contracts",
            "manual_import_core",
            "source_connector_sdk",
        ),
        allowed_external_imports=frozenset({"httpx"}),
    ),
    "osm_worker": OwnerPolicy(
        project_path="apps/osm_worker",
        distribution_name="osm-worker",
        allowed_internal_imports=(
            "osm_overpass",
            "source_connector_sdk",
        ),
        allowed_external_imports=frozenset(),
    ),
    "osm_overpass": OwnerPolicy(
        project_path="connectors/osm_overpass",
        distribution_name="osm-overpass-connector",
        allowed_internal_imports=(),
        allowed_external_imports=frozenset({"httpx"}),
    ),
'''
    if '"manual_import_worker": OwnerPolicy(' not in text:
        text = _replace_once(
            text,
            '    "collection_infrastructure": OwnerPolicy(\n',
            policies + '    "collection_infrastructure": OwnerPolicy(\n',
            "Architecture registry",
        )
    checker.write_text(text, encoding="utf-8")

    policy = subprocess.check_output(
        [sys.executable, str(checker), "--print-policy"],
        text=True,
    ).strip()
    policy_doc = root / "docs/architecture/dependency-rules.md"
    text = policy_doc.read_text(encoding="utf-8")
    start_marker = "<!-- dependency-policy:start -->"
    end_marker = "<!-- dependency-policy:end -->"
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    policy_doc.write_text(text[:start] + policy + text[end:], encoding="utf-8")

    ci = root / ".github/workflows/ci.yml"
    text = ci.read_text(encoding="utf-8")
    text = _replace_once(
        text,
        'uv run pytest -m "not integration"',
        'uv run pytest -m "not integration and not object_store_integration"',
        "CI unit test selection",
    )
    text = _replace_once(
        text,
        "uv run python -m compileall -q apps database packages tools",
        "uv run python -m compileall -q apps connectors database packages tools",
        "CI compilation scope",
    )
    if "Build manual import worker image" not in text:
        collector_step = (
            "      - name: Build collector CLI image\n"
            "        run: docker build --file deploy/docker/collector-cli.Dockerfile ."
        )
        manual_step = (
            collector_step
            + "\n\n"
            + "      - name: Build manual import worker image\n"
            + "        run: docker build --file deploy/docker/manual-import-worker.Dockerfile ."
        )
        text = _replace_once(text, collector_step, manual_step, "CI image inventory")
    ci.write_text(text, encoding="utf-8")

    admission = root / (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/manual_import_admission.py"
    )
    text = admission.read_text(encoding="utf-8")
    text = text.replace(
        '"Inspect the Work Engine semantic identity and retry the exact admission."',
        '"Inspect the Work Engine semantic identity and "\n'
        '                            "retry the exact admission."',
    )
    text = text.replace(
        '"Inspect admission, artifact, and Work Engine rows before retrying the exact plan."',
        '"Inspect admission, artifact, and Work Engine rows before "\n'
        '                    "retrying the exact plan."',
    )
    admission.write_text(text, encoding="utf-8")

    child_writer = root / (
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/manual_import_child_writer.py"
    )
    text = child_writer.read_text(encoding="utf-8")
    text = text.replace(
        '    """Uses the Work Engine\'s transaction-local enqueue owner without opening a second transaction."""',
        '    """Use the Work Engine transaction-local enqueue owner.\n\n'
        '    The admission transaction must not open a second database transaction.\n'
        '    """',
    )
    child_writer.write_text(text, encoding="utf-8")

    repair_part1(root)
    repair_part2a(root)
    repair_part2b1(root)
    repair_part2b2(root)
    repair_part3(root)
    repair_part4(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
