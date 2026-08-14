from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()


def main() -> int:
    path = ROOT / "packages/collection_infrastructure/pyproject.toml"
    text = path.read_text(encoding="utf-8")
    marker = "[tool.uv.sources]\n"
    if marker not in text:
        raise RuntimeError("collection-infrastructure uv sources section is missing")
    required = (
        "review-application",
        "review-contracts",
        "review-core",
    )
    additions = ""
    for distribution in required:
        declaration = f"{distribution} = {{ workspace = true }}\n"
        if declaration not in text:
            additions += declaration
    path.write_text(text + additions, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
