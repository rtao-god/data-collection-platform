from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected source fragment is missing")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> int:
    for name in ("part1.py", "part2.py", "part3a.py", "part3b.py", "part3c.py"):
        path = ROOT / ".github/stage8b_apply_20260814" / name
        if path.exists():
            subprocess.run([sys.executable, str(path)], check=True)

    service_test = ROOT / "packages/review_application/tests/test_service.py"
    text = service_test.read_text(encoding="utf-8")
    if "deterministic_suppression_id" not in text:
        text = text.replace(
            "from review_contracts import ManualObservation, ReviewCase, ReviewDecision, SuppressionRevision\n",
            "from review_contracts import SuppressionRevision, deterministic_suppression_id\n",
        )
    old = '''    suppression_id = uuid4()
    service.resolve_suppression(
'''
    new = '''    suppression_id = deterministic_suppression_id(
        "candidate",
        "candidate-1",
        ("discovery", "export"),
        "LEGAL_REVIEW",
    )
    service.resolve_suppression(
'''
    if old in text:
        text = text.replace(old, new, 1)
    service_test.write_text(text, encoding="utf-8")

    app_test = ROOT / "apps/control_api/tests/test_app.py"
    text = app_test.read_text(encoding="utf-8")
    if "deterministic_case_id" not in text:
        text = text.replace(
            "    ReviewDecision,\n    deterministic_decision_id,\n",
            "    ReviewDecision,\n    deterministic_case_id,\n    deterministic_decision_id,\n",
        )
    text = text.replace(
        '''class Service:
    def __init__(self) -> None:
        self.principal = None
        self.decision_call = None
''',
        '''class Service:
    def __init__(self) -> None:
        self.principal = None
        self.decision_call = None
        self.candidate_id = uuid4()
        self.case_id = deterministic_case_id(
            self.candidate_id,
            0,
            ("MATCH_REVIEW",),
        )
''',
    )
    text = text.replace(
        '''        case_id = values["case_id"]
        digest = review_decision_command_digest(
''',
        '''        case_id = values["case_id"]
        assert case_id == self.case_id
        digest = review_decision_command_digest(
''',
    )
    text = text.replace(
        "            candidate_id=uuid4(),\n",
        "            candidate_id=self.candidate_id,\n",
        1,
    )
    text = text.replace(
        '''    service = Service()
    case_id = uuid4()
    body = {
''',
        '''    service = Service()
    case_id = service.case_id
    body = {
''',
    )
    app_test.write_text(text, encoding="utf-8")

    repository = ROOT / (
        "packages/collection_infrastructure/src/collection_infrastructure/postgres/"
        "review_repository.py"
    )
    text = repository.read_text(encoding="utf-8")
    decision_anchor = '''    if (
        decision.case_id != command.case_id
        or decision.outcome != command.outcome
'''
    decision_replacement = '''    if (
        decision.case_id != command.case_id
        or decision.case_revision != command.expected_case_revision + 1
        or decision.outcome != command.outcome
'''
    if decision_anchor in text:
        text = text.replace(decision_anchor, decision_replacement, 1)
    suppression_anchor = '''    if (
        suppression.target_kind != command.target_kind
        or suppression.target_id != command.target_id
'''
    suppression_replacement = '''    expected_revision = (
        0 if command.expected_revision is None else command.expected_revision + 1
    )
    if (
        suppression.revision != expected_revision
        or suppression.target_kind != command.target_kind
        or suppression.target_id != command.target_id
'''
    if suppression_anchor in text:
        text = text.replace(suppression_anchor, suppression_replacement, 1)
    repository.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
