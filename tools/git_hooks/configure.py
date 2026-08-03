from __future__ import annotations

import subprocess


def main() -> int:
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
