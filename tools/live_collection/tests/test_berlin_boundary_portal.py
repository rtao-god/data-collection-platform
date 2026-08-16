from __future__ import annotations

import httpx
import pytest
from tools.live_collection.berlin_boundary import BoundaryMaterializationError
from tools.live_collection.berlin_boundary_portal import (
    _metadata_candidates,
    _verify_official_request,
)


def test_json_ld_distribution_produces_licensed_official_candidate() -> None:
    html = """
    <html><head>
      <script type="application/ld+json">
      {
        "@type": "Dataset",
        "name": "Landesgrenze Berlin",
        "license": "dl-de-by-2.0",
        "distribution": [{
          "@type": "DataDownload",
          "name": "Landesgrenze GeoJSON",
          "contentUrl": "https://gdi.berlin.de/landesgrenze.geojson"
        }]
      }
      </script>
    </head></html>
    """

    candidates = _metadata_candidates(
        "https://daten.berlin.de/datensaetze/landesgrenze",
        html,
    )

    assert len(candidates) == 1
    assert candidates[0].resource_url == ("https://gdi.berlin.de/landesgrenze.geojson")
    assert candidates[0].license_identifier == "dl-de-by-2.0"


def test_json_ld_external_distribution_is_rejected() -> None:
    html = """
    <script type="application/ld+json">
    {
      "name": "Landesgrenze Berlin",
      "license": "dl-de-by-2.0",
      "distribution": [{
        "name": "Mirror",
        "contentUrl": "https://example.org/berlin.geojson"
      }]
    }
    </script>
    """

    assert not _metadata_candidates(
        "https://daten.berlin.de/datensaetze/landesgrenze",
        html,
    )


def test_redirect_request_hook_rejects_external_host() -> None:
    request = httpx.Request("GET", "https://example.org/berlin.geojson")

    with pytest.raises(BoundaryMaterializationError):
        _verify_official_request(request)
