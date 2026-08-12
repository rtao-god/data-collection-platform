from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def refine_application() -> None:
    path = ROOT / (
        "packages/collection_application/src/collection_application/"
        "manual_import_admission.py"
    )
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    if "_MAX_CHILD_INPUT_BYTES" not in content:
        content = content.replace(
            '_CHILD_NAMESPACE = UUID("bd46bc6f-bd7b-5c60-b4d4-d4eb106e5417")',
            '_CHILD_NAMESPACE = UUID("bd46bc6f-bd7b-5c60-b4d4-d4eb106e5417")\n'
            "_MAX_CHILD_INPUT_BYTES = 256 * 1024",
            1,
        )
    anchor = '''    return ManualImportChildWork(
        work_id=work_id,
        semantic_key=semantic_key,
        input_digest=f"sha256:{sha256(payload).hexdigest()}",
        input_payload=payload,
        record=record,
    )
'''
    replacement = '''    if len(payload) > _MAX_CHILD_INPUT_BYTES:
        raise ValueError(
            "manual import record input exceeds the canonical child work payload limit"
        )
    return ManualImportChildWork(
        work_id=work_id,
        semantic_key=semantic_key,
        input_digest=f"sha256:{sha256(payload).hexdigest()}",
        input_payload=payload,
        record=record,
    )
'''
    if replacement not in content:
        if anchor not in content:
            raise RuntimeError("manual import child payload anchor was not found")
        content = content.replace(anchor, replacement, 1)
    path.write_text(content, encoding="utf-8")


def refine_worker_failure() -> None:
    path = ROOT / "apps/manual_import_worker/src/manual_import_worker/worker.py"
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    anchor = '''            self._gateway.fail(
                failure_lease,
                failure_kind=kind,
                code=code,
                message=str(exc) or type(exc).__name__,
                required_action=action,
            )
            raise
'''
    replacement = '''            try:
                self._gateway.fail(
                    failure_lease,
                    failure_kind=kind,
                    code=code,
                    message=str(exc) or type(exc).__name__,
                    required_action=action,
                )
            except Exception as report_error:
                exc.add_note(
                    "The worker also failed to persist its typed failure: "
                    f"{type(report_error).__name__}"
                )
                raise exc from report_error
            raise
'''
    if replacement not in content:
        if anchor not in content:
            raise RuntimeError("manual import worker failure anchor was not found")
        content = content.replace(anchor, replacement, 1)
    path.write_text(content, encoding="utf-8")


def refine_composition() -> None:
    path = ROOT / "apps/manual_import_worker/src/manual_import_worker/app.py"
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    anchor = '''            worker = ManualImportWorker(SourceWorkerGatewayAdapter(client), settings)
            worker.register()
            if arguments.once:
                result = worker.run_once()
'''
    replacement = '''            worker = ManualImportWorker(SourceWorkerGatewayAdapter(client), settings)
            if arguments.once:
                worker.register()
                result = worker.run_once()
'''
    if replacement not in content:
        if anchor not in content:
            raise RuntimeError("manual import composition anchor was not found")
        content = content.replace(anchor, replacement, 1)
    path.write_text(content, encoding="utf-8")


def add_payload_limit_test() -> None:
    path = ROOT / (
        "packages/collection_application/tests/test_manual_import_admission.py"
    )
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8")
    test = '''

def test_oversized_record_payload_is_rejected_before_persistence() -> None:
    record = ManualImportRecord(
        position=0,
        locator_kind="line",
        locator_value="1",
        record_digest=_RECORD_DIGEST,
        values={"description": "x" * (256 * 1024)},
    )
    store = _RecordingStore()

    with pytest.raises(ValueError, match="child work payload limit"):
        ManualImportAdmissionService(store).admit(_command(records=(record,)))

    assert store.children is None
'''
    if "test_oversized_record_payload_is_rejected" not in content:
        path.write_text(content.rstrip() + test + "\n", encoding="utf-8")


def main() -> None:
    refine_application()
    refine_worker_failure()
    refine_composition()
    add_payload_limit_test()


if __name__ == "__main__":
    main()
