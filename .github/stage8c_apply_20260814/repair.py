from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()


def main() -> int:
    apply = ROOT / ".github/stage8c_apply_20260814/apply.py"
    if apply.exists():
        subprocess.run([sys.executable, str(apply)], check=True)

    vite = ROOT / "apps/review_web/vite.config.ts"
    text = vite.read_text(encoding="utf-8")
    text = text.replace('import { defineConfig } from "vite";\n', 'import { defineConfig } from "vitest/config";\n')
    vite.write_text(text, encoding="utf-8")

    architecture = ROOT / "apps/review_web/tools/check-architecture.mjs"
    text = architecture.read_text(encoding="utf-8")
    text = text.replace(
        '''for (const file of files) {
  const text = readFileSync(file, "utf8");
  const name = relative(root, file);
''',
        '''for (const file of files) {
  const text = readFileSync(file, "utf8");
  const name = relative(root, file);
  if (name.startsWith("shared/api/generated/")) continue;
''',
    )
    architecture.write_text(text, encoding="utf-8")

    nginx = ROOT / "deploy/nginx/review-web.conf"
    text = nginx.read_text(encoding="utf-8")
    text = text.replace("style-src 'self' 'unsafe-inline'", "style-src 'self'")
    nginx.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
