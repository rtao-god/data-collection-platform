from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class ErrorEnvelope(BaseModel):
    """Transport-safe owner-context failure contract."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
    )

    type: str = Field(pattern=r"^collection/[a-z0-9-]+$")
    owner: str = Field(min_length=1, max_length=100)
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    message: str = Field(min_length=1, max_length=500)
    context: dict[str, Any]
    required_action: str = Field(
        alias="requiredAction",
        serialization_alias="requiredAction",
        min_length=1,
        max_length=500,
    )
    correlation_id: str = Field(
        alias="correlationId",
        serialization_alias="correlationId",
        min_length=1,
        max_length=100,
    )


class OwnerContextError(Exception):
    """Expected application failure that preserves its canonical error envelope."""

    def __init__(self, envelope: ErrorEnvelope) -> None:
        self.envelope = envelope
        super().__init__(envelope.message)


def owner_error(
    *,
    error_type: str,
    owner: str,
    code: str,
    message: str,
    context: Mapping[str, Any],
    required_action: str,
    correlation_id: str,
) -> OwnerContextError:
    return OwnerContextError(
        ErrorEnvelope(
            type=error_type,
            owner=owner,
            code=code,
            message=message,
            context=dict(context),
            required_action=required_action,
            correlation_id=correlation_id,
        )
    )
