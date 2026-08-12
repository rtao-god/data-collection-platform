from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _path(value: str) -> Path:
    return ROOT / value


def _replace_once(path: Path, old: str, new: str) -> None:
    content = path.read_text(encoding="utf-8")
    if new in content:
        return
    if old not in content:
        raise RuntimeError(f"missing hardening anchor in {path}: {old[:80]!r}")
    path.write_text(content.replace(old, new, 1), encoding="utf-8")


def harden_store() -> None:
    path = _path(
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/manual_import_admission.py"
    )
    if not path.exists():
        raise RuntimeError("manual import admission store does not exist")
    content = path.read_text(encoding="utf-8")
    content = content.replace(
        "from collections.abc import Mapping, Sequence",
        "from collections.abc import Callable, Mapping, Sequence",
    )
    content = content.replace(
        "def __init__(self, engine: Engine, child_writer: ManualImportChildWorkWriter) -> None:\n"
        "        self._engine = engine\n"
        "        self._child_writer = child_writer",
        "def __init__(\n"
        "        self,\n"
        "        engine: Engine,\n"
        "        child_writer: ManualImportChildWorkWriter,\n"
        "        *,\n"
        "        clock: Callable[[], datetime] | None = None,\n"
        "    ) -> None:\n"
        "        self._engine = engine\n"
        "        self._child_writer = child_writer\n"
        "        self._clock = clock or (lambda: datetime.now(UTC))",
    )
    content = content.replace(
        "                admitted_at_utc = datetime.now(UTC)",
        "                admitted_at_utc = self._now_utc()",
    )
    old_load = '''    @staticmethod
    def _load_existing(
        connection: Connection, command: AdmitManualImportPlan
    ) -> RowMapping | None:
        return (
            connection.execute(
                sa.select(plan_admissions)
                .where(
                    sa.or_(
                        plan_admissions.c.admission_id == command.admission_id,
                        sa.and_(
                            plan_admissions.c.parent_work_id == command.parent_work_id,
                            plan_admissions.c.plan_artifact_id
                            == command.plan.plan_artifact_id,
                        ),
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
'''
    new_load = '''    @staticmethod
    def _load_existing(
        connection: Connection, command: AdmitManualImportPlan
    ) -> RowMapping | None:
        by_id = (
            connection.execute(
                sa.select(plan_admissions)
                .where(plan_admissions.c.admission_id == command.admission_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if by_id is not None:
            return by_id
        return (
            connection.execute(
                sa.select(plan_admissions)
                .where(
                    plan_admissions.c.parent_work_id == command.parent_work_id,
                    plan_admissions.c.plan_artifact_id
                    == command.plan.plan_artifact_id,
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
'''
    if old_load in content:
        content = content.replace(old_load, new_load, 1)
    parent_anchor = '''        if "run_id" in parent and parent["run_id"] != command.run_id:
            raise _conflict(
                code="MANUAL_IMPORT_PARENT_RUN_MISMATCH",
                message="The parent work unit belongs to a different collection run.",
                command=command,
                required_action="Use the run identity owned by the parent work unit.",
            )
'''
    parent_checks = parent_anchor + '''        if "capability" in parent and str(parent["capability"]) != "manual_import":
            raise _conflict(
                code="MANUAL_IMPORT_PARENT_CAPABILITY_MISMATCH",
                message="The parent work unit is not a manual import work unit.",
                command=command,
                required_action="Admit only a plan produced by manual import work.",
            )
        if "state" in parent and str(parent["state"]) != "succeeded":
            raise _conflict(
                code="MANUAL_IMPORT_PARENT_NOT_SUCCEEDED",
                message="The parent manual import work unit has not succeeded.",
                command=command,
                required_action="Complete and verify the parent work before plan admission.",
            )
'''
    if parent_checks not in content:
        if parent_anchor not in content:
            raise RuntimeError("parent work validation anchor was not found")
        content = content.replace(parent_anchor, parent_checks, 1)
    artifact_anchor = '''        if existing_ids != artifact_ids:
            raise _conflict(
                code="MANUAL_IMPORT_ARTIFACT_NOT_FOUND",
                message="The plan or source artifact is not verified in Collection metadata.",
                command=command,
                required_action="Verify both exact artifacts before admitting the plan.",
            )
        _require_artifact_binding(connection, command, input_binding=True)
'''
    artifact_checks = '''        if existing_ids != artifact_ids:
            raise _conflict(
                code="MANUAL_IMPORT_ARTIFACT_NOT_FOUND",
                message="The plan or source artifact is not verified in Collection metadata.",
                command=command,
                required_action="Verify both exact artifacts before admitting the plan.",
            )
        _require_artifact_digests(connection, command, raw_artifacts)
        _require_artifact_binding(connection, command, input_binding=True)
'''
    if artifact_checks not in content:
        if artifact_anchor not in content:
            raise RuntimeError("artifact validation anchor was not found")
        content = content.replace(artifact_anchor, artifact_checks, 1)
    now_method = '''
    def _now_utc(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Manual Import Admission clock must return UTC")
        return value
'''
    if now_method not in content:
        marker = "\ndef _require_artifact_binding("
        if marker not in content:
            raise RuntimeError("admission helper boundary was not found")
        content = content.replace(marker, now_method + marker, 1)
    digest_helper = '''

def _require_artifact_digests(
    connection: Connection,
    command: AdmitManualImportPlan,
    raw_artifacts: sa.Table,
) -> None:
    digest_column = next(
        (
            raw_artifacts.c[name]
            for name in ("content_digest", "artifact_digest", "digest")
            if name in raw_artifacts.c
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
    rows = {
        row["artifact_id"]: str(row["content_digest"])
        for row in connection.execute(
            sa.select(
                raw_artifacts.c.artifact_id,
                digest_column.label("content_digest"),
            ).where(
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
        if rows.get(artifact_id) != digest
    )
    if mismatches:
        raise _conflict(
            code="MANUAL_IMPORT_ARTIFACT_DIGEST_MISMATCH",
            message="The plan or source artifact digest does not match admission input.",
            command=command,
            required_action="Use the exact verified artifact identities and digests.",
            mismatches=mismatches,
        )
'''
    if digest_helper not in content:
        marker = "\ndef _require_artifact_binding("
        content = content.replace(marker, digest_helper + marker, 1)
    path.write_text(content, encoding="utf-8")


def harden_transactional_enqueue() -> None:
    path = _path(
        "packages/collection_infrastructure/src/collection_infrastructure/"
        "postgres/manual_import_child_writer.py"
    )
    if not path.exists():
        raise RuntimeError("manual import child writer does not exist")
    content = path.read_text(encoding="utf-8")
    old = '''    method = candidates[0][1]

    def invoke(connection: Connection, command: object) -> object:
        return method(connection, command)

    return invoke
'''
    new = '''    method = candidates[0][1]
    parameters = tuple(inspect.signature(method).parameters.values())

    def invoke(connection: Connection, command: object) -> object:
        arguments: list[object] = [connection, command]
        keywords: dict[str, object] = {}
        for parameter in parameters[2:]:
            if parameter.default is not inspect.Parameter.empty:
                continue
            if parameter.name in {"now", "now_utc", "created_at_utc"}:
                keywords[parameter.name] = datetime.now(UTC)
                continue
            raise RuntimeError(
                "Work Engine transaction-local enqueue has unsupported required field "
                f"{parameter.name!r}"
            )
        return method(*arguments, **keywords)

    return invoke
'''
    if new not in content:
        if old not in content:
            raise RuntimeError("transactional enqueue adapter anchor was not found")
        content = content.replace(old, new, 1)
    path.write_text(content, encoding="utf-8")


def harden_migration() -> None:
    migrations = [
        path
        for path in sorted(
            _path(
                "packages/collection_infrastructure/src/collection_infrastructure/"
                "postgres/migrations/versions"
            ).glob("*.py")
        )
        if "plan_admissions" in path.read_text(encoding="utf-8")
    ]
    if len(migrations) != 1:
        raise RuntimeError(f"expected one admission migration, found {migrations}")
    path = migrations[0]
    content = path.read_text(encoding="utf-8")
    if "manual_import.reject_admission_mutation" in content:
        return
    upgrade_anchor = '''    op.create_index(
        "ix_plan_admission_items_child_work",
        "plan_admission_items",
        ["child_work_id"],
        unique=True,
        schema="manual_import",
    )
'''
    upgrade = upgrade_anchor + '''    op.execute(
        """
        CREATE FUNCTION manual_import.reject_admission_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'manual import admission evidence is immutable';
        END
        $function$;

        CREATE FUNCTION manual_import.validate_admission_item_count()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            target_admission uuid;
            expected_count integer;
            actual_count integer;
        BEGIN
            target_admission := COALESCE(NEW.admission_id, OLD.admission_id);
            SELECT child_work_count
              INTO expected_count
              FROM manual_import.plan_admissions
             WHERE admission_id = target_admission;
            IF expected_count IS NULL THEN
                RETURN COALESCE(NEW, OLD);
            END IF;
            SELECT count(*)
              INTO actual_count
              FROM manual_import.plan_admission_items
             WHERE admission_id = target_admission;
            IF actual_count <> expected_count THEN
                RAISE EXCEPTION
                    'manual import admission % expected % items but has %',
                    target_admission,
                    expected_count,
                    actual_count;
            END IF;
            RETURN COALESCE(NEW, OLD);
        END
        $function$;

        CREATE TRIGGER plan_admissions_immutable
        BEFORE UPDATE OR DELETE ON manual_import.plan_admissions
        FOR EACH ROW EXECUTE FUNCTION manual_import.reject_admission_mutation();

        CREATE TRIGGER plan_admission_items_immutable
        BEFORE UPDATE OR DELETE ON manual_import.plan_admission_items
        FOR EACH ROW EXECUTE FUNCTION manual_import.reject_admission_mutation();

        CREATE CONSTRAINT TRIGGER plan_admissions_item_count
        AFTER INSERT ON manual_import.plan_admissions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION manual_import.validate_admission_item_count();

        CREATE CONSTRAINT TRIGGER plan_admission_items_item_count
        AFTER INSERT ON manual_import.plan_admission_items
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION manual_import.validate_admission_item_count();
        """
    )
'''
    if upgrade_anchor not in content:
        raise RuntimeError("admission migration upgrade anchor was not found")
    content = content.replace(upgrade_anchor, upgrade, 1)
    downgrade_anchor = '''def downgrade() -> None:
    op.drop_index(
'''
    downgrade = '''def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS plan_admission_items_item_count
            ON manual_import.plan_admission_items;
        DROP TRIGGER IF EXISTS plan_admissions_item_count
            ON manual_import.plan_admissions;
        DROP TRIGGER IF EXISTS plan_admission_items_immutable
            ON manual_import.plan_admission_items;
        DROP TRIGGER IF EXISTS plan_admissions_immutable
            ON manual_import.plan_admissions;
        DROP FUNCTION IF EXISTS manual_import.validate_admission_item_count();
        DROP FUNCTION IF EXISTS manual_import.reject_admission_mutation();
        """
    )
    op.drop_index(
'''
    if downgrade_anchor not in content:
        raise RuntimeError("admission migration downgrade anchor was not found")
    content = content.replace(downgrade_anchor, downgrade, 1)
    path.write_text(content, encoding="utf-8")


def add_duplicate_record_test() -> None:
    path = _path(
        "packages/collection_application/tests/test_manual_import_admission.py"
    )
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    test = '''

def test_identical_records_at_different_positions_create_distinct_child_work() -> None:
    first = _record(position=0)
    second = ManualImportRecord(
        position=1,
        locator_kind="line",
        locator_value="2",
        record_digest=first.record_digest,
        values=first.values,
    )
    store = _RecordingStore()

    result = ManualImportAdmissionService(store).admit(
        _command(records=(first, second))
    )

    assert len(result.child_work_ids) == 2
    assert result.child_work_ids[0] != result.child_work_ids[1]
'''
    if "test_identical_records_at_different_positions" not in content:
        path.write_text(content.rstrip() + test + "\n", encoding="utf-8")


def main() -> None:
    harden_store()
    harden_transactional_enqueue()
    harden_migration()
    add_duplicate_record_test()


if __name__ == "__main__":
    main()
