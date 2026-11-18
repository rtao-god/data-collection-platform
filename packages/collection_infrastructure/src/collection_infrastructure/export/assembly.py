"""Assemble the sealed collector export package from verified, export-eligible state.

The assembler is deliberately read-only over its inputs: it never recomputes quality,
never re-interprets match scores, and never repairs missing provenance. Any candidate
that is not provably export-eligible, or whose provenance is incomplete, fails the whole
attempt closed with owner context instead of being silently dropped from the package.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from collection_contracts import (
    ExportCandidateEntry,
    ExportCatalogBinding,
    ExportContact,
    ExportFieldEvidence,
    ExportGeography,
    ExportLocalizedName,
    ExportPackage,
    ExportPackagePayload,
    ExportQualitySummary,
    ExportReviewDecision,
    ExportSourcePolicy,
    ExportTypedAttribute,
    seal_export_candidate,
    seal_export_package,
)
from resolution_contracts import ClusterQualityAssessment


class ExportAssemblyError(Exception):
    """Raised when the requested export cannot be assembled from complete, eligible state."""

    def __init__(
        self,
        *,
        code: str,
        owner: str,
        message: str,
        context: Mapping[str, str],
        required_action: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.owner = owner
        self.message = message
        self.context = dict(context)
        self.required_action = required_action


@dataclass(frozen=True, slots=True)
class ExportCandidateSource:
    """One candidate revision offered to export, with its exact owned evidence.

    The source is already fully typed: names carry their locale and field state, contacts
    and attributes carry their own state, and every declared field path has a matching
    evidence record. The assembler only checks completeness and seals; it never infers a
    value, a locale or a language.
    """

    candidate_id: UUID
    candidate_revision: int
    snapshot_digest: str
    entity_kind: str
    cluster_id: UUID
    localized_names: tuple[ExportLocalizedName, ...]
    contacts: tuple[ExportContact, ...]
    typed_attributes: tuple[ExportTypedAttribute, ...]
    category_keys: tuple[str, ...]
    geography: ExportGeography
    field_evidence: tuple[ExportFieldEvidence, ...]
    quality: ExportQualitySummary
    quality_evaluation_digest: str
    quality_policy_digest: str
    review_decisions: tuple[ExportReviewDecision, ...]
    source_policies: tuple[ExportSourcePolicy, ...]
    source_artifact_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ExportRunContext:
    """Immutable run facts the package is bound to."""

    run_id: UUID
    campaign_bundle_digest: str
    campaign_key: str
    export_profile_key: str
    export_profile_revision: str
    quality_profile_digest: str
    # The declared classification of campaign categories into the Catalog taxonomy, pinned
    # to the exact configuration revision it was authored against. It travels inside the
    # sealed package so a consumer never re-derives a category from a different catalog.
    catalog_binding: ExportCatalogBinding
    generated_at_utc: datetime
    correlation_id: str


class ExportCandidateReader(Protocol):
    """Read-only port: the export assembler owns no candidate state itself."""

    def read_candidates(self, *, run_id: UUID) -> Sequence[ExportCandidateSource]: ...


class ExportQualityReader(Protocol):
    """Read-only port to the singular quality owner."""

    def read_assessments(
        self, *, run_id: UUID, cluster_ids: Sequence[UUID]
    ) -> Mapping[UUID, ClusterQualityAssessment]: ...


ExportableCoverage = Literal["inside", "boundary"]
_EXPORTABLE_COVERAGE: frozenset[str] = frozenset({"inside", "boundary"})

# The only source legal statuses that grant a redistribution right. Every other status -
# `research_only` above all - supports internal review but can never enter a published
# package, and is never downgraded to a weaker-but-different authorization such as
# `link_only`, which remains a distinct grant held by genuine `reference_only` sources.
_REDISTRIBUTABLE_LEGAL_STATUSES: frozenset[str] = frozenset({"approved", "reference_only"})


def _authorizes_redistribution(legal_status: str) -> bool:
    return legal_status in _REDISTRIBUTABLE_LEGAL_STATUSES


def _exportable_coverage(value: str) -> ExportableCoverage:
    if value not in _EXPORTABLE_COVERAGE:
        raise ExportAssemblyError(
            code="EXPORT_GEOGRAPHY_UNRESOLVED",
            owner="GeographyResolution",
            message="The candidate has no proven market-area coverage.",
            context={"geographyCoverage": value},
            required_action="Resolve the candidate coverage before export.",
        )
    return "inside" if value == "inside" else "boundary"


def assemble_export_package(
    *,
    context: ExportRunContext,
    candidates: Sequence[ExportCandidateSource],
    assessments: Mapping[UUID, ClusterQualityAssessment],
) -> ExportPackage:
    """Seal an export package, or fail closed naming the exact blocking owner state."""

    if not candidates:
        raise ExportAssemblyError(
            code="EXPORT_PACKAGE_WOULD_BE_EMPTY",
            owner="ExportAssembly",
            message="The run produced no export-eligible candidate revision.",
            context={
                "runId": str(context.run_id),
                "correlationId": context.correlation_id,
            },
            required_action=(
                "Complete acquisition, normalization, and review for this run before "
                "requesting an export; an empty package is not a valid catalog release."
            ),
        )

    entries: list[ExportCandidateEntry] = []
    seen_candidates: dict[UUID, int] = {}
    for candidate in candidates:
        coverage = _require_eligible(candidate=candidate, assessments=assessments)
        previous = seen_candidates.get(candidate.candidate_id)
        if previous is not None:
            raise ExportAssemblyError(
                code="EXPORT_CANDIDATE_MULTIPLE_REVISIONS",
                owner="ExportAssembly",
                message="The run exposes more than one revision of the same candidate.",
                context={
                    "runId": str(context.run_id),
                    "candidateId": str(candidate.candidate_id),
                    "revisions": f"{previous},{candidate.candidate_revision}",
                    "correlationId": context.correlation_id,
                },
                required_action=(
                    "Export exactly one decided revision per candidate; resolve the open "
                    "review state that keeps two revisions current."
                ),
            )
        seen_candidates[candidate.candidate_id] = candidate.candidate_revision
        _require_classified(candidate=candidate, binding=context.catalog_binding)
        _require_display_locale(candidate=candidate, binding=context.catalog_binding)
        entries.append(_seal_entry(candidate=candidate, coverage=coverage))

    entries.sort(key=lambda item: item.candidate_id)
    return seal_export_package(
        ExportPackagePayload(
            run_id=context.run_id,
            campaign_bundle_digest=context.campaign_bundle_digest,
            campaign_key=context.campaign_key,
            export_profile_key=context.export_profile_key,
            export_profile_revision=context.export_profile_revision,
            quality_profile_digest=context.quality_profile_digest,
            catalog_binding=context.catalog_binding,
            entries=tuple(entries),
            generated_at_utc=context.generated_at_utc,
            correlation_id=context.correlation_id,
        )
    )


def _require_eligible(
    *, candidate: ExportCandidateSource, assessments: Mapping[UUID, ClusterQualityAssessment]
) -> ExportableCoverage:
    context_base = {
        "candidateId": str(candidate.candidate_id),
        "candidateRevision": str(candidate.candidate_revision),
        "clusterId": str(candidate.cluster_id),
    }

    assessment = assessments.get(candidate.cluster_id)
    if assessment is None:
        raise ExportAssemblyError(
            code="EXPORT_QUALITY_ASSESSMENT_MISSING",
            owner="QualityCore",
            message="The candidate cluster has no persisted quality assessment.",
            context=context_base,
            required_action=(
                "Evaluate the cluster quality through the quality owner before export; "
                "absent quality state is not eligible by default."
            ),
        )
    if not assessment.export_eligible:
        raise ExportAssemblyError(
            code="EXPORT_QUALITY_NOT_ELIGIBLE",
            owner="QualityCore",
            message="The candidate cluster is not export-eligible.",
            context={
                **context_base,
                "issueCodes": ",".join(sorted(issue.code for issue in assessment.issues)),
            },
            required_action=(
                "Resolve the blocking quality issues recorded by the quality owner; export "
                "must consume exportEligible and must not reinterpret raw match scores."
            ),
        )
    unauthorized = sorted(
        policy.source_key
        for policy in candidate.source_policies
        if not _authorizes_redistribution(policy.legal_status)
    )
    if unauthorized:
        raise ExportAssemblyError(
            code="EXPORT_CANDIDATE_SOURCE_POLICY_NOT_EXPORTABLE",
            owner="CandidateMaterialization",
            message="The sealed source legal status does not authorize redistribution.",
            context={**context_base, "restrictedSourceKeys": ",".join(unauthorized)},
            required_action=(
                "Record an external redistribution decision for these sources and "
                "re-materialize the revision; the candidate stays non-exportable until then."
            ),
        )
    coverage = _exportable_coverage(candidate.geography.coverage)
    if not any(item.state == "observed" for item in candidate.localized_names):
        raise ExportAssemblyError(
            code="EXPORT_CANDIDATE_NAME_MISSING",
            owner="CandidateMaterialization",
            message="The candidate revision carries no observed display name.",
            context=context_base,
            required_action="Re-normalize the candidate so it exposes an observed name field.",
        )
    if not candidate.category_keys:
        raise ExportAssemblyError(
            code="EXPORT_CANDIDATE_CATEGORY_MISSING",
            owner="CatalogMapping",
            message="The candidate revision carries no proven catalog category.",
            context=context_base,
            required_action=(
                "Map the candidate to an approved campaign taxonomy key; an unmapped "
                "candidate stays blocked instead of being classified by guess."
            ),
        )
    if not candidate.source_policies or not candidate.source_artifact_ids:
        raise ExportAssemblyError(
            code="EXPORT_PROVENANCE_MISSING",
            owner="CandidateMaterialization",
            message="The candidate revision carries no source provenance.",
            context=context_base,
            required_action=(
                "Restore the evidence binding for this revision; an unattributed catalog "
                "entry cannot be published."
            ),
        )
    if not candidate.field_evidence:
        raise ExportAssemblyError(
            code="EXPORT_FIELD_EVIDENCE_MISSING",
            owner="CandidateMaterialization",
            message="The candidate revision carries no field-level evidence.",
            context=context_base,
            required_action=(
                "Seal per-field evidence for this revision; a consumer cannot attribute a "
                "value whose origin is not proven."
            ),
        )
    return coverage


def _require_classified(*, candidate: ExportCandidateSource, binding: ExportCatalogBinding) -> None:
    """Every observed campaign category must have a declared Catalog target in this pin.

    An unclassified category is not a missing value to be dropped: the entry would then
    reach the consumer with fewer claims than its own evidence supports. It stays blocked
    here, at the owner that declares the mapping.
    """

    targets = binding.targets
    unclassified = sorted(key for key in candidate.category_keys if key not in targets)
    if unclassified:
        raise ExportAssemblyError(
            code="EXPORT_CANDIDATE_CATEGORY_UNCLASSIFIED",
            owner="CatalogMapping",
            message="An observed candidate category has no declared Catalog target.",
            context={
                "candidateId": str(candidate.candidate_id),
                "candidateRevision": str(candidate.candidate_revision),
                "catalogConfigurationRevisionId": str(binding.configuration_revision_id),
                "campaignCategories": ",".join(unclassified),
            },
            required_action=(
                "Declare the campaign category to Catalog category correspondence in the "
                "campaign catalog mapping for the pinned configuration revision; an "
                "unclassified category stays blocked instead of being guessed."
            ),
        )


def _require_display_locale(
    *, candidate: ExportCandidateSource, binding: ExportCatalogBinding
) -> None:
    """Every observed name must carry a display locale this pin declares.

    A name whose language no source and no operator established stays `und`, and `und` is
    not a Catalog display locale. Such a candidate stays blocked here, at the owner that
    declares the mapping, instead of being exported under a locale the evidence never
    supported.
    """

    targets = {mapping.campaign_locale for mapping in binding.name_locales}
    unresolved = sorted(
        {
            name.locale
            for name in candidate.localized_names
            if name.state == "observed" and name.locale not in targets
        }
    )
    if unresolved:
        raise ExportAssemblyError(
            code="EXPORT_CANDIDATE_NAME_LOCALE_UNRESOLVED",
            owner="CatalogMapping",
            message="An observed candidate name has no declared Catalog display locale.",
            context={
                "candidateId": str(candidate.candidate_id),
                "candidateRevision": str(candidate.candidate_revision),
                "catalogConfigurationRevisionId": str(binding.configuration_revision_id),
                "campaignLocales": ",".join(unresolved),
            },
            required_action=(
                "Record the language the name was published in at its source, or declare the "
                "operator-entered language; a name whose language nothing established stays "
                "blocked instead of being displayed under a guessed locale."
            ),
        )


def _seal_entry(
    *, candidate: ExportCandidateSource, coverage: ExportableCoverage
) -> ExportCandidateEntry:
    """Seal one entry, leaving the deterministic content identity to the contract.

    The geography coverage is re-declared from the verified decision, so a source object
    cannot present a coverage the assembler did not verify.
    """

    geography = candidate.geography.model_copy(update={"coverage": coverage})
    # The owner seals the digest over the normalized wire document, so the assembler hands
    # over the exact fields once and never recomputes the entry identity itself.
    return seal_export_candidate(
        {
            "candidateId": str(candidate.candidate_id),
            "candidateRevision": candidate.candidate_revision,
            "snapshotDigest": candidate.snapshot_digest,
            "entityKind": candidate.entity_kind,
            "clusterId": str(candidate.cluster_id),
            "localizedNames": [
                item.model_dump(mode="json", by_alias=True) for item in candidate.localized_names
            ],
            "contacts": [
                item.model_dump(mode="json", by_alias=True) for item in candidate.contacts
            ],
            "typedAttributes": [
                item.model_dump(mode="json", by_alias=True) for item in candidate.typed_attributes
            ],
            "categoryKeys": list(candidate.category_keys),
            "geography": geography.model_dump(mode="json", by_alias=True),
            "fieldEvidence": [
                item.model_dump(mode="json", by_alias=True) for item in candidate.field_evidence
            ],
            "quality": candidate.quality.model_dump(mode="json", by_alias=True),
            "qualityEvaluationDigest": candidate.quality_evaluation_digest,
            "qualityPolicyDigest": candidate.quality_policy_digest,
            "reviewDecisions": [
                item.model_dump(mode="json", by_alias=True) for item in candidate.review_decisions
            ],
            "sourcePolicies": [
                item.model_dump(mode="json", by_alias=True) for item in candidate.source_policies
            ],
            "sourceArtifactIds": [str(item) for item in candidate.source_artifact_ids],
        }
    )
