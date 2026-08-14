from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from source_connector_sdk import WorkFailureKind


class OfficialHttpError(ValueError):
    """Typed official-HTTP contract failure with owner context."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        kind: WorkFailureKind = "contract_invalid",
        context: Mapping[str, object] | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.kind = kind
        self.context = dict(context or {})
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


HttpOutcome = Literal["fetched", "unchanged", "redirect", "not_found"]
