from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from collection_contracts import (
    CandidateEvidence as CollectionCandidateEvidence,
)
from collection_contracts import (
    CandidateRevision as CollectionCandidateRevision,
)
from review_contracts import (
    CandidateEvidence,
    CandidateRevision,
    ReviewDecisionCommand,
    SuppressionCommand,
    candidate_snapshot_digest,
    review_decision_command_digest,
    suppression_command_digest,
)

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
LEGACY_CANDIDATE_REVISION_JSON = (
    "{\n"
    '  "candidate_id": "00000000-0000-0000-0000-000000000001",\n'
    '  "revision": 0,\n'
    '  "entity_kind": "place",\n'
    '  "cluster_id": "00000000-0000-0000-0000-000000000002",\n'
    '  "resolution_state": "review",\n'
    '  "snapshot_digest": "'
    'sha256:17061ff827641b85d7c629d696c879537bfb443f31c3ab67e6fcb62452ff19d4",\n'
    '  "source_lineage_digest": "'
    'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",\n'
    '  "normalized_payload": {"displayName": "Studio"},\n'
    '  "evidence": [{"position": 0, "evidence_kind": "source_observation", '
    '"evidence_digest": "'
    'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}],\n'
    '  "recorded_at_utc": "2026-08-14T08:00:00Z",\n'
    '  "correlation_id": "legacy-persisted-candidate"\n'
    "}"
)


def candidate_revision() -> CandidateRevision:
    candidate_id = uuid4()
    cluster_id = uuid4()
    evidence = (
        CandidateEvidence(
            position=0,
            evidence_kind="source_observation",
            evidence_digest=DIGEST_A,
        ),
    )
    digest = candidate_snapshot_digest(
        candidate_id=candidate_id,
        revision=0,
        entity_kind="place",
        cluster_id=cluster_id,
        resolution_state="review",
        normalized_payload={"displayName": "Studio"},
        evidence=evidence,
        source_lineage_digest=DIGEST_B,
    )
    return CandidateRevision(
        candidate_id=candidate_id,
        revision=0,
        entity_kind="place",
        cluster_id=cluster_id,
        resolution_state="review",
        snapshot_digest=digest,
        source_lineage_digest=DIGEST_B,
        normalized_payload={"displayName": "Studio"},
        evidence=evidence,
        recorded_at_utc=NOW,
        correlation_id="candidate-test",
    )


def test_candidate_snapshot_digest_is_wire_enforced() -> None:
    value = candidate_revision()
    with pytest.raises(ValidationError):
        CandidateRevision.model_validate({**value.model_dump(), "snapshot_digest": DIGEST_A})


def test_candidate_contract_reads_legacy_persisted_revision_json() -> None:
    restored = CollectionCandidateRevision.model_validate_json(LEGACY_CANDIDATE_REVISION_JSON)

    assert CandidateRevision is CollectionCandidateRevision
    assert CandidateEvidence is CollectionCandidateEvidence
    assert restored.candidate_id.hex == "00000000000000000000000000000001"
    assert restored.snapshot_digest == (
        "sha256:17061ff827641b85d7c629d696c879537bfb443f31c3ab67e6fcb62452ff19d4"
    )


def test_candidate_evidence_must_be_contiguous() -> None:
    value = candidate_revision()
    evidence = (
        CandidateEvidence(
            position=1,
            evidence_kind="source_observation",
            evidence_digest=DIGEST_A,
        ),
    )
    with pytest.raises(ValidationError):
        CandidateRevision.model_validate({**value.model_dump(), "evidence": evidence})


def test_decision_command_rejects_markup() -> None:
    case_id = uuid4()
    with pytest.raises(ValidationError):
        ReviewDecisionCommand(
            case_id=case_id,
            expected_case_revision=0,
            outcome="accept_candidate",
            actor_id="reviewer",
            rationale="<b>unsafe</b>",
            evidence_references=(DIGEST_A,),
            command_digest=DIGEST_B,
            correlation_id="review-test",
        )


def test_decision_command_digest_is_enforced() -> None:
    case_id = uuid4()
    digest = review_decision_command_digest(
        case_id=case_id,
        expected_case_revision=0,
        outcome="accept_candidate",
        actor_id="reviewer",
        rationale="Verified against source evidence.",
        evidence_references=(DIGEST_A,),
        supersedes_decision_id=None,
    )
    command = ReviewDecisionCommand(
        case_id=case_id,
        expected_case_revision=0,
        outcome="accept_candidate",
        actor_id="reviewer",
        rationale="Verified against source evidence.",
        evidence_references=(DIGEST_A,),
        command_digest=digest,
        correlation_id="review-test",
    )
    assert command.command_digest == digest


def test_suppression_scopes_are_canonical() -> None:
    digest = suppression_command_digest(
        target_kind="candidate",
        target_id="candidate-1",
        scopes=("discovery", "export"),
        reason_code="LEGAL_REVIEW",
        actor_id="reviewer",
        evidence_reference=DIGEST_A,
        expected_revision=None,
        expires_at_utc=None,
    )
    with pytest.raises(ValidationError):
        SuppressionCommand(
            target_kind="candidate",
            target_id="candidate-1",
            scopes=("export", "discovery"),
            reason_code="LEGAL_REVIEW",
            actor_id="reviewer",
            evidence_reference=DIGEST_A,
            expected_revision=None,
            command_digest=digest,
            correlation_id="suppression-test",
        )
