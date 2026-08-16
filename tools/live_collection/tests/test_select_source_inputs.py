from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tools.live_collection.select_source_inputs import (
    SourceInputSelectionError,
    _binding_keys,
    _disable_schedules,
    _enable_bindings,
    _validate_budgets,
    _validate_urls,
)


def test_binding_identity_discovery_keeps_osm_and_official_http() -> None:
    value = {
        "bindings": [
            {"source_binding_key": "berlin_osm_overpass"},
            {"source_binding_key": "berlin_official_http"},
            {"source_binding_key": "manual_seed_import"},
        ]
    }

    assert _binding_keys(value) == (
        "berlin_official_http",
        "berlin_osm_overpass",
    )


def test_automatic_schedules_are_disabled_recursively() -> None:
    value = {
        "schedule_enabled": True,
        "nested": [{"auto_start": True}, {"automatic": True}],
    }

    assert _disable_schedules(value) == {
        "schedule_enabled": False,
        "nested": [{"auto_start": False}, {"automatic": False}],
    }


def test_non_https_source_is_rejected() -> None:
    with pytest.raises(SourceInputSelectionError, match="HTTPS"):
        _validate_urls({"endpoint": "http://overpass-api.de/api/interpreter"})


def test_unbounded_concurrency_is_rejected() -> None:
    with pytest.raises(SourceInputSelectionError, match="bounded"):
        _validate_budgets({"max_active_requests": 20})


def test_campaign_binding_enablement_preserves_existing_order(tmp_path: Path) -> None:
    path = tmp_path / "campaign.yaml"
    path.write_text(
        yaml.safe_dump(
            {"enabled_source_bindings": ["manual_seed_import"]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    _enable_bindings(
        path,
        ("berlin_osm_overpass", "berlin_official_http"),
    )

    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert value["enabled_source_bindings"] == [
        "manual_seed_import",
        "berlin_osm_overpass",
        "berlin_official_http",
    ]
