import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from kyiv_open_data import (
    KyivOpenDataError,
    datastore_records_to_geojson,
    load_datastore_records,
    normalize_station_features,
    resolve_datastore_download_url,
)


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


def test_datastore_pagination_reads_total_records():
    calls = []

    def opener(request, timeout):
        query = parse_qs(urlparse(request.full_url).query)
        offset = int(query["offset"][0])
        calls.append(offset)
        page = [{"_id": offset + 1, "code1": f"mr{offset + 1}"}]
        return FakeResponse({
            "success": True,
            "result": {"total": 3, "records": page},
        })

    records, total = load_datastore_records(
        "93a62dca-e442-43b6-a00e-112c7eb6c13f",
        page_size=1,
        opener=opener,
    )

    assert total == 3
    assert len(records) == 3
    assert calls == [0, 1, 2]


def test_datastore_station_records_are_normalized_to_geojson():
    payload = datastore_records_to_geojson([
        {
            "_id": 1,
            "code1": "fn01",
            "name": "Фунікулер. Верхня станція",
            "lat": 50.4569,
            "lon": 30.5229,
        }
    ], name="stopsFuniculer")

    stations = normalize_station_features(payload, system="funicular")

    assert len(stations) == 1
    assert stations[0]["id"] == "fn01"
    assert stations[0]["latitude"] == pytest.approx(50.4569)
    assert stations[0]["longitude"] == pytest.approx(30.5229)


def test_datastore_never_allows_internal_stage_url():
    def opener(request, timeout):
        return FakeResponse({
            "success": True,
            "result": {
                "total": 1,
                "records": [{
                    "resource_url": "https://gisserver-stage.kyivcity.gov.ua/internal"
                }],
            },
        })

    with pytest.raises(KyivOpenDataError, match="non-public download URL"):
        resolve_datastore_download_url("93a62dca-e442-43b6-a00e-112c7eb6c13f", opener=opener)


def test_datastore_resolves_official_public_download_url():
    expected = (
        "https://data.kyivcity.gov.ua/dataset/dani-pro-mistse-rozmishchennia-zupynok-"
        "miskoho-elektrychnoho-ta-avtomobilnoho-transpor-dep-transport/resource/"
        "93a62dca-e442-43b6-a00e-112c7eb6c13f/data/download"
    )

    def opener(request, timeout):
        return FakeResponse({
            "success": True,
            "result": {
                "total": 1,
                "records": [{"resource_url": expected}],
            },
        })

    assert resolve_datastore_download_url(
        "93a62dca-e442-43b6-a00e-112c7eb6c13f",
        opener=opener,
    ) == expected


def test_kyiv_resource_registry_keeps_expected_station_counts():
    config_path = Path(__file__).parents[1] / "config" / "kyiv-systems-resources.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["expectedCounts"]["metroStations"] == 52
    assert config["expectedCounts"]["funicularStations"] == 2
    assert config["expectedIDs"]["funicularStations"] == ["fn01", "fn02"]
    assert config["resources"]["metroStations"]["resourceID"] == (
        "93a62dca-e442-43b6-a00e-112c7eb6c13f"
    )
    assert config["resources"]["funicularStations"]["resourceID"] == (
        "984462ae-86cd-40b8-a3f5-68064a97408d"
    )
