"""Deterministic candidate-review identity shared by every review producer.

Review case identity is derived once, here, because both the review module and the
candidate-intake producer persist cases and must agree byte-for-byte; two private
namespaces silently produced duplicate cases for the same candidate revision.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from collection_contracts.canonical import canonical_digest

_REVIEW_CASE_NAMESPACE = UUID("01cf920c-9a4f-4fd3-9cb0-39e4475d24f5")


def review_case_id(
    candidate_id: UUID,
    candidate_revision: int,
    reason_codes: tuple[str, ...],
) -> UUID:
    """Return the review case identity for one candidate revision and reason set."""
    canonical = tuple(sorted(set(reason_codes)))
    if not canonical:
        raise ValueError("a review case requires at least one reason")
    return uuid5(
        _REVIEW_CASE_NAMESPACE,
        f"{candidate_id}:{candidate_revision}:{canonical_digest(canonical)}",
    )
