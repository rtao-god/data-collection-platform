"""Match-relevant context of one candidate pair.

An operator's MATCH/SEPARATE judgement is only meaningful for the facts the operator actually
saw. Retrieval instants, run identifiers and artifact digests change with every fetch and say
nothing about whether two candidates are the same studio, so they are deliberately absent here:
a re-fetch of unchanged facts must not invalidate a decision, and a changed name, kind, contact
detail, website, address or geography must.
"""

from __future__ import annotations

from decimal import Decimal

from resolution_contracts import ResolutionCandidate, canonical_digest


def candidate_pair_facts(candidate: ResolutionCandidate) -> dict[str, object]:
    """The facts of one candidate that a pair judgement depends on."""

    geography = candidate.geography
    return {
        "candidateId": str(candidate.candidate_id),
        "entityKind": candidate.entity_kind.value,
        "names": list(candidate.names),
        "phones": list(candidate.phones),
        "emails": list(candidate.emails),
        "websiteUrls": list(candidate.website_urls),
        "addresses": list(candidate.addresses),
        "geographyCoverage": geography.coverage.value,
        "marketAreaRevision": geography.market_area_revision,
        "marketAreaDigest": geography.market_area_digest,
        "latitude": _decimal_text(geography.latitude),
        "longitude": _decimal_text(geography.longitude),
    }


def pair_context_digest(left: ResolutionCandidate, right: ResolutionCandidate) -> str:
    """Digest of the exact match-relevant facts of a canonically ordered pair."""

    ordered = sorted((left, right), key=lambda item: item.candidate_id.hex)
    return canonical_digest([candidate_pair_facts(item) for item in ordered])


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
