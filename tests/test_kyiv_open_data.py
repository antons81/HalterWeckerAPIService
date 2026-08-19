import json
import sys
import tempfile
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from kyiv_open_data import (
    KyivOpenDataError,
    KyivResourceCache,
    _read_url,
    build_kyiv_systems_artifact,
    copy_validated_kyiv_systems_artifact,
    datastore_records_to_geojson,
    load_datastore_records,
    normalize_station_features,
    resolve_datastore_download_url,
    validate_kyiv_systems_artifact,
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


def _json_response(payload, status=200):
    response = FakeResponse(payload)
    response.status = status
    return response


def _station_payload(station_id="station-1"):
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "id": station_id,
            "geometry": {"type": "Point", "coordinates": [30.5, 50.4]},
            "properties": {"code1": station_id, "name": "Station", "line": "1"},
        }],
    }


def _topology_payload():
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "id": "edge-1",
            "geometry": {
                "type": "LineString",
                "coordinates": [[30.5, 50.4], [30.6, 50.5]],
            },
            "properties": {
                "globalid": "edge-1",
                "from_code1": "station-1",
                "to_code1": "station-2",
                "num_route": "1",
                "napryamok": "forward",
            },
        }],
    }


def _attributes_payload(attributes):
    return {"features": [{"attributes": attributes}]}


def _resource_fixture():
    names = (
        "metroStations", "metroTopology", "metroStopTimes", "metroPeriods",
        "metroCalendar", "metroInterchanges", "funicularStations",
        "funicularTopology", "funicularStopTimes", "funicularPeriods",
        "funicularCalendar", "funicularInterchanges", "expressStations",
        "expressTopology", "expressStopTimes", "expressInterchanges",
    )
    stop_time = {
        "code1": "station-1", "name": "Station", "line": "1",
        "napryamok": "forward", "first_trn1": "05:00", "last_trn1": "23:00",
    }
    period = {
        "line": "1", "timeperiod": "weekday", "st_weekday": "10",
        "rv_weekday": "10", "st_holiday": "15", "rv_holiday": "15",
    }
    calendar = {
        "code1": "station-1", "line": "1", "open_max": "05:00", "close_min": "23:00",
    }
    interchange = {
        "from_code1": "station-1", "to_code1": "station-2",
        "from_name": "From", "to_name": "To", "comment": "",
    }
    express_stop_time = {
        "code1": "station-1", "name": "Station", "num_route": "E1",
        "num_r_eng": "E1", "napryamok": "forward", "type": "regular",
        "train": "train-1", "arrival": "05:00", "departure": "05:01", "actual": False,
    }
    payloads = {
        "metroStations": _station_payload(),
        "funicularStations": _station_payload("fn01"),
        "expressStations": _station_payload(),
        "metroTopology": _topology_payload(),
        "funicularTopology": _topology_payload(),
        "expressTopology": _topology_payload(),
        "metroStopTimes": _attributes_payload(stop_time),
        "funicularStopTimes": _attributes_payload(stop_time),
        "metroPeriods": _attributes_payload(period),
        "funicularPeriods": _attributes_payload(period),
        "metroCalendar": _attributes_payload(calendar),
        "funicularCalendar": _attributes_payload(calendar),
        "metroInterchanges": _attributes_payload(interchange),
        "funicularInterchanges": _attributes_payload(interchange),
        "expressStopTimes": _attributes_payload(express_stop_time),
        "expressInterchanges": _attributes_payload(interchange),
    }
    config = {
        "expectedCounts": {
            "metroStations": 1,
            "funicularStations": 1,
            "expressPlatforms": 1,
            "expressStopTimes": 1,
        },
        "expectedIDs": {"funicularStations": ["fn01"]},
        "resources": {
            name: {
                "name": name,
                "resourceID": f"resource-{name}",
                "isAPI": False,
                "downloadURL": f"https://data.kyivcity.gov.ua/{name}",
            }
            for name in names
        },
    }
    return config, payloads


def _build_fixture(root, opener, *, cache_root=None):
    config, payloads = _resource_fixture()
    config_path = root / "kyiv-systems-resources.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    output = root / "out" / "transit" / "kyiv-systems.json"
    return build_kyiv_systems_artifact(
        repository_root=root,
        output=output,
        sources_path=config_path,
        opener=opener,
        cache_root=cache_root,
        sleep=lambda _seconds: None,
    ), payloads


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


def test_read_url_retries_timeouts_then_succeeds():
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        if len(calls) < 3:
            raise TimeoutError("simulated timeout")
        return _json_response({"ok": True})

    body, _ = _read_url(
        "https://data.kyivcity.gov.ua/resource/data/download",
        opener=opener,
        source_name="test-resource",
        sleep=lambda _seconds: None,
    )

    assert json.loads(body) == {"ok": True}
    assert len(calls) == 3
    assert all(timeout == 45 for _, timeout in calls)


def test_read_url_does_not_retry_permanent_http_error():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return _json_response({}, status=404)

    with pytest.raises(KyivOpenDataError, match="HTTP 404"):
        _read_url(
            "https://data.kyivcity.gov.ua/resource/data/download",
            opener=opener,
            source_name="missing-resource",
            sleep=lambda _seconds: None,
        )

    assert len(calls) == 1


def test_happy_path_builds_and_populates_validated_cache():
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        config, payloads = _resource_fixture()

        def opener(request, timeout):
            return _json_response(payloads[Path(urlparse(request.full_url).path).name])

        output, _ = _build_fixture(root, opener, cache_root=root / "cache")

        assert output.is_file()
        assert json.loads(output.read_text(encoding="utf-8"))["cityID"] == "kyiv"
        for spec in config["resources"].values():
            assert (root / "cache" / f"{spec['resourceID']}.json").is_file()


def test_build_retries_two_timeouts_then_completes():
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        _, payloads = _resource_fixture()
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) <= 2:
                raise TimeoutError("simulated timeout")
            return _json_response(payloads[Path(urlparse(request.full_url).path).name])

        output, _ = _build_fixture(root, opener, cache_root=root / "cache")

        assert output.is_file()
        assert len(calls) == 2 + len(payloads)


def test_all_attempts_failed_uses_valid_cache(capsys):
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        config, payloads = _resource_fixture()
        cache_root = root / "cache"

        def successful_opener(request, timeout):
            return _json_response(payloads[Path(urlparse(request.full_url).path).name])

        _build_fixture(root, successful_opener, cache_root=cache_root)

        calls = []

        def timeout_opener(request, timeout):
            calls.append(request.full_url)
            raise TimeoutError("simulated outage")

        output, _ = _build_fixture(root, timeout_opener, cache_root=cache_root)

        assert output.is_file()
        assert len(calls) == len(config["resources"]) * 3
        assert "using cached resource age=" in capsys.readouterr().out


def test_invalid_download_does_not_replace_existing_cache():
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        config, payloads = _resource_fixture()
        cache_root = root / "cache"

        def successful_opener(request, timeout):
            return _json_response(payloads[Path(urlparse(request.full_url).path).name])

        _build_fixture(root, successful_opener, cache_root=cache_root)
        metro_spec = config["resources"]["metroStations"]
        cache = KyivResourceCache(cache_root)
        before = cache.load(metro_spec)
        assert before is not None

        def invalid_opener(request, timeout):
            name = Path(urlparse(request.full_url).path).name
            if name == "metroStations":
                return _json_response({"type": "FeatureCollection", "features": []})
            return _json_response(payloads[name])

        output, _ = _build_fixture(root, invalid_opener, cache_root=cache_root)
        after = cache.load(metro_spec)

        assert output.is_file()
        assert after is not None
        assert after[0] == before[0]


def test_no_network_and_no_cache_does_not_create_empty_artifact():
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)

        def timeout_opener(request, timeout):
            raise urllib.error.URLError(TimeoutError("simulated outage"))

        with pytest.raises(KyivOpenDataError):
            _build_fixture(root, timeout_opener, cache_root=root / "empty-cache")

        assert not (root / "out" / "transit" / "kyiv-systems.json").exists()


def test_empty_previous_artifact_is_rejected():
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        source = root / "previous" / "kyiv-systems.json"
        destination = root / "staged" / "kyiv-systems.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({
            "schemaVersion": 1,
            "cityID": "kyiv",
            "source": {"contentDigest": "digest", "contentSize": 1},
            "systems": {
                "metro": {key: [] for key in ("stations", "topology", "stopTimes", "periods", "calendar", "interchanges")},
                "funicular": {key: [] for key in ("stations", "topology", "stopTimes", "periods", "calendar", "interchanges")},
                "cityExpress": {key: [] for key in ("platforms", "topology", "stopTimes", "interchanges")},
            },
        }), encoding="utf-8")

        with pytest.raises(KyivOpenDataError, match="no metro stations"):
            copy_validated_kyiv_systems_artifact(source, destination)

        assert not destination.exists()


def test_previous_artifact_with_minimal_data_is_copied_atomically():
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        config, payloads = _resource_fixture()

        def successful_opener(request, timeout):
            name = Path(urlparse(request.full_url).path).name
            return _json_response(payloads[name])

        source, _ = _build_fixture(root, successful_opener, cache_root=root / "cache")
        destination = root / "staged" / "kyiv-systems.json"

        validate_kyiv_systems_artifact(source)
        copy_validated_kyiv_systems_artifact(source, destination)

        assert destination.read_bytes() == source.read_bytes()
