"""Cross-layer primitives with one production owner per meaning."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ContractViolation(ValueError):
    """Typed fail-fast contract error with stable machine-readable context."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = MappingProxyType(dict(context or {}))

    def __str__(self) -> str:
        if not self.context:
            return f"{self.code}: {self.message}"
        rendered_context = ", ".join(
            f"{key}={value!r}" for key, value in sorted(self.context.items())
        )
        return f"{self.code}: {self.message} ({rendered_context})"


def require_utc(value: datetime, *, field_name: str) -> datetime:
    """Require an aware UTC datetime; never normalize an ambiguous value."""

    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ContractViolation(
            code="contract.datetime_not_utc",
            message="A domain timestamp must be timezone-aware UTC.",
            context={"field": field_name, "value": value.isoformat()},
        )
    return value


def require_non_empty_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ContractViolation(
            code="contract.empty_text",
            message="A required text value must not be empty.",
            context={"field": field_name},
        )
    return normalized


def canonical_json_bytes(value: JsonValue) -> bytes:
    """Serialize JSON deterministically for artifacts and content identities."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def sha256_hex(content: bytes) -> str:
    """The only SHA-256 text-digest implementation used by the platform."""

    return sha256(content).hexdigest()


def require_sha256_hex(value: str, *, field_name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ContractViolation(
            code="contract.invalid_sha256",
            message="A SHA-256 identity must contain exactly 64 lowercase hexadecimal characters.",
            context={"field": field_name, "value": value},
        )
    return normalized
