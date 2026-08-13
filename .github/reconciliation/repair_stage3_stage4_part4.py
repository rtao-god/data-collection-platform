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
