from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()


def main() -> int:
    apply_script = ROOT / ".github/stage7_apply_20260814/apply.py"
    if apply_script.exists():
        subprocess.run([sys.executable, str(apply_script)], check=True)

    path = ROOT / "packages/entity_resolution_core/tests/test_engine.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("def test_transitive_match_cannot_bypass_explicit_separation() -> None:\n")
    end = text.index("\n\ndef test_missing_cross_pair_is_fail_closed_for_transitive_union", start)
    replacement = '''def test_transitive_match_cannot_bypass_explicit_separation() -> None:
    first = CandidateSnapshot(
        candidate_id=uuid4(),
        entity_kind="place",
        normalized_name="one",
        phones=("+4930111",),
        domains=("one.example",),
    )
    second = CandidateSnapshot(
        candidate_id=uuid4(),
        entity_kind="place",
        normalized_name="two",
        phones=("+4930111", "+4930222"),
        domains=("one.example", "two.example"),
    )
    third = CandidateSnapshot(
        candidate_id=uuid4(),
        entity_kind="place",
        normalized_name="three",
        phones=("+4930222",),
        domains=("two.example",),
    )
    relations = {frozenset((first.candidate_id, third.candidate_id)): "conflict"}
    result = resolve_batch(request((first, second, third), relations))
    member_sets = {frozenset(cluster.member_candidate_ids) for cluster in result.clusters}
    assert frozenset((first.candidate_id, second.candidate_id, third.candidate_id)) not in member_sets
    assert len(result.clusters) == 2
'''
    path.write_text(text[:start] + replacement + text[end:], encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
