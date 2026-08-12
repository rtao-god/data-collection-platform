from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / (
    "packages/collection_infrastructure/src/collection_infrastructure/"
    "postgres/manual_import_admission.py"
)


def main() -> None:
    if not PATH.exists():
        raise RuntimeError("manual import admission store does not exist")
    content = PATH.read_text(encoding="utf-8")
    content = content.replace(
        "_require_artifact_digests(connection, command, raw_artifacts)",
        "_require_artifact_digests(connection, command)",
    )
    pattern = re.compile(
        r"\ndef _require_artifact_digests\(.*?(?=\ndef _require_artifact_binding\()",
        re.DOTALL,
    )
    replacement = '''
def _require_artifact_digests(
    connection: Connection,
    command: AdmitManualImportPlan,
) -> None:
    raw_artifacts = artifact_metadata.raw_artifacts
    artifact_objects = getattr(artifact_metadata, "artifact_objects", None)
    raw_digest = next(
        (
            raw_artifacts.c[name]
            for name in ("content_digest", "artifact_digest", "digest")
            if name in raw_artifacts.c
        ),
        None,
    )
    selectable: sa.FromClause = raw_artifacts
    digest_column: sa.ColumnElement[object] | None = raw_digest
    kind_column: sa.ColumnElement[object] | None = next(
        (
            raw_artifacts.c[name]
            for name in ("artifact_kind", "kind")
            if name in raw_artifacts.c
        ),
        None,
    )
    if digest_column is None and isinstance(artifact_objects, sa.Table):
        object_digest = next(
            (
                artifact_objects.c[name]
                for name in ("content_digest", "artifact_digest", "digest")
                if name in artifact_objects.c
            ),
            None,
        )
        if "object_id" in raw_artifacts.c and "object_id" in artifact_objects.c:
            selectable = raw_artifacts.join(
                artifact_objects,
                artifact_objects.c.object_id == raw_artifacts.c.object_id,
            )
            digest_column = object_digest
            if kind_column is None:
                kind_column = next(
                    (
                        artifact_objects.c[name]
                        for name in ("artifact_kind", "kind")
                        if name in artifact_objects.c
                    ),
                    None,
                )
    if digest_column is None:
        raise _conflict(
            code="MANUAL_IMPORT_ARTIFACT_DIGEST_UNAVAILABLE",
            message="Artifact metadata does not expose a canonical digest column.",
            command=command,
            required_action="Apply the canonical artifact metadata contract before admission.",
        )
    columns = [
        raw_artifacts.c.artifact_id,
        digest_column.label("content_digest"),
    ]
    if kind_column is not None:
        columns.append(kind_column.label("artifact_kind"))
    rows = {
        row["artifact_id"]: row
        for row in connection.execute(
            sa.select(*columns)
            .select_from(selectable)
            .where(
                raw_artifacts.c.artifact_id.in_(
                    {
                        command.plan.plan_artifact_id,
                        command.plan.source_artifact_id,
                    }
                )
            )
        ).mappings()
    }
    expected = {
        command.plan.plan_artifact_id: command.plan.plan_digest,
        command.plan.source_artifact_id: command.plan.source_digest,
    }
    mismatches = sorted(
        str(artifact_id)
        for artifact_id, digest in expected.items()
        if artifact_id not in rows
        or str(rows[artifact_id]["content_digest"]) != digest
    )
    if mismatches:
        raise _conflict(
            code="MANUAL_IMPORT_ARTIFACT_DIGEST_MISMATCH",
            message="The plan or source artifact digest does not match admission input.",
            command=command,
            required_action="Use the exact verified artifact identities and digests.",
            mismatches=mismatches,
        )
    if kind_column is not None:
        plan_kind = str(rows[command.plan.plan_artifact_id]["artifact_kind"])
        if plan_kind != "derived_artifact":
            raise _conflict(
                code="MANUAL_IMPORT_PLAN_ARTIFACT_KIND_INVALID",
                message="The admitted plan is not stored as a derived artifact.",
                command=command,
                required_action="Admit the verified manual import plan output artifact.",
            )
'''
    if pattern.search(content) is None:
        marker = "\ndef _require_artifact_binding("
        if marker not in content:
            raise RuntimeError("artifact binding helper was not found")
        content = content.replace(marker, replacement + marker, 1)
    else:
        content = pattern.sub(replacement, content, count=1)
    PATH.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
