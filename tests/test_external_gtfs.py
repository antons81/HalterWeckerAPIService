import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import (  # noqa: E402
    load_cities,
    load_gtfs_archive,
    merge_manifest_entries,
    transit_radar_manifest,
)
from external_gtfs import (  # noqa: E402
    authenticated_external_request,
    build_external_departure_index,
    build_external_lines,
    build_external_stop_packages,
    external_city_ids,
    load_external_cities,
    load_external_gtfs_sources,
    parse_external_gtfs_url_args,
    process_external_gtfs_sources,
    validate_external_gtfs_source,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _gtfs_zip(
    path: Path,
    *,
    stops: str,
    routes: str = "route_id,route_short_name,route_long_name,route_type\nR1,17,Line 17,0\n",
    trips: str = "route_id,service_id,trip_id,trip_headsign,direction_id\nR1,S1,T1,Towards Depot,0\n",
    stop_times: str = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:00:00,08:00:00,9022001000001001,1\n"
        "T1,08:10:00,08:10:00,9022001000002002,2\n"
    ),
    calendar: str | None = (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "S1,1,1,1,1,1,1,1,20200101,20301231\n"
    ),
    calendar_dates: str | None = None,
) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("stops.txt", stops)
        archive.writestr("routes.txt", routes)
        archive.writestr("trips.txt", trips)
        archive.writestr("stop_times.txt", stop_times)
        if calendar is not None:
            archive.writestr("calendar.txt", calendar)
        if calendar_dates is not None:
            archive.writestr("calendar_dates.txt", calendar_dates)
    return path


class ExternalGTFSRegistryTests(unittest.TestCase):
    def test_load_and_validate_sweden_registry(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        self.assertEqual(len(sources), 1)
        validate_external_gtfs_source(sources[0], REPOSITORY_ROOT)
        cities = load_external_cities(sources[0], REPOSITORY_ROOT)
        self.assertEqual([city["id"] for city in cities], ["stockholm"])
        self.assertEqual(cities[0]["packageMode"], "external")
        self.assertEqual(cities[0]["externalGTFSProvider"], "sweden")

    def test_duplicate_provider_ids_fail(self) -> None:
        source = {
            "id": "sweden",
            "cities": "config/sweden-cities.json",
            "timezone": "Europe/Stockholm",
            "identifierPrefix": "se:",
            "stopIDMode": "exact",
            "country": "SE",
        }
        with self.assertRaisesRegex(ValueError, "Duplicate external GTFS source id"):
            validate_external_gtfs_source(
                source,
                REPOSITORY_ROOT,
                known_source_ids={"sweden"},
            )

    def test_duplicate_city_ids_across_sources_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            cities_a = [{
                "id": "stockholm",
                "name": "Stockholm",
                "aliases": [],
                "latitude": 59.33,
                "longitude": 18.07,
                "radiusMeters": 1000,
                "packageMode": "external",
                "externalGTFSProvider": "sweden",
            }]
            cities_b = [dict(cities_a[0], externalGTFSProvider="sweden-b")]
            (root / "config" / "a.json").write_text(json.dumps(cities_a))
            (root / "config" / "b.json").write_text(json.dumps(cities_b))
            sources = [
                {
                    "id": "sweden",
                    "cities": "config/a.json",
                    "timezone": "Europe/Stockholm",
                    "identifierPrefix": "se:",
                    "stopIDMode": "exact",
                    "country": "SE",
                },
                {
                    "id": "sweden-b",
                    "cities": "config/b.json",
                    "timezone": "Europe/Stockholm",
                    "identifierPrefix": "se-b:",
                    "stopIDMode": "exact",
                    "country": "SE",
                },
            ]
            (root / "sources.json").write_text(json.dumps(sources))
            _gtfs_zip(
                root / "a.zip",
                stops="stop_id,stop_name,stop_lat,stop_lon\nA,A,59.33,18.07\n",
            )
            _gtfs_zip(
                root / "b.zip",
                stops="stop_id,stop_name,stop_lat,stop_lon\nB,B,59.33,18.07\n",
            )
            with self.assertRaisesRegex(ValueError, "Duplicate city id across feeds"):
                process_external_gtfs_sources(
                    repository_root=root,
                    sources_path=root / "sources.json",
                    url_by_provider={
                        "sweden": str(root / "a.zip"),
                        "sweden-b": str(root / "b.zip"),
                    },
                    output=root / "out",
                    load_gtfs_archive=load_gtfs_archive,
                )

    def test_invalid_timezone_fails(self) -> None:
        source = {
            "id": "sweden",
            "cities": "config/sweden-cities.json",
            "timezone": "Not/AZone",
            "identifierPrefix": "se:",
            "stopIDMode": "exact",
            "country": "SE",
        }
        with self.assertRaisesRegex(ValueError, "invalid timezone"):
            validate_external_gtfs_source(source, REPOSITORY_ROOT)

    def test_missing_city_file_fails(self) -> None:
        source = {
            "id": "sweden",
            "cities": "config/missing-cities.json",
            "timezone": "Europe/Stockholm",
            "identifierPrefix": "se:",
            "stopIDMode": "exact",
            "country": "SE",
        }
        with self.assertRaisesRegex(ValueError, "city file does not exist"):
            validate_external_gtfs_source(source, REPOSITORY_ROOT)

    def test_parse_external_gtfs_url_args(self) -> None:
        mapping = parse_external_gtfs_url_args([
            "sweden=https://example.test/sweden.zip",
            "denmark=https://example.test/dk.zip",
        ])
        self.assertEqual(
            mapping,
            {
                "sweden": "https://example.test/sweden.zip",
                "denmark": "https://example.test/dk.zip",
            },
        )
        with self.assertRaisesRegex(ValueError, "providerID=URL"):
            parse_external_gtfs_url_args(["sweden"])

    def test_missing_api_key_fails_without_leaking_secret(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "SAMTRAFIKEN_STATIC_API_KEY",
        ) as raised:
            authenticated_external_request(
                "sweden",
                "https://example.test/gtfs.zip",
                environ={},
            )
        self.assertNotIn("secret", str(raised.exception).lower())
        self.assertNotIn("key=", str(raised.exception).lower())


class ExternalStopAndDepartureTests(unittest.TestCase):
    def test_child_platform_collapses_into_parent_station(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "se.zip"
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type\n"
                    "9022001000001001,T-Centralen,59.331,18.058,,1\n"
                    "9022001000001001_1,T-Centralen platform 1,59.3311,18.0581,9022001000001001,0\n"
                    "9022001000002002,Slussen,59.320,18.072,,0\n"
                ),
            )
            cities = [{
                "id": "stockholm",
                "name": "Stockholm",
                "aliases": [],
                "latitude": 59.3293,
                "longitude": 18.0686,
                "radiusMeters": 30000,
                "packageMode": "external",
            }]
            with zipfile.ZipFile(archive_path) as archive:
                manifest, package_stops = build_external_stop_packages(
                    archive, cities, Path(temp) / "out", stop_id_mode="exact"
                )
            ids = {stop["id"] for stop in package_stops["stockholm"]}
            self.assertEqual(ids, {"9022001000001001", "9022001000002002"})
            self.assertEqual(manifest[0]["stopCount"], 2)
            payload = json.loads(
                (Path(temp) / "out" / "stops" / "stockholm.json").read_text()
            )
            self.assertEqual({item["id"] for item in payload}, ids)

    def test_radius_filtering_and_zero_stop_city_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "se.zip"
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "near,Near,59.3293,18.0686\n"
                    "far,Far,57.7089,11.9746\n"
                ),
            )
            cities = [{
                "id": "stockholm",
                "name": "Stockholm",
                "aliases": [],
                "latitude": 59.3293,
                "longitude": 18.0686,
                "radiusMeters": 5_000,
                "packageMode": "external",
            }]
            with zipfile.ZipFile(archive_path) as archive:
                _, package_stops = build_external_stop_packages(
                    archive, cities, Path(temp) / "out", stop_id_mode="exact"
                )
            self.assertEqual(
                [stop["id"] for stop in package_stops["stockholm"]],
                ["near"],
            )

            empty_city = [{
                "id": "kiruna",
                "name": "Kiruna",
                "aliases": [],
                "latitude": 67.8558,
                "longitude": 20.2253,
                "radiusMeters": 500,
                "packageMode": "external",
            }]
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaisesRegex(ValueError, "No stops found"):
                    build_external_stop_packages(
                        archive,
                        empty_city,
                        Path(temp) / "out2",
                        stop_id_mode="exact",
                    )

    def test_route_lines_and_compact_departures(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "se.zip"
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "9022001000001001,T-Centralen,59.331,18.058\n"
                    "9022001000002002,Slussen,59.320,18.072\n"
                ),
                routes=(
                    "route_id,route_short_name,route_long_name,route_type\n"
                    "R1,17,,0\n"
                    "R2,,Blue Line,1\n"
                ),
                trips=(
                    "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                    "R1,S1,T1,Towards Slussen,0\n"
                    "R2,S1,T2,,1\n"
                    "R1,S1,T3,,0\n"
                ),
                stop_times=(
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "T1,08:00:00,08:00:00,9022001000001001,1\n"
                    "T1,08:10:00,08:10:00,9022001000002002,2\n"
                    "T2,09:00:00,09:00:00,9022001000001001,1\n"
                    "T2,09:12:00,09:12:00,9022001000002002,2\n"
                    "T3,25:15:00,25:15:00,9022001000001001,1\n"
                    "T3,25:30:00,25:30:00,9022001000002002,2\n"
                ),
            )
            cities = [{
                "id": "stockholm",
                "name": "Stockholm",
                "aliases": [],
                "latitude": 59.3293,
                "longitude": 18.0686,
                "radiusMeters": 30000,
                "packageMode": "external",
            }]
            out = root / "out"
            with zipfile.ZipFile(archive_path) as archive:
                _, package_stops = build_external_stop_packages(
                    archive, cities, out, stop_id_mode="exact"
                )
                lines = build_external_lines(archive, package_stops)
                build_external_departure_index(
                    archive, cities, out, timezone_name="Europe/Stockholm"
                )

            self.assertIn("9022001000001001", lines)
            self.assertEqual(lines["9022001000001001"]["R1"]["names"], ["17"])
            self.assertEqual(
                lines["9022001000001001"]["R2"]["names"],
                ["Blue Line"],
            )

            departures = json.loads((out / "departures" / "stockholm.json").read_text())
            self.assertEqual(departures["timezone"], "Europe/Stockholm")
            self.assertIn("generatedAt", departures)
            stop_board = departures["stops"]["9022001000001001"]
            by_trip = {item["t"]: item for item in stop_board}
            self.assertEqual(by_trip["T1"]["h"], "Towards Slussen")
            self.assertEqual(by_trip["T1"]["r"], "R1")
            self.assertEqual(by_trip["T1"]["p"], "08:00:00")
            self.assertEqual(by_trip["T2"]["h"], "Slussen")  # terminal fallback
            self.assertEqual(by_trip["T3"]["p"], "25:15:00")
            # exact IDs only — no shortened alternate keys
            self.assertNotIn("0001001", departures["stops"])

    def test_calendar_dates_only_feed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "se.zip"
            from datetime import datetime
            from zoneinfo import ZoneInfo

            today = datetime.now(ZoneInfo("Europe/Stockholm")).date().strftime("%Y%m%d")
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "9022001000001001,T-Centralen,59.331,18.058\n"
                    "9022001000002002,Slussen,59.320,18.072\n"
                ),
                calendar=None,
                calendar_dates=(
                    "service_id,date,exception_type\n"
                    f"S1,{today},1\n"
                ),
            )
            cities = [{
                "id": "stockholm",
                "name": "Stockholm",
                "aliases": [],
                "latitude": 59.3293,
                "longitude": 18.0686,
                "radiusMeters": 30000,
                "packageMode": "external",
            }]
            out = root / "out"
            with zipfile.ZipFile(archive_path) as archive:
                build_external_stop_packages(archive, cities, out, stop_id_mode="exact")
                build_external_departure_index(
                    archive, cities, out, timezone_name="Europe/Stockholm"
                )
            departures = json.loads((out / "departures" / "stockholm.json").read_text())
            self.assertGreater(len(departures["stops"]["9022001000001001"]), 0)

    def test_stockholm_appears_once_in_manifests(self) -> None:
        cities = load_cities(REPOSITORY_ROOT / "config" / "sweden-cities.json")
        radar = transit_radar_manifest(cities, skip_auto_radar_stops=True)
        app_ids = [city["appCityID"] for city in radar["cities"]]
        self.assertEqual(app_ids.count("stockholm"), 1)
        stockholm = next(city for city in radar["cities"] if city["appCityID"] == "stockholm")
        self.assertEqual(stockholm["cityID"], "stockholm-se")
        provider = stockholm["providers"][0]
        self.assertEqual(provider["adapter"], "sweden")
        self.assertEqual(provider["operator"], "sl")
        self.assertEqual(provider["providerID"], "sweden-stockholm")
        self.assertNotIn("gatewayURL", provider)
        self.assertNotIn("region", provider)

        manifest: list[dict[str, object]] = []
        sources: dict[str, str] = {}
        merge_manifest_entries(
            manifest,
            [{"id": "stockholm", "name": "Stockholm"}],
            source="External GTFS source sweden",
            sources_by_city_id=sources,
        )
        self.assertEqual([entry["id"] for entry in manifest].count("stockholm"), 1)

    def test_end_to_end_process_with_local_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            archive_path = root / "sweden.zip"
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_name,stop_lat,stop_lon,parent_station\n"
                    "9022001000001001,T-Centralen,59.331,18.058,\n"
                    "9022001000001001_1,T-Centralen P1,59.3311,18.0581,9022001000001001\n"
                ),
            )
            (root / "config" / "sweden-cities.json").write_text(json.dumps([{
                "id": "stockholm",
                "name": "Stockholm",
                "aliases": [],
                "latitude": 59.3293,
                "longitude": 18.0686,
                "radiusMeters": 30000,
                "packageMode": "external",
                "externalGTFSProvider": "sweden",
                "transitRadar": {
                    "adapter": "sweden",
                    "operator": "sl",
                    "isEnabled": True,
                    "features": [
                        "liveVehicles",
                        "realtimeDepartures",
                        "firstDepartures",
                        "realtimeDelay",
                    ],
                },
            }]))
            (root / "config" / "external-gtfs-sources.json").write_text(json.dumps([{
                "id": "sweden",
                "cities": "config/sweden-cities.json",
                "timezone": "Europe/Stockholm",
                "identifierPrefix": "se:",
                "stopIDMode": "exact",
                "country": "SE",
                "buildStops": True,
                "buildRoutes": True,
                "buildDepartures": True,
            }]))
            out = root / "out"
            entries, external_cities, package_stops, lines = process_external_gtfs_sources(
                repository_root=root,
                sources_path=root / "config" / "external-gtfs-sources.json",
                url_by_provider={"sweden": str(archive_path)},
                output=out,
                load_gtfs_archive=load_gtfs_archive,
                environ={"SAMTRAFIKEN_STATIC_API_KEY": "test-key-not-for-production"},
            )
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["id"], "stockholm")
            self.assertEqual([city["id"] for city in external_cities], ["stockholm"])
            self.assertTrue((out / "stops" / "stockholm.json").exists())
            self.assertTrue((out / "departures" / "stockholm.json").exists())
            self.assertTrue((out / "routes" / "stockholm.json").exists())
            ids = {stop["id"] for stop in package_stops["stockholm"]}
            self.assertEqual(ids, {"9022001000001001", "9022001000001001_1"})
            self.assertTrue(lines)

            url, headers = authenticated_external_request(
                "sweden",
                "https://example.test/gtfs.zip",
                environ={"SAMTRAFIKEN_STATIC_API_KEY": "unit-test-key"},
            )
            self.assertIn("key=unit-test-key", url)
            self.assertEqual(headers.get("Accept-Encoding"), "gzip")

    def test_gzip_payload_is_accepted_by_load_gtfs_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w") as archive:
                archive.writestr(
                    "stops.txt",
                    "stop_id,stop_name,stop_lat,stop_lon\nA,A,59.33,18.07\n",
                )
            compressed = __import__("gzip").compress(zip_buffer.getvalue())
            class FakeResponse:
                def read(self):
                    return compressed
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            with mock.patch(
                "build_stop_packages.urllib.request.urlopen",
                return_value=FakeResponse(),
            ):
                archive = load_gtfs_archive(
                    "https://example.test/sweden.gtfs",
                    headers={"Accept-Encoding": "gzip"},
                )
                names = archive.namelist()
                archive.close()
            self.assertIn("stops.txt", names)


class ExternalExclusionTests(unittest.TestCase):
    def test_external_city_ids_include_stockholm(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        ids = external_city_ids(sources, REPOSITORY_ROOT)
        self.assertIn("stockholm", ids)

    def test_configured_external_city_ids_excludes_stockholm_from_sqlite(self) -> None:
        from import_static_departures_database import configured_external_city_ids

        excluded = configured_external_city_ids(
            REPOSITORY_ROOT / "config" / "cities.json",
            REPOSITORY_ROOT / "config" / "swiss-cities.json",
        )
        self.assertIn("stockholm", excluded)
        self.assertIn("wien", excluded)
        self.assertIn("zurich", excluded)


if __name__ == "__main__":
    unittest.main()
