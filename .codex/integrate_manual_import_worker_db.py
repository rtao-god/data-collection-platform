from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "packages/collection_infrastructure/src/collection_infrastructure/postgres/migrations/versions"


def _revision_graph() -> tuple[set[str], dict[str, str | None]]:
    revisions: set[str] = set()
    parents: dict[str, str | None] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        revision: str | None = None
        down_revision: str | None = None
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for target in targets:
                if not isinstance(target, ast.Name) or not isinstance(value, ast.Constant):
                    continue
                if target.id == "revision" and isinstance(value.value, str):
                    revision = value.value
                if target.id == "down_revision" and (
                    isinstance(value.value, str) or value.value is None
                ):
                    down_revision = value.value
        if revision is not None:
            revisions.add(revision)
            parents[revision] = down_revision
    return revisions, parents


def _migration_head(revisions: set[str], parents: dict[str, str | None]) -> str:
    referenced = {value for value in parents.values() if value is not None}
    heads = sorted(revisions - referenced)
    if len(heads) != 1:
        raise RuntimeError(f"expected one Alembic head, found {heads}")
    return heads[0]


def _artifact_schema() -> str:
    path = ROOT / "packages/collection_infrastructure/src/collection_infrastructure/postgres/artifact_metadata.py"
    content = path.read_text(encoding="utf-8")
    tree = ast.parse(content)
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        if not isinstance(node.args[0], ast.Constant) or node.args[0].value != "artifact_uploads":
            continue
        for keyword in node.keywords:
            if keyword.arg != "schema":
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value
            if isinstance(keyword.value, ast.Name) and keyword.value.id in constants:
                return constants[keyword.value.id]
    raise RuntimeError("artifact_uploads schema could not be resolved")


def _next_revision(revisions: set[str]) -> str:
    for suffix in range(8, 100):
        candidate = f"20260812_{suffix:04d}"
        if candidate not in revisions:
            return candidate
    raise RuntimeError("no free migration revision ID")


def add_migration() -> None:
    for path in VERSIONS.glob("*.py"):
        if "derived_artifact" in path.read_text(encoding="utf-8"):
            return
    revisions, parents = _revision_graph()
    head = _migration_head(revisions, parents)
    revision = _next_revision(revisions)
    schema = _artifact_schema()
    constraint = "ck_artifact_uploads_artifact_kind"
    content = f'''"""Allow verified manual import plans as derived artifacts."""

from __future__ import annotations

from alembic import op

revision = "{revision}"
down_revision = "{head}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _replace_kind_constraint(
        "artifact_kind IN ('raw_artifact', 'diagnostic_artifact', 'derived_artifact')"
    )


def downgrade() -> None:
    _replace_kind_constraint("artifact_kind IN ('raw_artifact', 'diagnostic_artifact')")


def _replace_kind_constraint(expression: str) -> None:
    op.execute(
        f"""
        DO $migration$
        DECLARE current_constraint text;
        BEGIN
            SELECT constraint_name
              INTO current_constraint
              FROM information_schema.check_constraints
             WHERE constraint_schema = '{schema}'
               AND check_clause LIKE '%artifact_kind%'
             ORDER BY constraint_name
             LIMIT 1;
            IF current_constraint IS NOT NULL THEN
                EXECUTE format(
                    'ALTER TABLE {schema}.artifact_uploads DROP CONSTRAINT %I',
                    current_constraint
                );
            END IF;
            EXECUTE 'ALTER TABLE {schema}.artifact_uploads '
                'ADD CONSTRAINT {constraint} CHECK (' || expression || ')';
        END
        $migration$;
        """
    )
'''
    path = VERSIONS / f"{revision}_derived_artifacts.py"
    path.write_text(content, encoding="utf-8")


def patch_object_store() -> None:
    candidates = sorted(
        (ROOT / "packages/collection_infrastructure/src/collection_infrastructure").rglob(
            "*object_store*.py"
        )
    )
    for path in candidates:
        content = path.read_text(encoding="utf-8")
        if "diagnostic-artifacts" not in content or "raw-artifacts" not in content:
            continue
        if "derived-artifacts" in content:
            return
        updated = re.sub(
            r'(ArtifactKind\.DIAGNOSTIC_ARTIFACT\s*:\s*"diagnostic-artifacts"\s*,?)',
            r'\1\n        ArtifactKind.DERIVED_ARTIFACT: "derived-artifacts",',
            content,
            count=1,
        )
        if updated == content:
            updated = content.replace(
                '"diagnostic_artifact": "diagnostic-artifacts",',
                '"diagnostic_artifact": "diagnostic-artifacts",\n'
                '        "derived_artifact": "derived-artifacts",',
                1,
            )
        if updated == content:
            raise RuntimeError(f"unsupported object-store kind mapping in {path}")
        path.write_text(updated, encoding="utf-8")
        return
    raise RuntimeError("artifact object-store adapter was not found")


def main() -> None:
    add_migration()
    patch_object_store()


if __name__ == "__main__":
    main()
