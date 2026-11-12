from __future__ import annotations

import sys
from pathlib import Path

# --import-mode=importlib leaves sibling test modules off sys.path, so shared
# database test support must be registered explicitly before any test imports it.
_SUPPORT_ROOT = Path(__file__).resolve().parent
if str(_SUPPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_ROOT))
