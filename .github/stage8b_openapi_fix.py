from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()


def main() -> int:
    path = ROOT / "apps/control_api/src/control_api/app.py"
    text = path.read_text(encoding="utf-8")
    prefix = "from __future__ import annotations\n\n"
    if not text.startswith(prefix):
        raise RuntimeError("Control API postponed-annotation header is missing")
    path.write_text(text[len(prefix) :], encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
