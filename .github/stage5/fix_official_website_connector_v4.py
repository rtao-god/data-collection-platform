from __future__ import annotations

from pathlib import Path

path = Path("connectors/official_website_http/src/official_website_http/core.py")
text = path.read_text(encoding="utf-8")
text = text.replace("import gzip\n", "")
text = text.replace(
    "except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:",
    "except (OSError, http.client.HTTPException) as exc:",
)
text = text.replace(
    "        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:\n"
    "            address = address.ipv4_mapped\n"
    "        if not address.is_global:\n",
    "        normalized_address: ipaddress.IPv4Address | ipaddress.IPv6Address = address\n"
    "        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:\n"
    "            normalized_address = address.ipv4_mapped\n"
    "        if not normalized_address.is_global:\n",
)
text = text.replace(
    '                f"DNS resolved to non-public address {address.compressed}",\n',
    '                f"DNS resolved to non-public address {normalized_address.compressed}",\n',
)
text = text.replace(
    "            if declared is not None:\n"
    "                try:\n"
    "                    if int(declared) > request.maximum_wire_bytes:\n"
    "                        raise _limit(\"HTTP_WIRE_SIZE_EXCEEDED\")\n",
    "            if declared is not None:\n"
    "                try:\n"
    "                    declared_size = int(declared)\n"
    "                    if declared_size < 0:\n"
    "                        raise ValueError(\"negative Content-Length\")\n"
    "                    if declared_size > request.maximum_wire_bytes:\n"
    "                        raise _limit(\"HTTP_WIRE_SIZE_EXCEEDED\")\n",
)
path.write_text(text, encoding="utf-8")
