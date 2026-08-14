from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()


def main() -> int:
    repair = ROOT / ".github/stage8b_apply_20260814/repair.py"
    if repair.exists():
        subprocess.run([sys.executable, str(repair)], check=True)

    main_path = ROOT / "apps/control_api/src/control_api/main.py"
    text = main_path.read_text(encoding="utf-8")
    if "from fastapi import FastAPI\n" not in text:
        text = text.replace("import sqlalchemy as sa\n", "import sqlalchemy as sa\nfrom fastapi import FastAPI\n", 1)
    text = text.replace("def create_runtime_app():\n", "def create_runtime_app() -> FastAPI:\n")
    main_path.write_text(text, encoding="utf-8")

    generator = ROOT / "tools/control_api_contract_generation/generate.py"
    text = generator.read_text(encoding="utf-8")
    text = text.replace("    operations = []\n", "    operations: list[dict[str, str]] = []\n")
    generator.write_text(text, encoding="utf-8")

    cursors = ROOT / "packages/review_application/src/review_application/cursors.py"
    text = cursors.read_text(encoding="utf-8")
    old = '''        return ReviewQueueCursor(
            recorded_at_utc=datetime.fromisoformat(payload["recordedAtUtc"]),
            case_id=UUID(payload["caseId"]),
        )
'''
    new = '''        recorded_at_utc = datetime.fromisoformat(payload["recordedAtUtc"])
        if recorded_at_utc.tzinfo is None or recorded_at_utc.utcoffset() is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return ReviewQueueCursor(
            recorded_at_utc=recorded_at_utc,
            case_id=UUID(payload["caseId"]),
        )
'''
    if old in text:
        text = text.replace(old, new, 1)
    cursors.write_text(text, encoding="utf-8")

    repository = ROOT / (
        "packages/collection_infrastructure/src/collection_infrastructure/postgres/"
        "review_repository.py"
    )
    text = repository.read_text(encoding="utf-8")
    text = text.replace(
        "        scopes=tuple(sorted(scopes)),\n",
        "        scopes=tuple(sorted(scopes)),  # type: ignore[arg-type]\n",
        1,
    )
    repository.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
