"""Emit the backend package for one real Wikidata studio record, for the backend validator.

The payload is produced from the pilot's unmodified Wikidata entity document: adapter ->
mapping -> candidate revision -> export evidence -> sealed export-package@2 -> the exact
bytes a registration would upload. The backend owns validation, so this fixture is fed
untouched into `IngestionPackageValidator` by the cross-system test on that side; a
producer defect in the category claim then shows up as a backend rejection.

Run from the repository root:

    uv run python tools/emit_wikidata_cross_system_package.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID

from collection_infrastructure.export.assembly import (
    ExportCandidateSource,
    ExportRunContext,
    assemble_export_package,
)
from collector_cli.backend_canonical import compute_digest
from collector_cli.backend_ingestion import build_manifest, build_package_documents
from resolution_contracts import ClusterQualityAssessment

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "connectors" / "wikidata_studios" / "tests")
)

import test_category_provenance  # type: ignore[import-not-found]

OUTPUT = Path(__file__).resolve().parents[1] / "contracts/golden/wikidata-category-package.json"
_RUN_ID = UUID("019c0000-0000-7000-8000-000000000301")


def main() -> None:
    record = test_category_provenance._adapted("Q667023")
    payload, _ = test_category_provenance._candidate_payload(record)
    source: ExportCandidateSource = test_category_provenance._export_source(
        test_category_provenance._export_row(payload)
    )
    context = ExportRunContext(
        run_id=_RUN_ID,
        campaign_bundle_digest="sha256:" + "a" * 64,
        campaign_key="berlin_recording_services",
        export_profile_key="collector-candidate-export-v1",
        export_profile_revision="collector-candidate-export-v1",
        quality_profile_digest="sha256:" + "b" * 64,
        catalog_binding=test_category_provenance._binding(),
        generated_at_utc=test_category_provenance._RETRIEVED_AT,
        correlation_id="wikidata-category-cross-system",
    )
    package = assemble_export_package(
        context=context,
        candidates=(source,),
        assessments={
            source.cluster_id: ClusterQualityAssessment(
                cluster_id=source.cluster_id, export_eligible=True
            )
        },
    )
    documents = build_package_documents(package)
    manifest = build_manifest(
        package,
        documents,
        producer_identity="data-collection-platform",
        producer_build="wikidata-pilot",
        target_site_key="berlin-recording",
        target_catalog_key="berlin-recording-services",
        target_catalog_configuration_revision_id="019fdeab-5c00-7000-8000-000000000002",
    )
    document = {
        "source": {
            "entityId": "Q667023",
            "entityRevisionId": record.document["entityRevisionId"],
            "statementId": record.classification_evidence["statementId"],
            "retrievedAtUtc": record.document["observedAtUtc"],
            "sourceUrl": record.document["sourceUrl"],
        },
        "manifest": manifest,
        "manifestDigest": compute_digest(manifest),
        "payloadBytes": documents.payload_bytes.decode("utf-8"),
        "itemIndex": list(documents.item_index),
        "expectedItemDigests": {
            item["itemKey"]: documents.item_digest(item["itemKey"]) for item in documents.items
        },
    }
    OUTPUT.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
