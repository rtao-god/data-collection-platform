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
    part1 = ROOT / ".github/stage8a_apply_20260814/part1.py"
    part2 = ROOT / ".github/stage8a_apply_20260814/part2.py"
    if part1.exists():
        subprocess.run([sys.executable, str(part1)], check=True)
    if part2.exists():
        subprocess.run([sys.executable, str(part2)], check=True)

    transitions = ROOT / "packages/review_core/src/review_core/transitions.py"
    text = transitions.read_text(encoding="utf-8")
    anchor = '''    if identity != current.suppression_id:
        raise SuppressionTransitionError("suppression identity cannot change on resolution")
    return SuppressionRevision(
'''
    replacement = '''    if identity != current.suppression_id:
        raise SuppressionTransitionError("suppression identity cannot change on resolution")
    if command.expires_at_utc != current.expires_at_utc:
        raise SuppressionTransitionError("suppression expiry cannot change on resolution")
    return SuppressionRevision(
'''
    if "suppression expiry cannot change on resolution" not in text:
        if anchor not in text:
            raise RuntimeError("suppression resolution identity anchor is missing")
        transitions.write_text(text.replace(anchor, replacement, 1), encoding="utf-8")

    schema_test = ROOT / "database/tests/test_candidate_review_schema.py"
    text = schema_test.read_text(encoding="utf-8")
    text = text.replace("ARRAY[:evidence]", "ARRAY[CAST(:evidence AS text)]")
    text = text.replace(
        "with pytest.raises((InternalError, sa.exc.DBAPIError)), engine.begin() as connection:",
        "with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:",
    )
    text = text.replace("from sqlalchemy.exc import IntegrityError, InternalError\n", "from sqlalchemy.exc import IntegrityError\n")
    schema_test.write_text(text, encoding="utf-8")

    core_test = ROOT / "packages/review_core/tests/test_transitions.py"
    text = core_test.read_text(encoding="utf-8")
    marker = "\ndef test_suppression_resolution_is_revision_guarded() -> None:\n"
    if "test_suppression_resolution_rejects_expiry_change" not in text:
        addition = '''

def test_suppression_resolution_rejects_expiry_change() -> None:
    active = activate_suppression(suppression_command(expected_revision=None), now_utc=NOW)
    values = {
        "target_kind": "candidate",
        "target_id": "candidate-1",
        "scopes": ("discovery", "export"),
        "reason_code": "LEGAL_REVIEW",
        "actor_id": "reviewer",
        "evidence_reference": DIGEST_B,
        "expected_revision": 0,
        "expires_at_utc": NOW + timedelta(days=2),
    }
    command = SuppressionCommand(
        **values,
        command_digest=suppression_command_digest(**values),
        correlation_id="suppression-test",
    )
    with pytest.raises(Exception, match="expiry cannot change"):
        resolve_suppression(active, command, now_utc=NOW + timedelta(minutes=1))
'''
        if marker not in text:
            raise RuntimeError("suppression resolution test insertion point is missing")
        core_test.write_text(text.replace(marker, addition + marker, 1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
