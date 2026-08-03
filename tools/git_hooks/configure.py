from __future__ import annotations

import shutil
import subprocess


def main() -> int:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is unavailable")
    subprocess.run(
        [git, "config", "core.hooksPath", ".githooks"],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
