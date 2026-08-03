from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from pydantic import ValidationError

from collection_application.ports import RawCampaignBundle
from collection_contracts import ManualSeedRow, SourceBindingsDocument, owner_error

MANUAL_SEED_HEADERS = (
    "expected_entity_kind",
    "display_name",
    "website",
    "osm_id",
    "reference_urls",
    "note",
    "provenance",
)


def load_manual_seed_rows(
    bundle: RawCampaignBundle,
    bindings: SourceBindingsDocument,
    correlation_id: str,
) -> dict[str, tuple[ManualSeedRow, ...]]:
    result: dict[str, tuple[ManualSeedRow, ...]] = {}
    for binding in bindings.items:
        if binding.capability != "manual_import":
            continue
        if binding.seed_provider.kind != "file":
            raise AssertionError("validated manual binding must use a file seed provider")
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
        result[path] = parse_seed_csv(raw, path, correlation_id)
    return result


def parse_seed_csv(raw: bytes, path: str, correlation_id: str) -> tuple[ManualSeedRow, ...]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise owner_error(
            error_type="collection/manual-seed-encoding-invalid",
            owner="ManualSeedImport",
            code="MANUAL_SEED_ENCODING_INVALID",
            message="Manual seed file is not valid UTF-8.",
            context={"path": path, "byteOffset": exc.start},
            required_action="Encode the complete seed file as UTF-8 and import it again.",
            correlation_id=correlation_id,
        ) from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = tuple(reader.fieldnames or ())
    if headers != MANUAL_SEED_HEADERS:
        raise owner_error(
            error_type="collection/manual-seed-header-invalid",
            owner="ManualSeedImport",
            code="MANUAL_SEED_HEADER_INVALID",
            message="Manual seed CSV header does not match the owned contract.",
            context={
                "path": path,
                "expectedHeaders": list(MANUAL_SEED_HEADERS),
                "actualHeaders": list(headers),
            },
            required_action="Use the exact documented header and preserve its column order.",
            correlation_id=correlation_id,
        )

    rows: list[ManualSeedRow] = []
    for row_number, row in enumerate(reader, start=2):
        if None in row:
            _raise_invalid_seed_row(
                path,
                row_number,
                [{"type": "extra_columns", "values": row[None]}],
                correlation_id,
            )
        if any(row.get(header) is None for header in MANUAL_SEED_HEADERS):
            _raise_invalid_seed_row(
                path,
                row_number,
                [{"type": "missing_column_value"}],
                correlation_id,
            )
        try:
            rows.append(
                ManualSeedRow.model_validate(
                    {
                        "row_number": row_number,
                        "expected_entity_kind": row["expected_entity_kind"],
                        "display_name": row["display_name"],
                        "website": _empty_to_none(row["website"]),
                        "osm_id": _empty_to_none(row["osm_id"]),
                        "reference_urls": _split_reference_urls(row["reference_urls"]),
                        "note": _empty_to_none(row["note"]),
                        "provenance": row["provenance"],
                    }
                )
            )
        except ValidationError as exc:
            _raise_invalid_seed_row(
                path,
                row_number,
                exc.errors(include_input=False, include_url=False),
                correlation_id,
            )
    return tuple(rows)


def _raise_invalid_seed_row(
    path: str,
    row_number: int,
    errors: Sequence[Mapping[str, Any]],
    correlation_id: str,
) -> NoReturn:
    raise owner_error(
        error_type="collection/manual-seed-row-invalid",
        owner="ManualSeedImport",
        code="MANUAL_SEED_ROW_INVALID",
        message="Manual seed row does not satisfy its typed contract.",
        context={"path": path, "rowNumber": row_number, "errors": list(errors)},
        required_action="Correct the complete row and validate the seed file again.",
        correlation_id=correlation_id,
    )


def _empty_to_none(value: str) -> str | None:
    stripped = value.strip()
    return stripped if stripped else None


def _split_reference_urls(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    return tuple(part.strip() for part in value.split("|") if part.strip())
