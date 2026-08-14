from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import UUID

from review_application.errors import ReviewInputInvalid
from review_application.models import ReviewQueueCursor


def encode_cursor(cursor: ReviewQueueCursor | None) -> str | None:
    if cursor is None:
        return None
    payload = json.dumps(
        {
            "caseId": str(cursor.case_id),
            "recordedAtUtc": cursor.recorded_at_utc.isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str | None) -> ReviewQueueCursor | None:
    if value is None:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        if set(payload) != {"caseId", "recordedAtUtc"}:
            raise ValueError("unexpected cursor shape")
        recorded_at_utc = datetime.fromisoformat(payload["recordedAtUtc"])
        if recorded_at_utc.tzinfo is None or recorded_at_utc.utcoffset() is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return ReviewQueueCursor(
            recorded_at_utc=recorded_at_utc,
            case_id=UUID(payload["caseId"]),
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ReviewInputInvalid(
            "The review cursor is malformed.",
            "Restart pagination from the first review page.",
        ) from exc
