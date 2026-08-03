from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_collection_platform.configuration.compiler import (
    CampaignConfigurationViolation,
    compile_campaign_directory,
)


class CampaignBundleCompilerTests(unittest.TestCase):
    def test_same_owned_sources_compile_to_identical_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_campaign(root)

            first = compile_campaign_directory(root)
            second = compile_campaign_directory(root)

            self.assertEqual(first.bundle_sha256, second.bundle_sha256)
            self.assertEqual(first.to_json_bytes(), second.to_json_bytes())
            self.assertEqual("example-city-studios", first.campaign_id)
            self.assertEqual(4, len(first.document["source_manifest"]))

    def test_atomic_write_emits_exact_canonical_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            root.mkdir()
            self._write_valid_campaign(root)
            output = Path(directory) / "artifacts" / "campaign.bundle.json"

            bundle = compile_campaign_directory(root)
            bundle.write_atomic(output)

            self.assertEqual(bundle.to_json_bytes(), output.read_bytes())
            self.assertFalse(any(output.parent.glob(f".{output.name}.*.tmp")))

    def test_changed_seed_source_changes_bundle_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_campaign(root)
            original = compile_campaign_directory(root)

            seed = self._seed()
            seed["note"] = "second reviewed note"
            self._write_ndjson(root / "seeds.ndjson", [seed])
            changed = compile_campaign_directory(root)

            self.assertNotEqual(original.bundle_sha256, changed.bundle_sha256)

    def test_disabled_source_cannot_enter_executable_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_campaign(root)
            policy = self._source_policy()
            policy["status"] = "disabled"
            self._write_json(root / "sources" / "example-http.json", policy)

            with self.assertRaises(CampaignConfigurationViolation) as captured:
                compile_campaign_directory(root)

            self.assertEqual("campaign.source_not_approved", captured.exception.code)

    def test_path_escape_is_rejected_before_file_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_campaign(root)
            campaign = self._campaign()
            campaign["geography_file"] = "../outside.geojson"
            self._write_json(root / "campaign.json", campaign)

            with self.assertRaises(CampaignConfigurationViolation) as captured:
                compile_campaign_directory(root)

            self.assertEqual("campaign.invalid_relative_path", captured.exception.code)

    def test_open_geography_ring_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_campaign(root)
            geography = self._geography()
            geometry = geography["geometry"]
            self.assertIsInstance(geometry, dict)
            coordinates = geometry["coordinates"]
            self.assertIsInstance(coordinates, list)
            ring = coordinates[0]
            self.assertIsInstance(ring, list)
            ring[-1] = [13.0, 52.1]
            self._write_json(root / "geography.geojson", geography)

            with self.assertRaises(CampaignConfigurationViolation) as captured:
                compile_campaign_directory(root)

            self.assertEqual("geography.open_ring", captured.exception.code)

    def test_seed_without_external_reference_is_not_accepted_as_fact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_valid_campaign(root)
            seed = self._seed()
            seed["website"] = None
            seed["osm_id"] = None
            seed["reference_urls"] = []
            self._write_ndjson(root / "seeds.ndjson", [seed])

            with self.assertRaises(CampaignConfigurationViolation) as captured:
                compile_campaign_directory(root)

            self.assertEqual("seed.missing_reference", captured.exception.code)

    def _write_valid_campaign(self, root: Path) -> None:
        (root / "sources").mkdir(parents=True, exist_ok=True)
        self._write_json(root / "campaign.json", self._campaign())
        self._write_json(root / "geography.geojson", self._geography())
        self._write_json(root / "sources" / "example-http.json", self._source_policy())
        self._write_ndjson(root / "seeds.ndjson", [self._seed()])

    @staticmethod
    def _campaign() -> dict[str, object]:
        return {
            "schema_version": 1,
            "campaign_id": "example-city-studios",
            "display_name": "Example City recording studios",
            "entity_kind": "place",
            "geography_file": "geography.geojson",
            "source_policy_files": ["sources/example-http.json"],
            "seeds_file": "seeds.ndjson",
        }

    @staticmethod
    def _geography() -> dict[str, object]:
        return {
            "type": "Feature",
            "properties": {
                "name": "Synthetic contract polygon",
                "source": "test fixture for geography validation",
                "license": "test-only",
                "observed_at": "2026-01-01",
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [13.0, 52.0],
                        [13.2, 52.0],
                        [13.2, 52.2],
                        [13.0, 52.0],
                    ]
                ],
            },
        }

    @staticmethod
    def _source_policy() -> dict[str, object]:
        return {
            "schema_version": 1,
            "source_id": "example.http",
            "source_type": "http",
            "status": "approved",
            "allowed_hosts": ["example.org"],
            "request_budget": 20,
            "requests_per_minute": 5,
            "max_concurrency": 2,
            "terms_reviewed_at": "2026-01-01",
            "robots_policy": "respect",
        }

    @staticmethod
    def _seed() -> dict[str, object]:
        return {
            "expected_entity_kind": "place",
            "display_name": "Synthetic Studio Fixture",
            "website": "https://example.org/studio",
            "osm_id": None,
            "reference_urls": ["https://example.org/studio/about"],
            "note": "test fixture for evidence-bearing seed contract",
            "provenance": "unit-test fixture",
        }

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_ndjson(path: Path, values: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values
        )
        path.write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
