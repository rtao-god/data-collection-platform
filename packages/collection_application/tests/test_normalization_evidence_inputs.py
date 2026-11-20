from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import pytest

from collection_application.pipeline_advancement import (
    ArtifactIdentity,
    SucceededWorkOutput,
    _NormalizationPlanningError,
    _read_normalization_evidence,
)
from collection_contracts import (
    ExtractedRecordPayload,
    ExtractionRequest,
    canonical_extracted_record_json,
    seal_extracted_record,
)
from collection_domain import WorkStage

_NOW = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
_CAMPAIGN_BUNDLE = b"campaign bundle bytes"


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _source(*extra_roles: str) -> SucceededWorkOutput:
    request = ExtractionRequest.model_validate(
        {
            "sourceRecordId": "trixx-studios-seed",
            "rawArtifactDigest": f"sha256:{'1' * 64}",
            "sourceUrl": "https://www.trixx-studios.de/",
            "contentType": "text/html",
            "sourcePolicyDigest": f"sha256:{'2' * 64}",
            "extractorRevision": "official-website-extractor-v1",
            "observedAtUtc": _NOW.isoformat(),
            "allowedFields": ["display_name", "address"],
            "prohibitedFields": ["review_text"],
            "maximumEvidenceChars": 300,
            "maximumDocumentBytes": 1024,
        }
    )
    request_content = _canonical(request.model_dump(mode="json", by_alias=True))
    record = seal_extracted_record(
        ExtractedRecordPayload.model_validate(
            {
                "sourceRecordId": request.source_record_id,
                "rawArtifactDigest": request.raw_artifact_digest,
                "sourceUrl": request.source_url,
                "contentType": request.content_type,
                "sourcePolicyDigest": request.source_policy_digest,
                "extractorRevision": request.extractor_revision,
                "observedAtUtc": _NOW.isoformat(),
            }
        )
    )
    record_content = canonical_extracted_record_json(record).encode("utf-8")
    inputs = (
        ArtifactIdentity(
            artifact_id=UUID("00000000-0000-0000-0000-000000000001"),
            role="raw_source_document",
            content_digest=request.raw_artifact_digest,
            size_bytes=1024,
            content_type="text/html",
        ),
        ArtifactIdentity(
            artifact_id=UUID("00000000-0000-0000-0000-000000000002"),
            role="extraction_request",
            content_digest=_digest(request_content),
            size_bytes=len(request_content),
            content_type="application/vnd.collection.extraction-request+json",
        ),
        *(
            ArtifactIdentity(
                artifact_id=UUID(f"00000000-0000-0000-0000-00000000001{index}"),
                role=role,
                content_digest=f"sha256:{'3' * 64}",
                size_bytes=256,
                content_type="application/vnd.collection.manual-record-lineage+json",
            )
            for index, role in enumerate(extra_roles)
        ),
    )
    return SucceededWorkOutput(
        source_work_unit_id=UUID("00000000-0000-0000-0000-000000000010"),
        run_id=UUID("00000000-0000-0000-0000-000000000011"),
        stage_run_id=UUID("00000000-0000-0000-0000-000000000012"),
        stage=WorkStage.EXTRACTION,
        capability="extraction",
        output_contract="extracted-record@1",
        output_digest=_digest(record_content),
        output_artifact=ArtifactIdentity(
            artifact_id=UUID("00000000-0000-0000-0000-000000000013"),
            role="extracted_record",
            content_digest=_digest(record_content),
            size_bytes=len(record_content),
            content_type="application/vnd.collection.extracted-record+json",
        ),
        input_artifacts=inputs,
        source_binding_key="official_site_berlin_studios",
        config_bundle_digest=_digest(_CAMPAIGN_BUNDLE),
        output_content=record_content,
        extraction_request_content=request_content,
        campaign_bundle_content=_CAMPAIGN_BUNDLE,
    )


def test_extraction_evidence_with_manual_lineage_is_accepted() -> None:
    record, request, raw = _read_normalization_evidence(_source("manual_record_lineage"))

    assert record.source_url == request.source_url
    assert raw.role == "raw_source_document"


def test_extraction_evidence_without_manual_lineage_stays_accepted() -> None:
    _read_normalization_evidence(_source())


def test_extraction_evidence_refuses_an_unbound_extra_input() -> None:
    with pytest.raises(_NormalizationPlanningError) as caught:
        _read_normalization_evidence(_source("robots_decision"))

    assert caught.value.code == "PIPELINE_NORMALIZATION_INPUT_BINDING_INVALID"


def test_extraction_evidence_refuses_two_lineage_inputs() -> None:
    with pytest.raises(ValueError, match="duplicate roles"):
        _source("manual_record_lineage", "manual_record_lineage")


def test_extraction_evidence_refuses_a_substituted_request_payload() -> None:
    source = _source("manual_record_lineage")
    other_request = ExtractionRequest.model_validate(
        {
            **_json(source.extraction_request_content),
            "sourceUrl": "https://example.invalid/",
        }
    )
    tampered = replace(
        source,
        extraction_request_content=_canonical(
            other_request.model_dump(mode="json", by_alias=True)
        ),
    )

    with pytest.raises(_NormalizationPlanningError) as caught:
        _read_normalization_evidence(tampered)

    assert caught.value.code == "PIPELINE_NORMALIZATION_INPUT_BINDING_INVALID"


def _json(content: bytes | None) -> dict[str, object]:
    assert content is not None
    return json.loads(content)
