"""Address points come from the address dataset with a named CRS, never from an excerpt."""

from __future__ import annotations

import json
import pathlib

import pytest
from tools import wfs_address_points as wfs


def test_utm33n_inverse_matches_the_published_wgs84_point() -> None:
    latitude, longitude = wfs.utm33n_to_wgs84(392181.815, 5818151.337)

    assert latitude == pytest.approx(52.5028179, abs=1e-6)
    assert longitude == pytest.approx(13.4115532, abs=1e-6)


def test_split_house_number_keeps_the_letter_and_refuses_ranges() -> None:
    assert wfs.split_house_number("33a") == (33, "a")
    assert wfs.split_house_number("16") == (16, "")
    assert wfs.split_house_number("19-20") is None


def _payload(**overrides: object) -> dict[str, object]:
    properties = {
        "adressid": "187448",
        "hnr": 16,
        "hnr_zusatz": None,
        "plz": "10969",
        "typ": "Adresse",
        "qualitaet": "Qualitaet A",
        "adr_datum": "2008-01-28",
    }
    properties.update(overrides)
    return {
        "features": [
            {
                "geometry": {"coordinates": [392181.815, 5818151.337]},
                "properties": properties,
            }
        ]
    }


def test_exact_address_yields_a_point_with_its_dataset_identity() -> None:
    point = wfs.parse_address_point(_payload(), "16", "10969", "reference")

    assert point is not None
    assert point.address_record_id == "187448"
    assert point.dataset_identifier == "adressen_berlin@2008-01-28"
    assert point.source_crs == "EPSG:25833"
    assert point.as_manifest_block("assertion")["latitude"] == pytest.approx(52.5028179)


@pytest.mark.parametrize(
    "overrides",
    [{"hnr_zusatz": "a"}, {"plz": "10115"}, {"qualitaet": "Qualitaet C"}, {"typ": "Ort"}],
)
def test_a_non_matching_address_never_becomes_a_coordinate(overrides: dict[str, object]) -> None:
    assert wfs.parse_address_point(_payload(**overrides), "16", "10969", "reference") is None


def test_repeated_lookup_reuses_the_cached_response(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_fetch(url: str, timeout: float) -> bytes:
        calls.append(url)
        return json.dumps(_payload()).encode("utf-8")

    monkeypatch.setattr(wfs, "_fetch", fake_fetch)
    lookup = wfs.AddressPointLookup(tmp_path)
    first = lookup("16", "10969")
    second = lookup("16", "10969")

    assert first is not None and second is not None
    assert first.as_manifest_block("a") == second.as_manifest_block("a")
    assert len(calls) == 1
    assert lookup.requests_made == 1 and lookup.cache_hits == 1
