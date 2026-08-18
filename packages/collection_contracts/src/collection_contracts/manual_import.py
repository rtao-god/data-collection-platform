from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from collection_contracts.campaign_config import ManualSeedRow

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_SOURCE_ARTIFACT_ROLE_PATTERN = (
    r"^(?:manual_source|manual_import_source):"
    r"(?:csv|json|jsonl):(?:atomic|partial)$"
)
_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024 * 1024
_MAX_MANUAL_IMPORT_RECORDS = 100_000


type ManualImportContextValue = str | int | bool | tuple[str, ...] | None


class ManualImportFormat(StrEnum):
    CSV = "csv"
    JSON = "json"
    JSONL = "jsonl"


class ManualImportMode(StrEnum):
    ATOMIC = "atomic"
    PARTIAL = "partial"


class ManualImportDisposition(StrEnum):
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    REJECTED = "rejected"


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


class ManualImportLocator(_ContractModel):
    kind: Literal["csv_row", "json_index", "jsonl_line"]
    index: int = Field(ge=1)
    pointer: str = Field(min_length=1, max_length=200)


class ManualImportIssue(_ContractModel):
    locator: ManualImportLocator | None = None
    code: str
    message: str = Field(min_length=1, max_length=500)
    context: dict[str, ManualImportContextValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_code(self) -> Self:
        if _CODE_PATTERN.fullmatch(self.code) is None:
            raise ValueError("manual import issue code has an invalid format")
        return self


class ManualImportRecord(_ContractModel):
    locator: ManualImportLocator
    record: ManualSeedRow
    record_digest: str = Field(alias="recordDigest")

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        _require_digest("record_digest", self.record_digest)
        if self.record.row_number != self.locator.index:
            raise ValueError("manual import record row number must match its locator")
        return self


class ManualImportPlan(_ContractModel):
    contract: Literal["collector-manual-import-plan"] = "collector-manual-import-plan"
    contract_revision: Literal["manual-import-plan-v1"] = Field(
        default="manual-import-plan-v1",
        alias="contractRevision",
    )
    source_digest: str = Field(alias="sourceDigest")
    source_size_bytes: int = Field(alias="sourceSizeBytes", ge=0, le=_MAX_ARTIFACT_BYTES)
    format: ManualImportFormat
    mode: ManualImportMode
    disposition: ManualImportDisposition
    valid_record_count: int = Field(alias="validRecordCount", ge=0, le=_MAX_MANUAL_IMPORT_RECORDS)
    issue_count: int = Field(alias="issueCount", ge=0, le=_MAX_MANUAL_IMPORT_RECORDS)
    records: tuple[ManualImportRecord, ...] = ()
    issues: tuple[ManualImportIssue, ...] = ()
    plan_digest: str = Field(alias="planDigest")

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        _require_digest("source_digest", self.source_digest)
        _require_digest("plan_digest", self.plan_digest)
        if self.valid_record_count != len(self.records):
            raise ValueError("valid_record_count must equal the record collection size")
        if self.issue_count != len(self.issues):
            raise ValueError("issue_count must equal the issue collection size")

        expected_disposition = ManualImportDisposition.ACCEPTED
        if self.issues:
            expected_disposition = (
                ManualImportDisposition.PARTIAL
                if self.mode is ManualImportMode.PARTIAL and self.records
                else ManualImportDisposition.REJECTED
            )
        if self.disposition is not expected_disposition:
            raise ValueError(
                "manual import disposition does not match mode, records, and issue ledger"
            )
        return self


class ManualImportRecordDocument(_ContractModel):
    contract: Literal["collector-manual-import-record"] = "collector-manual-import-record"
    contract_revision: Literal["manual-import-record-v1"] = Field(
        default="manual-import-record-v1",
        alias="contractRevision",
    )
    source_digest: str = Field(alias="sourceDigest")
    source_artifact_role: str = Field(
        alias="sourceArtifactRole",
        pattern=_SOURCE_ARTIFACT_ROLE_PATTERN,
    )
    plan_digest: str = Field(alias="planDigest")
    plan_artifact_digest: str = Field(alias="planArtifactDigest")
    plan_record_position: int = Field(
        alias="planRecordPosition",
        ge=0,
        le=_MAX_MANUAL_IMPORT_RECORDS - 1,
    )
    locator: ManualImportLocator
    record_digest: str = Field(alias="recordDigest")
    record: ManualSeedRow
    materializer_revision: Literal["manual-import-record-materializer-v1"] = Field(
        default="manual-import-record-materializer-v1",
        alias="materializerRevision",
    )
    content_digest: str = Field(alias="contentDigest")

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        _require_digest("source_digest", self.source_digest)
        _require_digest("plan_digest", self.plan_digest)
        _require_digest("plan_artifact_digest", self.plan_artifact_digest)
        _require_digest("record_digest", self.record_digest)
        _require_digest("content_digest", self.content_digest)
        if self.record.row_number != self.locator.index:
            raise ValueError("manual import record document row number must match its locator")
        return self


def _require_digest(name: str, value: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be canonical SHA-256")
