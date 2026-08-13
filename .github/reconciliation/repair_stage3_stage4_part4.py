from __future__ import annotations

from pathlib import Path


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected source fragment is missing")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def apply(root: Path) -> None:
    _write(
        root / "deploy/docker/osm-worker.Dockerfile",
        '''FROM python:3.13.14-slim AS build

ENV UV_COMPILE_BYTECODE=1 \\
    UV_LINK_MODE=copy

WORKDIR /workspace

RUN pip install --no-cache-dir uv==0.10.0

COPY .python-version pyproject.toml uv.lock ./
COPY connectors/osm_overpass ./connectors/osm_overpass
COPY packages/collection_contracts ./packages/collection_contracts
COPY packages/source_connector_sdk ./packages/source_connector_sdk
COPY apps/osm_worker ./apps/osm_worker

RUN uv sync \\
    --frozen \\
    --no-dev \\
    --no-editable \\
    --package osm-worker

FROM python:3.13.14-slim AS runtime

ENV PATH="/workspace/.venv/bin:${PATH}" \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 worker \\
    && useradd --uid 10001 --gid worker --create-home worker

WORKDIR /workspace
COPY --from=build --chown=worker:worker /workspace/.venv /workspace/.venv

USER 10001:10001

ENTRYPOINT ["osm-worker"]
''',
    )

    ci = root / ".github/workflows/ci.yml"
    text = ci.read_text(encoding="utf-8")
    if "Build OSM worker image" not in text:
        anchor = (
            "      - name: Build manual import worker image\n"
            "        run: docker build --file deploy/docker/manual-import-worker.Dockerfile ."
        )
        addition = (
            anchor
            + "\n\n"
            + "      - name: Build OSM worker image\n"
            + "        run: docker build --file deploy/docker/osm-worker.Dockerfile ."
        )
        if anchor not in text:
            raise RuntimeError("CI manual worker image anchor is missing")
        text = text.replace(anchor, addition, 1)
    ci.write_text(text, encoding="utf-8")

    manual_worker = root / "apps/manual_import_worker/src/manual_import_worker/worker.py"
    text = manual_worker.read_text(encoding="utf-8")
    old_import = "from manual_import_core import build_manual_import_plan\n"
    new_import = (
        "from collection_contracts import ManualImportPlan\n"
        "from manual_import_core import (\n"
        "    build_manual_import_plan,\n"
        "    canonical_manual_import_plan_json,\n"
        ")\n"
    )
    if old_import not in text:
        raise RuntimeError("ManualImportWorker: planner import anchor is missing")
    text = text.replace(old_import, new_import, 1)
    plan_view = (
        "class _PlanView(Protocol):\n"
        "    digest: str\n\n"
        "    def to_bytes(self) -> bytes: ...\n\n\n"
    )
    if plan_view not in text:
        raise RuntimeError("ManualImportWorker: obsolete plan view is missing")
    text = text.replace(plan_view, "", 1)
    old_serialization = (
        "            payload = plan.to_bytes()\n"
        "            plan_digest = _sha256_identity(payload)\n"
        "            if plan.digest != plan_digest:\n"
        "                raise ValueError(\"manual import plan digest does not match canonical bytes\")\n"
    )
    new_serialization = (
        "            payload = canonical_manual_import_plan_json(plan).encode(\"utf-8\")\n"
        "            artifact_digest = _sha256_identity(payload)\n"
        "            plan_digest = plan.plan_digest\n"
    )
    if old_serialization not in text:
        raise RuntimeError("ManualImportWorker: obsolete plan serialization is missing")
    text = text.replace(old_serialization, new_serialization, 1)
    old_builder_start = text.index(
        "def _build_plan(body: bytes, source: ManualImportSource) -> _PlanView:\n"
    )
    old_builder_end = text.index("\n\ndef _sha256_identity", old_builder_start)
    typed_builder = (
        "def _build_plan(body: bytes, source: ManualImportSource) -> ManualImportPlan:\n"
        "    return build_manual_import_plan(\n"
        "        body,\n"
        "        format=source.format,\n"
        "        mode=source.mode,\n"
        "    )"
    )
    text = text[:old_builder_start] + typed_builder + text[old_builder_end:]
    if "                content_digest=plan_digest,\n" not in text:
        raise RuntimeError("ManualImportWorker: upload digest anchor is missing")
    text = text.replace(
        "                content_digest=plan_digest,\n",
        "                content_digest=artifact_digest,\n",
        1,
    )
    manual_worker.write_text(text, encoding="utf-8")

    manual_test = root / "apps/manual_import_worker/tests/test_worker.py"
    _replace_once(
        manual_test,
        '        return b"name,active,count\\nStudio,true,2\\n"\n',
        (
            '        return (\n'
            '            b"expected_entity_kind,display_name,website,osm_id,"\n'
            '            b"reference_urls,note,provenance\\n"\n'
            '            b"place,Studio,,,,,manual-test\\n"\n'
            '        )\n'
        ),
    )

    osm_gateway = root / "apps/osm_worker/src/osm_worker/gateway.py"
    _replace_once(
        osm_gateway,
        '_OUTPUT_CONTRACTS = frozenset({"osm-overpass-result/1"})\n',
        '_OUTPUT_CONTRACTS = frozenset({"osm-overpass-result@1"})\n',
    )
    osm_test = root / "apps/osm_worker/tests/test_worker.py"
    _replace_once(
        osm_test,
        '        expected_output_contract="osm-overpass-result/1",\n',
        '        expected_output_contract="osm-overpass-result@1",\n',
    )

    artifact_test = root / (
        "packages/collection_infrastructure/tests/test_postgres_artifact_metadata.py"
    )
    _replace_once(
        artifact_test,
        '        "sources.artifact_uploads",\n'
        '        "sources.artifact_objects",\n',
        '        "sources.artifact_uploads",\n'
        '        "sources.artifact_cleanup_tombstones",\n'
        '        "sources.artifact_objects",\n',
    )

    architecture = root / "tools/architecture_checks/check_dependencies.py"

    import subprocess
    import sys

    policy = subprocess.check_output(
        [sys.executable, str(architecture), "--print-policy"],
        text=True,
    ).strip()
    policy_doc = root / "docs/architecture/dependency-rules.md"
    text = policy_doc.read_text(encoding="utf-8")
    start_marker = "<!-- dependency-policy:start -->"
    end_marker = "<!-- dependency-policy:end -->"
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    policy_doc.write_text(text[:start] + policy + text[end:], encoding="utf-8")
