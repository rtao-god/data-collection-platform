"""Canonical JSON and digests of the backend ingestion contract, proven by golden vectors.

This is the producer side of `aggregator-candidate-ingestion` revision 3. The rules are not
invented here: they are the ones the backend's own `IngestionCanonicalJson` applies, and
every constant in `GOLDEN_VECTORS` was produced by running that implementation. A change
that breaks agreement fails the equivalence tests instead of failing in production after an
upload has already been accepted.

The owner grammar, as observed in `IngestionCanonicalJson` and `IngestionDocumentDigest`:

* camelCase property names, null properties omitted;
* object keys sorted by ordinal (UTF-16-ordered .NET `StringComparer.Ordinal`) comparison;
* keys and string values NFC-normalized; numbers written verbatim, not reformatted;
* enums as camelCase tokens; non-ASCII escaped the way the writer escapes it;
* digest = lowercase hex SHA-256 of those exact bytes, without any `sha256:` prefix.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

CONTRACT_IDENTITY = "aggregator-candidate-ingestion"
CONTRACT_REVISION = 3


class BackendCanonicalError(ValueError):
    """Raised when a document cannot be expressed in the backend's canonical form."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def canonical_json_bytes(document: object) -> bytes:
    """Serialize a document exactly as the backend canonicalizer writes it."""

    return _write(_normalize(document)).encode("utf-8")


def compute_digest(document: object) -> str:
    """The backend's document digest: lowercase hex SHA-256, no prefix."""

    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def compute_bytes_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def as_wire_decimal(value: Decimal) -> str:
    """Render a decimal the way the backend writes it.

    A trailing `.0` on an integral decimal is not what `decimal` writes, so the textual
    form is used verbatim: `52.5007771` stays `52.5007771` and `52` stays `52`.
    """

    if not value.is_finite():
        raise BackendCanonicalError(
            "BACKEND_DECIMAL_NOT_FINITE", "Canonical documents cannot carry non-finite numbers."
        )
    return format(value, "f")


def _float_text(value: float) -> str:
    """Render a float in the shortest round-tripping form the backend's writer emits."""

    if value != value or value in (float("inf"), float("-inf")):
        raise BackendCanonicalError(
            "BACKEND_DECIMAL_NOT_FINITE", "Canonical documents cannot carry non-finite numbers."
        )
    text = repr(value)
    return text[:-2] if text.endswith(".0") else text


def _normalize(value: object) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return _RawNumber(as_wire_decimal(value))
    if isinstance(value, int) and not isinstance(value, bool):
        return _RawNumber(str(value))
    if isinstance(value, float):
        return _RawNumber(_float_text(value))
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise BackendCanonicalError(
                    "BACKEND_KEY_NOT_TEXT", "Canonical JSON object keys must be strings."
                )
            # The owner omits an absent property instead of writing it as null, so a null
            # member must never reach the digest; a null list element is still written.
            if item is None:
                continue
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in result:
                raise BackendCanonicalError(
                    "BACKEND_KEY_COLLISION",
                    "Two canonical keys normalize to the same name.",
                )
            result[normalized_key] = _normalize(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_normalize(item) for item in value]
    raise BackendCanonicalError(
        "BACKEND_VALUE_UNSUPPORTED",
        f"A canonical document cannot carry a {type(value).__name__} value.",
    )


class _RawNumber:
    """A number kept in its exact text form so it is written verbatim, never re-formatted."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


def _write(value: Any) -> str:
    """Emit the canonical text, keeping the owner's escape and ordering rules."""

    if isinstance(value, _RawNumber):
        return value.text
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return _write_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_write(item) for item in value) + "]"
    if isinstance(value, dict):
        # The owner orders keys by ordinal (UTF-16 code unit) comparison, which differs from
        # Python's default code-point order only outside the BMP; `.encode("utf-16-be", ...)`
        # reproduces it exactly, so surrogate-pair order is preserved.
        ordered = sorted(
            value.items(), key=lambda entry: entry[0].encode("utf-16-be", "surrogatepass")
        )
        return (
            "{" + ",".join(_write_string(key) + ":" + _write(item) for key, item in ordered) + "}"
        )
    raise BackendCanonicalError(
        "BACKEND_VALUE_UNSUPPORTED", "A canonical document value could not be written."
    )


def _write_string(value: str) -> str:
    """Escape every character outside the safe ASCII set, plus the plus sign.

    The owner writer escapes the plus sign and all non-ASCII code points, so the output
    is pure ASCII with uppercase hex escapes. Quotes and backslashes are the only
    characters kept as two-character escapes.
    """

    parts: list[str] = ['"']
    for character in value:
        code = ord(character)
        if character == '"':
            parts.append('\\"')
        elif character == "\\":
            parts.append("\\\\")
        elif 0x20 <= code <= 0x7E and character != "+":
            parts.append(character)
        elif code <= 0xFFFF:
            parts.append("\\u" + f"{code:04X}")
        else:
            high, low = _surrogate_pair(code)
            parts.append("\\u" + f"{high:04X}" + "\\u" + f"{low:04X}")
    parts.append('"')
    return "".join(parts)


def _surrogate_pair(code: int) -> tuple[int, int]:
    adjusted = code - 0x10000
    return 0xD800 + (adjusted >> 10), 0xDC00 + (adjusted & 0x3FF)
