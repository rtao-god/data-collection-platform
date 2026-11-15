from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class ManualRecordLineage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["collector-manual-record-lineage"] = "collector-manual-record-lineage"
    contract_revision: Literal["manual-record-lineage@1"] = Field(
        default="manual-record-lineage@1",
        alias="contractRevision",
    )
    run_id: UUID = Field(alias="runId")
    manual_record_work_id: UUID = Field(alias="manualRecordWorkId")
    manual_record_artifact_id: UUID = Field(alias="manualRecordArtifactId")
    manual_record_artifact_digest: Digest = Field(alias="manualRecordArtifactDigest")
    record_digest: Digest = Field(alias="recordDigest")
    expected_entity_kind: Annotated[
        str,
        Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$"),
    ] = Field(alias="expectedEntityKind")
    manual_record_content_digest: Digest = Field(alias="manualRecordContentDigest")
    source_artifact_id: UUID = Field(alias="sourceArtifactId")
    source_artifact_digest: Digest = Field(alias="sourceArtifactDigest")


def canonical_manual_record_lineage_bytes(lineage: ManualRecordLineage) -> bytes:
    return json.dumps(
        lineage.model_dump(mode="json", by_alias=True),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def decode_manual_record_lineage(content: bytes) -> ManualRecordLineage:
    lineage = ManualRecordLineage.model_validate_json(content, strict=True)
    if canonical_manual_record_lineage_bytes(lineage) != content:
        raise ValueError("manual record lineage is not canonical JSON")
    return lineage
