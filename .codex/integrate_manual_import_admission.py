from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "packages/collection_infrastructure/src/collection_infrastructure/postgres/migrations/versions"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _export_application() -> None:
    path = "packages/collection_application/src/collection_application/__init__.py"
    content = _read(path)
    if "ManualImportAdmissionService" in content:
        return
    import_block = '''from collection_application.manual_import_admission import (
    AdmitManualImportPlan,
    ManualImportAdmissionResult,
    ManualImportAdmissionService,
    ManualImportAdmissionStore,
    ManualImportChildWork,
    ManualImportPlanBlocked,
    ManualImportPlanForAdmission,
    ManualImportRecord,
    admission_result_digest,
)
'''
    content = import_block + content
    marker = "__all__ = ["
    if marker in content:
        additions = (
            '    "AdmitManualImportPlan",\n'
            '    "ManualImportAdmissionResult",\n'
            '    "ManualImportAdmissionService",\n'
            '    "ManualImportAdmissionStore",\n'
            '    "ManualImportChildWork",\n'
            '    "ManualImportPlanBlocked",\n'
            '    "ManualImportPlanForAdmission",\n'
            '    "ManualImportRecord",\n'
            '    "admission_result_digest",\n'
        )
        content = content.replace(marker, marker + "\n" + additions, 1)
    _write(path, content)


def _revision_graph() -> tuple[set[str], dict[str, str | None]]:
    revisions: set[str] = set()
    parents: dict[str, str | None] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        revision: str | None = None
        parent: str | None = None
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
                elif target.id == "down_revision" and (
                    isinstance(value.value, str) or value.value is None
                ):
                    parent = value.value
        if revision is not None:
            revisions.add(revision)
            parents[revision] = parent
    return revisions, parents


def _head(revisions: set[str], parents: dict[str, str | None]) -> str:
    referenced = {value for value in parents.values() if value is not None}
    heads = sorted(revisions - referenced)
    if len(heads) != 1:
        raise RuntimeError(f"expected one Alembic head, found {heads}")
    return heads[0]


def _table_identity(path: str, table_name: str) -> tuple[str, str]:
    content = _read(path)
    tree = ast.parse(content)
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or first.value != table_name:
            continue
        schema = None
        for keyword in node.keywords:
            if keyword.arg != "schema":
                continue
            if isinstance(keyword.value, ast.Constant):
                schema = keyword.value.value
            elif isinstance(keyword.value, ast.Name):
                schema = constants.get(keyword.value.id)
        if isinstance(schema, str):
            return schema, table_name
    raise RuntimeError(f"could not resolve {table_name} from {path}")


def _next_revision(revisions: set[str]) -> str:
    for suffix in range(9, 100):
        candidate = f"20260812_{suffix:04d}"
        if candidate not in revisions:
            return candidate
    raise RuntimeError("no free manual import admission revision ID")


def _add_migration() -> None:
    for path in VERSIONS.glob("*.py"):
        if "plan_admissions" in path.read_text(encoding="utf-8"):
            return
    revisions, parents = _revision_graph()
    revision = _next_revision(revisions)
    parent = _head(revisions, parents)
    work_schema, work_table = _table_identity(
        "packages/collection_infrastructure/src/collection_infrastructure/postgres/work_metadata.py",
        "work_units",
    )
    artifact_schema, artifact_table = _table_identity(
        "packages/collection_infrastructure/src/collection_infrastructure/postgres/artifact_metadata.py",
        "raw_artifacts",
    )
    content = f'''"""Persist atomic manual import plan admissions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "{revision}"
down_revision = "{parent}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS manual_import")
    op.create_table(
        "plan_admissions",
        sa.Column("admission_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("parent_work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_digest", sa.Text(), nullable=False),
        sa.Column("source_digest", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("plan_status", sa.Text(), nullable=False),
        sa.Column("target_stage", sa.Text(), nullable=False),
        sa.Column("target_capability", sa.Text(), nullable=False),
        sa.Column("target_output_contract", sa.Text(), nullable=False),
        sa.Column("total_record_count", sa.Integer(), nullable=False),
        sa.Column("accepted_record_count", sa.Integer(), nullable=False),
        sa.Column("rejected_record_count", sa.Integer(), nullable=False),
        sa.Column("child_work_count", sa.Integer(), nullable=False),
        sa.Column("result_digest", sa.Text(), nullable=False),
        sa.Column("admitted_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("plan_status = 'ready'", name="ck_plan_admissions_ready"),
        sa.CheckConstraint(
            "accepted_record_count + rejected_record_count = total_record_count",
            name="ck_plan_admissions_counts",
        ),
        sa.CheckConstraint(
            "accepted_record_count = child_work_count",
            name="ck_plan_admissions_child_count",
        ),
        sa.ForeignKeyConstraint(
            ["parent_work_id"], ["{work_schema}.{work_table}.work_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["plan_artifact_id"],
            ["{artifact_schema}.{artifact_table}.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"],
            ["{artifact_schema}.{artifact_table}.artifact_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "parent_work_id", "plan_artifact_id", name="uq_plan_admissions_parent_plan"
        ),
        schema="manual_import",
    )
    op.create_table(
        "plan_admission_items",
        sa.Column("admission_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("child_work_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("locator_kind", sa.Text(), nullable=False),
        sa.Column("locator_value", sa.Text(), nullable=False),
        sa.Column("record_digest", sa.Text(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_plan_admission_items_position"),
        sa.ForeignKeyConstraint(
            ["admission_id"],
            ["manual_import.plan_admissions.admission_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["child_work_id"], ["{work_schema}.{work_table}.work_id"], ondelete="RESTRICT"
        ),
        schema="manual_import",
    )
    op.create_index(
        "ix_plan_admission_items_child_work",
        "plan_admission_items",
        ["child_work_id"],
        unique=True,
        schema="manual_import",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plan_admission_items_child_work",
        table_name="plan_admission_items",
        schema="manual_import",
    )
    op.drop_table("plan_admission_items", schema="manual_import")
    op.drop_table("plan_admissions", schema="manual_import")
'''
    (VERSIONS / f"{revision}_manual_import_plan_admissions.py").write_text(
        content, encoding="utf-8"
    )


def _fix_replay_result() -> None:
    path = "packages/collection_infrastructure/src/collection_infrastructure/postgres/manual_import_admission.py"
    content = _read(path)
    content = content.replace(
        'return _result(existing, status="already_applied")',
        'return _result(connection, existing, status="already_applied")',
    )
    old = '''def _result(row: Mapping[str, object], *, status: str) -> ManualImportAdmissionResult:
    return ManualImportAdmissionResult(
        admission_id=UUID(str(row["admission_id"])),
        plan_digest=str(row["plan_digest"]),
        child_work_ids=(),
        status=status,
        result_digest=str(row["result_digest"]),
    )
'''
    new = '''def _result(
    connection: Connection, row: Mapping[str, object], *, status: str
) -> ManualImportAdmissionResult:
    child_work_ids = tuple(
        connection.execute(
            sa.select(plan_admission_items.c.child_work_id)
            .where(plan_admission_items.c.admission_id == row["admission_id"])
            .order_by(plan_admission_items.c.position)
        ).scalars()
    )
    return ManualImportAdmissionResult(
        admission_id=UUID(str(row["admission_id"])),
        plan_digest=str(row["plan_digest"]),
        child_work_ids=child_work_ids,
        status=status,
        result_digest=str(row["result_digest"]),
    )
'''
    if old in content:
        content = content.replace(old, new)
    _write(path, content)


def _update_status() -> None:
    path = "docs/implementation-status.md"
    content = _read(path)
    line = (
        "| Manual plan admission | Deterministic blocked-plan rejection, exact replay, "
        "and transactional admission evidence for one child work unit per accepted record |\n"
    )
    if line not in content:
        marker = "| Manual import worker |"
        index = content.find(marker)
        if index >= 0:
            end = content.find("\n", index) + 1
            content = content[:end] + line + content[end:]
    _write(path, content)


def main() -> None:
    _export_application()
    _add_migration()
    _fix_replay_result()
    _update_status()


if __name__ == "__main__":
    main()
