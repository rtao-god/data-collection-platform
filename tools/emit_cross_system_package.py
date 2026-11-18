"""Emit the exact bytes a registration would upload, for the backend's validator to consume.

The backend owns validation, so the cross-system proof lives there: this script produces
`payload` bytes, the item index and the manifest with every digest computed by the adapter,
and writes them to `contracts/golden/cross-system-package.json`. The backend test feeds those
untouched bytes into `IngestionPackageValidator.ValidatePayload`, so a producer defect shows
up as a backend rejection rather than as a producer-only assertion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from collector_cli.backend_canonical import compute_digest
from collector_cli.backend_ingestion import build_manifest, build_package_documents

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "collector_cli" / "tests"))

import test_backend_ingestion  # type: ignore[import-not-found]

OUTPUT = Path(__file__).resolve().parents[1] / "contracts/golden/cross-system-package.json"


def main() -> None:
    export = test_backend_ingestion._sealed_export()
    documents = build_package_documents(export)
    manifest = build_manifest(
        export,
        documents,
        producer_identity="data-collection-platform",
        producer_build="cross-system-vector",
        target_site_key="berlin-recording",
        target_catalog_key="berlin-recording-services",
        target_catalog_configuration_revision_id="00000000-0000-0000-0000-00000000d002",
    )
    document = {
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
