"""The operator's exact candidate-revision selection syntax.

Both the export and the Ingestion submission command take the same explicit
`CANDIDATE_ID:REVISION` selection, so the parsing (and its refusal messages) has one owner
instead of a copy per command.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID


class SelectionError(ValueError):
    """Raised when an operator selection cannot be expressed as exact candidate revisions."""


def parse_candidate_revisions(values: Sequence[str]) -> tuple[tuple[UUID, int], ...]:
    if not values:
        raise SelectionError(
            "select at least one --candidate-revision so the run cannot silently carry "
            "candidate state the operator did not choose"
        )
    revisions: list[tuple[UUID, int]] = []
    for value in values:
        candidate_text, separator, revision_text = value.partition(":")
        if not separator:
            raise SelectionError(f"candidate revision '{value}' must be CANDIDATE_ID:REVISION")
        try:
            candidate_id = UUID(candidate_text)
        except ValueError as exc:
            raise SelectionError(
                f"candidate revision '{value}' has an invalid candidate id"
            ) from exc
        try:
            revision = int(revision_text)
        except ValueError as exc:
            raise SelectionError(
                f"candidate revision '{value}' has an invalid revision number"
            ) from exc
        if revision < 0:
            raise SelectionError(f"candidate revision '{value}' has a negative revision number")
        revisions.append((candidate_id, revision))
    return tuple(revisions)


__all__ = ["SelectionError", "parse_candidate_revisions"]
