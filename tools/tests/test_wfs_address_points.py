"""Address points come from the official address dataset with an explicit CRS and provenance."""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib

import pytest
from tools import wfs_address_points as wfs

TRIXX = {"street": "Prinzessinnenstrasse", "street_number": "16", "postal_code": "10969"}
TRIXX_EASTING = 392181.815
TRIXX_NORTHING = 5818151.337
OTHER_EASTING = 391096.583
OTHER_NORTHING = 5818583.769


def test_utm33n_inverse_matches_the_published_wgs84_point() -> None:
    latitude, longitude = wfs.utm33n_to_wgs84(TRIXX_EASTING, TRIXX_NORTHING)

    assert latitude == pytest.approx(52.5028179, abs=1e-6)
    assert longitude == pytest.approx(13.4115532, abs=1e-6)


def test_split_house_number_keeps_the_letter_and_refuses_ranges() -> None:
    assert wfs.split_house_number("33a") == (33, "a")
    assert wfs.split_house_number("16") == (16, "")
    assert wfs.split_house_number("19-20") is None


def _feature(
    *,
    street: str = "Prinzessinnenstraße",
    number: int = 16,
    suffix: str | None = None,
    postal_code: str = "10969",
    record_id: str = "187448",
    easting: float = TRIXX_EASTING,
    northing: float = TRIXX_NORTHING,
    observed: str = "2008-01-28",
    crs: str | None = None,
) -> dict:
    feature: dict = {
        "geometry": {"type": "Point", "coordinates": [easting, northing]},
        "properties": {
            "adressid": record_id,
            "hnr": number,
            "hnr_zusatz": suffix,
            "str_name": street,
            "str_nr": "03538",
            "plz": postal_code,
            "typ": "Adresse",
            "qualitaet": "Qualitaet A",
            "adr_datum": observed,
        },
    }
    if crs is not None:
        feature["crs"] = {"type": "name", "properties": {"name": crs}}
    return feature


def _payload(*features: dict, crs: str | None = "urn:ogc:def:crs:EPSG::25833") -> dict:
    payload: dict = {
        "type": "FeatureCollection",
        "features": list(features) or [_feature()],
        "timeStamp": "2026-11-21T05:55:25.985Z",
    }
    if crs is not None:
        payload["crs"] = {"type": "name", "properties": {"name": crs}}
    return payload


def _raw(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def _resolve(payload: dict, **search_overrides: str | None) -> wfs.AddressResolution:
    values = {**TRIXX, **search_overrides}
    search = wfs.create_search(values["street"], values["street_number"], values["postal_code"])
    assert search is not None
    return wfs.resolve_address_point(
        payload, search, wfs.request_url(search), raw_bytes=_raw(payload)
    )


def test_exact_address_yields_one_point_with_source_provenance() -> None:
    payload = _payload()
    resolution = _resolve(payload)

    assert resolution.resolved
    point = resolution.point
    assert point is not None
    assert point.address_record_id == "187448"
    assert point.address_record_date == "2008-01-28"
    assert point.dataset_identifier == "adressen_berlin"
    assert point.dataset_version == "adressen_berlin@2026-11-21T05:55:25Z"
    assert point.source_crs == "EPSG:25833"
    assert point.source_x == f"{TRIXX_EASTING:.3f}"
    assert point.source_y == f"{TRIXX_NORTHING:.3f}"
    assert point.source_digest == hashlib.sha256(_raw(payload)).hexdigest()
    assert point.source_observed_at == "2026-11-21T05:55:25Z"
    block = point.as_manifest_block("assertion")
    assert block["latitude"] == pytest.approx(52.5028179)
    assert block["sourceCrs"] == "EPSG:25833"
    assert block["outputCrs"] == "EPSG:4326"


def test_coarser_address_quality_stays_unresolved_with_its_class() -> None:
    payload = _payload(_feature())
    payload["features"][0]["properties"]["qualitaet"] = "Qualitaet B"

    resolution = _resolve(payload)

    assert resolution.point is None
    assert resolution.reason == f"{wfs.REASON_QUALITY_UNSUPPORTED}:Qualitaet B"


def test_registry_street_abbreviations_match_the_register_spelling() -> None:
    assert wfs.normalize_street_name("Mollstr.") == wfs.normalize_street_name("Mollstraße")
    assert wfs.normalize_street_name("Max-Beer-Str.") == wfs.normalize_street_name("Max-Beer-Straße")
    assert wfs.normalize_street_name("Schliemannstr") == wfs.normalize_street_name("Schliemannstraße")
    # Only the street suffix is expanded; a name that merely contains "str" stays literal.
    assert wfs.normalize_street_name("Stralauer Allee") == "stralauer allee"


def test_one_word_suffix_matches_the_register_spelling_with_a_space() -> None:
    # The register separates the suffix and the registry writes one word; both name one street.
    assert wfs.normalize_street_name("Skalitzerstr.") == wfs.normalize_street_name("Skalitzer Straße")
    assert wfs.normalize_street_name("Forsterstrasse") == wfs.normalize_street_name("Forster Straße")
    assert wfs.normalize_street_name("Holzmarktstraße") == wfs.normalize_street_name("Holzmarkt Str.")
    assert wfs.normalize_street_name("Prinzessinnenstrasse") == wfs.normalize_street_name(
        "Prinzessinnenstraße"
    )


def test_one_word_suffix_resolves_the_address_the_register_lists_with_a_space() -> None:
    payload = _payload(
        _feature(street="Skalitzer Straße", number=136, postal_code="10999", record_id="366187")
    )
    search = wfs.create_search("Skalitzerstr.", "136", "10999")
    assert search is not None

    resolution = wfs.resolve_address_point(
        payload, search, wfs.request_url(search), raw_bytes=_raw(payload)
    )

    assert resolution.resolved, resolution.reason
    assert resolution.point is not None
    assert resolution.point.address_record_id == "366187"


def test_same_postal_code_and_house_number_in_two_streets_stay_distinct() -> None:
    payload = _payload(
        _feature(street="Prinzessinnenstraße", record_id="187448"),
        _feature(street="Markgrafenstraße", record_id="246505", easting=OTHER_EASTING, northing=OTHER_NORTHING),
    )

    trixx = _resolve(payload)
    markgrafen = _resolve(payload, street="Markgrafenstrasse")

    assert trixx.point is not None and trixx.point.address_record_id == "187448"
    assert markgrafen.point is not None and markgrafen.point.address_record_id == "246505"
    assert trixx.point.latitude != markgrafen.point.latitude
    assert _resolve(payload, street="Lindenstrasse").reason == wfs.REASON_NOT_FOUND


def test_street_spelling_variant_matches_the_same_record() -> None:
    payload = _payload()

    for spelling in ("Prinzessinnenstrasse", "prinzessinnenstraße", "PRINZESSINNENSTRASSE"):
        resolution = _resolve(payload, street=spelling)
        assert resolution.resolved, (spelling, resolution.reason)
        assert resolution.point is not None
        assert resolution.point.address_record_id == "187448"


def test_two_matching_records_are_ambiguous_not_first_result() -> None:
    payload = _payload(_feature(record_id="187448"), _feature(record_id="187449"))

    resolution = _resolve(payload)

    assert resolution.reason == wfs.REASON_AMBIGUOUS
    assert resolution.point is None


def test_expected_address_record_id_must_match_the_response() -> None:
    payload = _payload()
    search = wfs.create_search("Prinzessinnenstrasse", "16", "10969", "999999")
    assert search is not None

    resolution = wfs.resolve_address_point(
        payload, search, wfs.request_url(search), raw_bytes=_raw(payload)
    )

    assert resolution.reason == wfs.REASON_CONFLICTING_RECORD
    expected = wfs.create_search("Prinzessinnenstrasse", "16", "10969", "187448")
    assert expected is not None
    assert wfs.resolve_address_point(
        payload, expected, wfs.request_url(expected), raw_bytes=_raw(payload)
    ).resolved


@pytest.mark.parametrize(
    "crs", ["urn:ogc:def:crs:EPSG::4326", None, "urn:ogc:def:crs:EPSG::25832"]
)
def test_unknown_or_wrong_source_crs_is_refused(crs: str | None) -> None:
    payload = _payload(crs=crs)

    assert _resolve(payload).reason == wfs.REASON_CRS_UNSUPPORTED


def test_mixed_feature_crs_is_refused() -> None:
    payload = _payload(
        _feature(),
        _feature(record_id="2", crs="urn:ogc:def:crs:EPSG::4326"),
    )

    assert _resolve(payload).reason == wfs.REASON_CRS_UNSUPPORTED


def test_missing_street_in_the_response_never_matches() -> None:
    payload = _payload(_feature(street=""))

    assert _resolve(payload).reason == wfs.REASON_NOT_FOUND


def test_incomplete_address_has_no_search() -> None:
    assert wfs.create_search(None, "16", "10969") is None
    assert wfs.create_search("Prinzessinnenstrasse", "", "10969") is None
    assert wfs.create_search("Prinzessinnenstrasse", "16", None) is None
    lookup = wfs.AddressPointLookup(pathlib.Path("unused"))
    assert lookup(None, "16", "10969").reason == wfs.REASON_ADDRESS_INCOMPLETE
    assert lookup.requests_made == 0


def test_point_outside_the_official_berlin_boundary_is_refused() -> None:
    payload = _payload(_feature(easting=383000.0, northing=5806000.0))

    assert _resolve(payload).reason == wfs.REASON_OUTSIDE_BOUNDARY


def test_boundary_is_the_official_polygon_not_the_bounding_box() -> None:
    # Inside Berlin's bounding box, outside the official boundary (south of the city).
    assert not wfs._in_berlin(52.39173, 13.2806)
    assert not wfs._in_berlin(52.34118, 13.53212)
    assert wfs._in_berlin(52.5028179, 13.4115532)


def _cached(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> tuple[wfs.AddressPointLookup, list[str]]:
    calls: list[str] = []
    payload = _payload(
        _feature(street="Prinzessinnenstraße", record_id="187448"),
        _feature(street="Markgrafenstraße", record_id="246505", easting=OTHER_EASTING, northing=OTHER_NORTHING),
    )

    def fake_fetch(url: str, timeout: float) -> bytes:
        calls.append(url)
        return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(wfs, "_fetch", fake_fetch)
    return wfs.AddressPointLookup(tmp_path), calls


def test_repeated_lookup_reuses_the_cached_raw_response(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup, calls = _cached(tmp_path, monkeypatch)

    first = lookup("Prinzessinnenstrasse", "16", "10969")
    second = lookup("Prinzessinnenstrasse", "16", "10969")

    assert first.resolved and second.resolved
    assert first.point is not None and second.point is not None
    assert first.point.as_manifest_block("a") == second.point.as_manifest_block("a")
    assert len(calls) == 1
    assert lookup.requests_made == 1 and lookup.cache_hits == 1


def test_cache_identity_includes_the_complete_street_identity(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup, calls = _cached(tmp_path, monkeypatch)

    trixx = lookup("Prinzessinnenstrasse", "16", "10969")
    markgrafen = lookup("Markgrafenstrasse", "16", "10969")

    assert trixx.point is not None and markgrafen.point is not None
    assert trixx.point.address_record_id == "187448"
    assert markgrafen.point.address_record_id == "246505"
    # Two different streets cannot share one cached response, so both are fetched.
    assert len(calls) == 2, calls
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_legacy_cache_schema_is_refused_instead_of_reused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup, _ = _cached(tmp_path, monkeypatch)
    search = wfs.create_search("Prinzessinnenstrasse", "16", "10969")
    assert search is not None
    path = tmp_path / f"{search.cache_key()[:32]}.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")

    with pytest.raises(wfs.WfsError, match="schema"):
        lookup("Prinzessinnenstrasse", "16", "10969")


def test_changed_cached_source_bytes_are_detected(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup, _ = _cached(tmp_path, monkeypatch)
    _ = lookup("Prinzessinnenstrasse", "16", "10969")
    search = wfs.create_search("Prinzessinnenstrasse", "16", "10969")
    assert search is not None
    path = tmp_path / f"{search.cache_key()[:32]}.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["responseBytesBase64"] = base64.b64encode(
        json.dumps(_payload(_feature(record_id="000001"))).encode("utf-8")
    ).decode("ascii")
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(wfs.WfsError, match="digest"):
        lookup("Prinzessinnenstrasse", "16", "10969")
