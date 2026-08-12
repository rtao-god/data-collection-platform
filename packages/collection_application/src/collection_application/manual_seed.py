from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from collection_application.ports import RawCampaignBundle
from collection_contracts import (
    ManualImportDisposition,
    ManualImportFormat,
    ManualImportMode,
    ManualImportPlan,
    ManualSeedRow,
    SourceBindingsDocument,
    SourcePolicy,
    owner_error,
)
from manual_import_core import build_manual_import_plan

ManualSeedFormat = Literal["csv", "json", "jsonl"]


def load_manual_seed_rows(
    bundle: RawCampaignBundle,
    bindings: SourceBindingsDocument,
    policies: Mapping[str, SourcePolicy],
    correlation_id: str,
) -> dict[str, tuple[ManualSeedRow, ...]]:
    result: dict[str, tuple[ManualSeedRow, ...]] = {}
    for binding in bindings.items:
        if binding.capability != "manual_import":
            continue
        if binding.seed_provider.kind != "file":
            raise owner_error(
                error_type="collection/manual-seed-binding-invalid",
                owner="ManualSeedImport",
                code="MANUAL_SEED_BINDING_INVALID",
                message="Manual source binding has no file seed provider.",
                context={"bindingKey": binding.key},
                required_action="Correct the typed source binding before reading campaign seeds.",
                correlation_id=correlation_id,
            )
        policy = policies.get(binding.source_policy_key)
        if policy is None:
            raise owner_error(
                error_type="collection/manual-seed-policy-missing",
                owner="ManualSeedImport",
                code="MANUAL_SEED_POLICY_MISSING",
                message="Manual source binding references a missing source policy.",
                context={
                    "bindingKey": binding.key,
                    "policyKey": binding.source_policy_key,
                },
                required_action="Add the exact manual source policy before reading campaign seeds.",
                correlation_id=correlation_id,
            )
        access = policy.access
        if access.kind != "manual":
            raise owner_error(
                error_type="collection/manual-seed-policy-invalid",
                owner="ManualSeedImport",
                code="MANUAL_SEED_POLICY_INVALID",
                message="Manual source binding does not use a manual access policy.",
                context={
                    "bindingKey": binding.key,
                    "policyKey": policy.policy_key,
                    "accessKind": access.kind,
                },
                required_action="Publish a manual access policy for the manual source binding.",
                correlation_id=correlation_id,
            )
        declared_format: ManualSeedFormat = binding.seed_provider.format
        if declared_format not in access.accepted_formats:
            raise owner_error(
                error_type="collection/manual-seed-format-forbidden",
                owner="ManualSeedImport",
                code="MANUAL_SEED_FORMAT_FORBIDDEN",
                message="Manual seed format is not allowed by the source policy.",
                context={
                    "bindingKey": binding.key,
                    "policyKey": policy.policy_key,
                    "format": declared_format,
                    "acceptedFormats": list(access.accepted_formats),
                },
                required_action=(
                    "Use a policy-approved format or publish a reviewed policy revision."
                ),
                correlation_id=correlation_id,
            )

        path = binding.seed_provider.path
        raw = bundle.files.get(path)
        if raw is None:
            raise owner_error(
                error_type="collection/manual-seed-missing",
                owner="ManualSeedImport",
                code="MANUAL_SEED_MISSING",
                message="Manual source binding references a missing seed file.",
                context={"bindingKey": binding.key, "path": path},
                required_action="Add the referenced seed file inside the campaign bundle.",
                correlation_id=correlation_id,
            )
        plan = read_manual_seed_records(
            raw,
            path=path,
            format=declared_format,
            max_file_bytes=access.max_file_bytes,
            partial_mode=False,
            partial_mode_allowed=access.partial_mode_allowed,
            require_records=False,
            correlation_id=correlation_id,
        )
        result[path] = tuple(record.record for record in plan.records)
    return result


def read_manual_seed_records(
    raw: bytes,
    *,
    path: str,
    format: ManualSeedFormat,
    max_file_bytes: int,
    partial_mode: bool,
    partial_mode_allowed: bool,
    require_records: bool,
    correlation_id: str,
) -> ManualImportPlan:
    if partial_mode and not partial_mode_allowed:
        raise owner_error(
            error_type="collection/manual-seed-partial-mode-forbidden",
            owner="ManualSeedImport",
            code="MANUAL_SEED_PARTIAL_MODE_FORBIDDEN",
            message="Partial manual import is not allowed by the source policy.",
            context={"path": path},
            required_action=(
                "Correct the complete file or publish a policy that explicitly allows partial mode."
            ),
            correlation_id=correlation_id,
        )

    plan = build_manual_import_plan(
        raw,
        format=ManualImportFormat(format),
        mode=ManualImportMode.PARTIAL if partial_mode else ManualImportMode.ATOMIC,
        max_file_bytes=max_file_bytes,
        require_records=require_records,
    )
    if plan.disposition is ManualImportDisposition.REJECTED:
        _raise_rejected_plan(plan, path, correlation_id)
    return plan


def parse_seed_csv(raw: bytes, path: str, correlation_id: str) -> tuple[ManualSeedRow, ...]:
    plan = read_manual_seed_records(
        raw,
        path=path,
        format="csv",
        max_file_bytes=max(1, len(raw)),
        partial_mode=False,
        partial_mode_allowed=False,
        require_records=False,
        correlation_id=correlation_id,
    )
    return tuple(record.record for record in plan.records)


def _raise_rejected_plan(
    plan: ManualImportPlan,
    path: str,
    correlation_id: str,
) -> None:
    primary = plan.issues[0]
    code, error_type, message, required_action = _owner_error_contract(primary.code)
    raise owner_error(
        error_type=error_type,
        owner="ManualSeedImport",
        code=code,
        message=message,
        context={
            "path": path,
            "format": plan.format.value,
            "recordCount": plan.valid_record_count + plan.issue_count,
            "validRecordCount": plan.valid_record_count,
            "invalidRecordCount": plan.issue_count,
            "planDigest": plan.plan_digest,
            "issues": [issue.model_dump(mode="json", by_alias=True) for issue in plan.issues],
        },
        required_action=required_action,
        correlation_id=correlation_id,
    )


def _owner_error_contract(issue_code: str) -> tuple[str, str, str, str]:
    if issue_code == "MANUAL_IMPORT_FILE_EMPTY":
        return (
            "MANUAL_SEED_EMPTY",
            "collection/manual-seed-empty",
            "Manual seed file contains no records.",
            "Provide at least one complete manual seed record.",
        )
    if issue_code == "MANUAL_IMPORT_FILE_TOO_LARGE":
        return (
            "MANUAL_SEED_SIZE_INVALID",
            "collection/manual-seed-size-invalid",
            "Manual seed file size is outside the source-policy limit.",
            "Provide a non-empty file within the reviewed source-policy size limit.",
        )
    if issue_code == "MANUAL_IMPORT_UTF8_INVALID":
        return (
            "MANUAL_SEED_ENCODING_INVALID",
            "collection/manual-seed-encoding-invalid",
            "Manual seed file is not valid UTF-8.",
            "Encode the complete seed file as UTF-8 and import it again.",
        )
    if issue_code in {"MANUAL_IMPORT_CSV_HEADER_INVALID", "MANUAL_IMPORT_CSV_HEADER_MISMATCH"}:
        return (
            "MANUAL_SEED_HEADER_INVALID",
            "collection/manual-seed-header-invalid",
            "Manual seed CSV header does not match the owned contract.",
            "Use the exact documented header and preserve its column order.",
        )
    if issue_code == "MANUAL_IMPORT_CSV_MALFORMED":
        return (
            "MANUAL_SEED_CSV_INVALID",
            "collection/manual-seed-csv-invalid",
            "Manual seed CSV cannot be parsed safely.",
            "Correct the CSV quoting and row structure before importing it again.",
        )
    if issue_code == "MANUAL_IMPORT_JSON_MALFORMED":
        return (
            "MANUAL_SEED_JSON_INVALID",
            "collection/manual-seed-json-invalid",
            "Manual seed JSON cannot be parsed safely.",
            "Correct the JSON syntax, duplicate keys, or non-finite numbers.",
        )
    if issue_code == "MANUAL_IMPORT_RECORD_LIMIT_EXCEEDED":
        return (
            "MANUAL_SEED_RECORD_LIMIT_EXCEEDED",
            "collection/manual-seed-record-limit-exceeded",
            "Manual seed file contains more records than the platform limit.",
            "Split the source into independently reviewable import files.",
        )
    return (
        "MANUAL_SEED_FILE_INVALID",
        "collection/manual-seed-file-invalid",
        "Manual seed file contains invalid records and cannot be partially accepted.",
        "Correct every reported record or explicitly use policy-approved partial mode.",
    )
