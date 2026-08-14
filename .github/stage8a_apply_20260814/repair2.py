from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()


def main() -> int:
    repair = ROOT / ".github/stage8a_apply_20260814/repair.py"
    if repair.exists():
        subprocess.run([sys.executable, str(repair)], check=True)

    path = ROOT / "packages/review_core/tests/test_transitions.py"
    text = path.read_text(encoding="utf-8")
    old_import = '''    ReviewDecisionConflict,
    StaleReviewRevision,
'''
    new_import = '''    ReviewDecisionConflict,
    StaleReviewRevision,
    SuppressionTransitionError,
'''
    if "SuppressionTransitionError" not in text:
        if old_import not in text:
            raise RuntimeError("typed review error import anchor is missing")
        text = text.replace(old_import, new_import, 1)
    text = text.replace(
        'with pytest.raises(Exception, match="expiry cannot change"):',
        'with pytest.raises(SuppressionTransitionError, match="expiry cannot change"):',
    )
    path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
