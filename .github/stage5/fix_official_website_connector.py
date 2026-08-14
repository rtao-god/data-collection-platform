from __future__ import annotations

from pathlib import Path

path = Path("connectors/official_website_http/src/official_website_http/core.py")
text = path.read_text(encoding="utf-8")
text = text.replace("import gzip\n", "")
text = text.replace(
    "except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:",
    "except (OSError, http.client.HTTPException) as exc:",
)
path.write_text(text, encoding="utf-8")
