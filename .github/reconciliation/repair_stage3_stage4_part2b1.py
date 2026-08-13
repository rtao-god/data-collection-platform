from __future__ import annotations

from pathlib import Path


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected source fragment is missing")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def apply(root: Path) -> None:
    admission = root / (
        "packages/collection_infrastructure/src/collection_infrastructure/postgres/"
        "manual_import_admission.py"
    )
    _replace_once(
        admission,
        "        raw_artifacts = artifact_metadata.raw_artifacts\n",
        "        raw_artifacts = artifact_metadata.artifact_records\n",
    )
    _replace_once(
        admission,
        "def _result(row: Mapping[str, object], *, status: str) -> ManualImportAdmissionResult:\n",
        "def _result(row: RowMapping, *, status: str) -> ManualImportAdmissionResult:\n",
    )

    work_engine = root / (
        "packages/collection_infrastructure/src/collection_infrastructure/postgres/work_engine.py"
    )
    if "def enqueue_work_in_transaction(" not in work_engine.read_text(encoding="utf-8"):
        _replace_once(
            work_engine,
            "    def enqueue_work(self, command: WorkUnitSpec) -> None:\n"
            "        self._transaction(\n"
            "            lambda connection, now_utc: self._enqueue_work(connection, now_utc, command)\n"
            "        )\n\n",
            "    def enqueue_work(self, command: WorkUnitSpec) -> None:\n"
            "        self._transaction(\n"
            "            lambda connection, now_utc: self._enqueue_work(connection, now_utc, command)\n"
            "        )\n\n"
            "    def enqueue_work_in_transaction(\n"
            "        self,\n"
            "        connection: Connection,\n"
            "        command: WorkUnitSpec,\n"
            "    ) -> None:\n"
            "        \"\"\"Enqueue work inside a transaction owned by a higher-level use case.\"\"\"\n"
            "        self._enqueue_work(connection, self._now_utc(), command)\n\n",
        )
