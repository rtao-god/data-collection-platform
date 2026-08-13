from __future__ import annotations

import re
from pathlib import Path

ROOT = Path.cwd()


def replace(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path}: expected source fragment is missing")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace(
    "packages/browser_contracts/src/browser_contracts/contracts.py",
    "from typing import Literal, Self\n",
    "from typing import Literal, Self, cast\n",
)
replace(
    "packages/browser_contracts/src/browser_contracts/contracts.py",
    '''        return BrowserOrigin(
            scheme=scheme,
''',
    '''        return BrowserOrigin(
            scheme=cast(Literal["http", "https"], scheme),
''',
)
replace(
    "packages/browser_contracts/src/browser_contracts/contracts.py",
    '''            resultState=(
                "completed_with_blocked_subresources"
                if blocked
                else "completed"
            ),
''',
    '''            resultState=(
                "completed_with_blocked_subresources"
                if runtime.blocked_request_count
                else "completed"
            ),
''',
)
replace(
    "packages/browser_security/src/browser_security/guard.py",
    '''        if getattr(address, "ipv4_mapped", None) is not None:
            address = address.ipv4_mapped  # type: ignore[assignment]
        if not address.is_global:
''',
    '''        mapped = getattr(address, "ipv4_mapped", None)
        checked_address = mapped if mapped is not None else address
        if not checked_address.is_global:
''',
)
replace(
    "connectors/playwright_browser/src/playwright_browser/executor.py",
    "from typing import cast\n\n",
    "",
)

# The transformed composition root must be browser-only and must construct the
# Stage 9 executor. Flexible constructor aliases preserve the existing generic
# worker bootstrap without duplicating SDK configuration ownership.
app_path = ROOT / "apps/browser_worker/src/browser_worker/app.py"
app = app_path.read_text(encoding="utf-8")
for forbidden in (
    "extraction_core",
    "ExtractionEngine",
    "SdkExtractionWorkerGateway",
    "ExtractionWorker",
    "extraction_worker",
):
    if forbidden in app:
        raise RuntimeError(f"browser worker composition retained {forbidden}")
if "PlaywrightBrowserExecutor" not in app:
    raise RuntimeError("browser worker composition does not create Playwright executor")
if "SdkBrowserWorkerGateway" not in app or "BrowserWorker" not in app:
    raise RuntimeError("browser worker composition root is incomplete")

worker_project = ROOT / "apps/browser_worker/pyproject.toml"
worker_text = worker_project.read_text(encoding="utf-8")
if 'name = "browser-worker"' not in worker_text:
    raise RuntimeError("browser worker distribution identity is invalid")
if 'browser-worker = "browser_worker.main:main"' not in worker_text:
    scripts_start = worker_text.find("[project.scripts]")
    if scripts_start < 0:
        worker_text += '\n[project.scripts]\nbrowser-worker = "browser_worker.main:main"\n'
    else:
        scripts_end = worker_text.find("\n[", scripts_start + 1)
        if scripts_end < 0:
            scripts_end = len(worker_text)
        block = worker_text[scripts_start:scripts_end]
        block = re.sub(
            r"^[a-z0-9-]+\s*=\s*\"[^\"]+\"$",
            'browser-worker = "browser_worker.main:main"',
            block,
            count=1,
            flags=re.M,
        )
        worker_text = worker_text[:scripts_start] + block + worker_text[scripts_end:]
worker_project.write_text(worker_text, encoding="utf-8")

connector_project = ROOT / "connectors/playwright_browser/pyproject.toml"
connector_text = connector_project.read_text(encoding="utf-8")
if 'name = "playwright-browser-connector"' not in connector_text:
    raise RuntimeError("Playwright connector distribution identity is invalid")
if "__PLAYWRIGHT_VERSION__" not in connector_text:
    raise RuntimeError("Playwright version placeholder is missing")

# Fail closed if the HTTP acquisition owner can import Stage 9 through its
# architecture policy. Existing policy must remain unchanged and excludes all
# browser packages by construction.
checker_path = ROOT / "tools/architecture_checks/check_dependencies.py"
checker = checker_path.read_text(encoding="utf-8")
http_start = checker.index('    "http_worker": OwnerPolicy(')
http_end = checker.index("    ),", http_start) + len("    ),")
http_policy = checker[http_start:http_end]
for forbidden in (
    "browser_contracts",
    "browser_security",
    "playwright_browser",
):
    if forbidden in http_policy:
        raise RuntimeError(f"HTTP worker architecture policy admits {forbidden}")

# The capability contract must be canonical before generated wire artifacts are
# refreshed.
capability_matches = []
for path in (ROOT / "packages/collection_contracts/src/collection_contracts").rglob("*.py"):
    text = path.read_text(encoding="utf-8")
    if "WorkCapability" in text and '"browser_fetch"' in text:
        capability_matches.append(path)
if len(capability_matches) != 1:
    raise RuntimeError(
        f"browser_fetch canonical capability owner count is {len(capability_matches)}"
    )
