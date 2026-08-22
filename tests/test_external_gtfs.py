import io
import json
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import (  # noqa: E402
    load_cities,
    load_gtfs_archive,
    merge_manifest_entries,
    transit_radar_manifest,
)
import import_static_departures_database as static_importer  # noqa: E402
from external_gtfs import (  # noqa: E402
    authenticated_external_request,
    build_external_departure_index,
    build_external_lines,
    build_external_route_index,
    build_external_stop_packages,
    build_external_trip_index,
    _deduplicate_merged_stops,
    external_city_ids,
    load_external_cities,
    load_external_gtfs_sources,
    parse_external_gtfs_url_args,
    process_external_gtfs_sources,
    validate_external_stop_packages,
    validate_external_gtfs_source,
    external_gtfs_resilience_policy,
    validate_kyiv_gtfs_archive,
)
from external_staging import ExternalMergeStage, iter_departure_payload  # noqa: E402
from gtfs_source_cache import GTFSArtifactCache  # noqa: E402


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
    def test_kyiv_gtfs_outage_without_cache_fails_closed_after_limited_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = next(
                item
                for item in load_external_gtfs_sources(
                    REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
                )
                if item["id"] == "kyiv"
            )
            source["supplementalStopCatalog"] = None
            source["buildStops"] = False
            source["buildRoutes"] = False
            source["buildDepartures"] = False
            source["buildTripIndex"] = False
            source["buildRadarTopology"] = False
            sources_path = root / "sources.json"
            sources_path.write_text(json.dumps([source]), encoding="utf-8")
            calls = []

            def unavailable(request, timeout=0):
                calls.append(request.get_method())
                raise urllib.error.URLError(TimeoutError("simulated outage"))

            with mock.patch(
                "gtfs_source_cache.urllib.request.urlopen",
                side_effect=unavailable,
            ):
                with self.assertRaises(urllib.error.URLError):
                    process_external_gtfs_sources(
                        repository_root=REPOSITORY_ROOT,
                        sources_path=sources_path,
                        url_by_provider={"kyiv": "https://data.kyivcity.gov.ua/gtfs.zip"},
                        output=root / "out",
                        load_gtfs_archive=load_gtfs_archive,
                        gtfs_cache=GTFSArtifactCache(root / "gtfs-cache"),
                    )

            self.assertEqual(calls, ["GET", "GET", "GET"])

    def test_streaming_merge_stages_large_departure_json_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            stops = [
                {"id": f"stop-{index}", "name": "Main", "latitude": 1.0, "longitude": 2.0, "searchName": "main"}
                for index in range(1200)
            ]
            (source / "stops.json").write_text(json.dumps(stops), encoding="utf-8")
            (source / "routes.json").write_text(
                json.dumps({"route-1": {"short_name": "1"}}), encoding="utf-8"
            )
            (source / "departures.json").write_text(
                json.dumps({
                    "generatedAt": "2026-08-16T00:00:00Z",
                    "timezone": "UTC",
                    "stops": {
                        f"stop-{index}": [
                            {"t": f"trip-{index}", "r": "route-1", "h": "Terminal", "d": "0", "p": "08:00:00", "q": "1"}
                        ]
                        for index in range(1200)
                    },
                    "platforms": {},
                }),
                encoding="utf-8",
            )
            (source / "trips.json").write_text(
                json.dumps({f"trip-{index}": {"r": "route-1"} for index in range(1200)}),
                encoding="utf-8",
            )
            output = root / "output"
            stage = ExternalMergeStage()
            try:
                stage.add_stops(source / "stops.json", "source-a")
                stage.add_object_file(source / "routes.json", "routes", "source-a")
                stage.add_departures(source / "departures.json")
                stage.add_object_file(source / "trips.json", "trips", "source-a")
                stage.write_outputs(output, "city")
            finally:
                stage.close()

            payload = json.loads((output / "departures" / "city.json").read_text())
            self.assertEqual(len(payload["stops"]), 1200)
            self.assertEqual(
                sum(1 for kind, _key, _value in iter_departure_payload(output / "departures" / "city.json") if kind == "stop"),
                1200,
            )

    def test_identical_merged_stop_ids_are_deduplicated(self) -> None:
        stop = {
            "id": "123",
            "name": "Main",
            "latitude": 40.0,
            "longitude": -73.0,
            "searchName": "main",
            "stopCode": None,
        }
        result = _deduplicate_merged_stops(
            [("subway", dict(stop)), ("bus", dict(stop))],
            "new-york",
        )
        self.assertEqual(result, [stop])

    def test_conflicting_merged_stop_ids_fail_with_sources(self) -> None:
        with self.assertRaisesRegex(ValueError, "new-york/123.*subway.*bus"):
            _deduplicate_merged_stops(
                [
                    ("subway", {"id": "123", "name": "Main", "latitude": 40.0, "longitude": -73.0}),
                    ("bus", {"id": "123", "name": "Different", "latitude": 40.1, "longitude": -73.1}),
                ],
                "new-york",
            )

    def test_wmata_external_city_uses_us_radar_city_id_and_timezone(self) -> None:
        city = load_external_cities(
            {"id": "wmata-bus", "cities": "config/wmata-cities.json"},
            REPOSITORY_ROOT,
        )[0]
        entry = transit_radar_manifest([city])["cities"][0]
        self.assertEqual(entry["cityID"], "washington-dc-us")
        self.assertEqual(entry["timeZoneIdentifier"], "America/New_York")
        self.assertEqual(entry["providers"][0]["providerID"], "wmata")

    def test_load_and_validate_sweden_registry(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        self.assertEqual(
            {source["id"] for source in sources},
            {
                "sweden",
                "norway",
                "ireland",
                "translink",
                "ttc-surface",
                "ttc-subway",
                "511-bay-area",
                "cta-chicago",
                "king-county-metro",
                "mta-ny-subway",
                "mta-ny-nyct-bus",
                "mta-ny-mta-bus",
                "mbta-boston",
                "wmata-bus",
                "wmata-rail",
                "kyiv",
                "stm-montreal",
                "finland-hsl",
                "finland-tampere",
                "finland-oulu",
                "finland-foli",
                "finland-hameenlinna",
                "finland-harma",
                "finland-joensuu",
                "finland-kajaani",
                "finland-korsisaari",
                "finland-kotka",
                "finland-kouvola",
                "finland-kuopio",
                "finland-linkki",
                "finland-lahti",
                "finland-lappeenranta",
                "finland-mikkeli",
                "finland-pahkakankaanliikenne",
                "finland-pohjolanmatka",
                "finland-pori",
                "finland-raasepori",
                "finland-rauma",
                "finland-rovaniemi",
                "finland-salo",
                "finland-varely",
                "finland-vaasa",
                "finland-digitraffic",
                "australia-translink-seq",
                "australia-adelaide",
            },
        )
        sweden = next(source for source in sources if source["id"] == "sweden")
        validate_external_gtfs_source(sweden, REPOSITORY_ROOT)
        cities = load_external_cities(sweden, REPOSITORY_ROOT)
        self.assertEqual(
            [city["id"] for city in cities],
            [
                "stockholm", "malmo", "goteborg", "uppsala", "vaxjo",
                "helsingborg", "linkoping", "jonkoping", "orebro", "vasteras",
            ],
        )
        self.assertEqual(cities[0]["packageMode"], "external")
        self.assertEqual(cities[0]["externalGTFSProvider"], "sweden")

    def test_kyiv_manifest_is_static_departures_plus_vehicle_positions(self) -> None:
        cities = load_cities(REPOSITORY_ROOT / "config" / "kyiv-cities.json")
        entry = transit_radar_manifest(cities)["cities"][0]
        provider = entry["providers"][0]

        self.assertEqual(entry["cityID"], "kyiv-ua")
        self.assertEqual(entry["timeZoneIdentifier"], "Europe/Kyiv")
        self.assertEqual(provider["providerID"], "kyiv")
        self.assertTrue(provider["staticOnly"])
        self.assertIn("liveVehicles", provider["features"])
        self.assertIn("vehiclePositions", provider["features"])
        self.assertNotIn("realtimeDepartures", provider["features"])
        self.assertNotIn("tripUpdates", provider["features"])

    def test_validate_ireland_local_registry_and_city_coverage(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        ireland = next(source for source in sources if source["id"] == "ireland")
        validate_external_gtfs_source(ireland, REPOSITORY_ROOT)
        cities = load_external_cities(ireland, REPOSITORY_ROOT)
        self.assertEqual(
            [city["id"] for city in cities],
            ["dublin", "cork", "galway", "limerick", "waterford"],
        )
        self.assertEqual(ireland["localPath"], "/srv/haltewecker/data/ireland/static")

    def test_validate_norway_registry_and_city_coverage(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        norway = next(source for source in sources if source["id"] == "norway")
        validate_external_gtfs_source(norway, REPOSITORY_ROOT)
        self.assertEqual(
            norway["url"],
            "https://storage.googleapis.com/marduk-production/outbound/gtfs/rb_norway-aggregated-gtfs.zip",
        )
        cities = load_external_cities(norway, REPOSITORY_ROOT)
        self.assertEqual(len(cities), 11)
        self.assertEqual({city["id"] for city in cities}, {
            "oslo", "bergen", "stavanger", "trondheim", "drammen",
            "fredrikstad", "skien", "kristiansand", "tonsberg",
            "alesund", "tromso",
        })

    def test_validate_translink_registry_and_static_manifest(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        translink = next(source for source in sources if source["id"] == "translink")
        validate_external_gtfs_source(translink, REPOSITORY_ROOT)
        cities = load_external_cities(translink, REPOSITORY_ROOT)
        self.assertEqual([city["id"] for city in cities], ["vancouver"])
        manifest = transit_radar_manifest(cities)
        city = manifest["cities"][0]
        provider = city["providers"][0]
        self.assertEqual(city["cityID"], "vancouver-ca")
        self.assertEqual(provider["providerID"], "translink-vancouver")
        self.assertEqual(provider["staticBaseURL"], "https://api.asoftlabs.app")
        self.assertEqual(
            provider["boardURL"],
            "https://api.asoftlabs.app/static-departures",
        )
        self.assertEqual(
            provider["realtimeURL"],
            "https://api.asoftlabs.app/translink/realtime/trip-updates",
        )
        self.assertNotIn("liveVehicles", provider["features"])
        self.assertTrue(translink["importIntoStaticDepartures"])
        self.assertEqual(translink["staticIdentifierPrefix"], "ca:")
        self.assertEqual(translink["staticStopIDPrefix"], "ca:")

    def test_validate_ttc_registry_and_static_manifest(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        ttc_sources = [
            source for source in sources if str(source["id"]).startswith("ttc-")
        ]
        self.assertEqual(
            {source["id"] for source in ttc_sources},
            {"ttc-surface", "ttc-subway"},
        )
        for source in ttc_sources:
            validate_external_gtfs_source(source, REPOSITORY_ROOT)
            self.assertEqual(source["timezone"], "America/Toronto")
            self.assertEqual(source["mergeGroup"], "toronto")
            self.assertTrue(str(source["namespace"]).startswith("ttc-"))
            self.assertTrue(source["importIntoStaticDepartures"])
            self.assertNotIn("apiKey", source)

        cities = load_external_cities(ttc_sources[0], REPOSITORY_ROOT)
        manifest = transit_radar_manifest(cities)
        city = manifest["cities"][0]
        provider = city["providers"][0]
        self.assertEqual(city["cityID"], "toronto-ca")
        self.assertEqual(provider["providerID"], "ttc-toronto")
        self.assertEqual(
            provider["boardURL"],
            "https://api.asoftlabs.app/static-departures",
        )
        self.assertEqual(
            provider["realtimeURL"],
            "https://api.asoftlabs.app/ttc/realtime/trip-updates",
        )
        self.assertIn("tripUpdates", provider["features"])
        self.assertNotIn("liveVehicles", provider["features"])
        self.assertNotIn("vehiclePositions", provider["features"])

    def test_validate_511_registry_preserves_native_ids_and_radar_urls(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        source = next(source for source in sources if source["id"] == "511-bay-area")
        validate_external_gtfs_source(source, REPOSITORY_ROOT)
        self.assertEqual(source["identifierPrefix"], "")
        self.assertTrue(source["preserveNativeIDs"])
        self.assertEqual(source["staticStopIDPrefix"], "511-bay-area:")
        self.assertNotIn("url", source)
        cities = load_external_cities(source, REPOSITORY_ROOT)
        manifest = transit_radar_manifest(cities)
        self.assertEqual(
            [city["cityID"] for city in manifest["cities"]],
            ["berkeley-us", "oakland-us", "san-francisco-us", "san-jose-us"],
        )
        provider = manifest["cities"][0]["providers"][0]
        self.assertEqual(provider["providerID"], "511-bay-area-berkeley")
        self.assertEqual(provider["adapter"], "bayArea511")
        self.assertEqual(provider["tripUpdatesURL"], "https://api.asoftlabs.app/511/realtime/trip-updates")
        self.assertIn("vehiclePositions", provider["features"])

    def test_validate_cta_chicago_static_registry_and_manifest(self) -> None:
        sources = load_external_gtfs_sources(
            REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
        )
        source = next(source for source in sources if source["id"] == "cta-chicago")
        validate_external_gtfs_source(source, REPOSITORY_ROOT)
        self.assertEqual(source["timezone"], "America/Chicago")
        self.assertEqual(source["identifierPrefix"], "cta-chicago:")
        self.assertEqual(source["staticStopIDPrefix"], "cta-chicago:")
        self.assertTrue(source["publishPassengerStopIDs"])
        cities = load_external_cities(source, REPOSITORY_ROOT)
        manifest = transit_radar_manifest(cities)
        city = manifest["cities"][0]
        provider = city["providers"][0]
        self.assertEqual(city["cityID"], "chicago-us")
        self.assertEqual(provider["providerID"], "cta-chicago")
        self.assertEqual(provider["adapter"], "ctaChicago")
        self.assertEqual(
            provider["features"],
            ["firstDepartures", "stopLookup"],
        )
        self.assertEqual(
            provider["boardURL"],
            "https://api.asoftlabs.app/static-departures",
        )

    def test_511_auth_requires_only_the_backend_environment_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "API_511_KEY") as raised:
            authenticated_external_request(
                "511-bay-area",
                "https://api.511.org/transit/datafeeds?operator_id=RG",
                environ={},
            )
        self.assertNotIn("api_key=", str(raised.exception))

    def test_canadian_timers_use_local_night_and_shared_service(self) -> None:
        vancouver_timer = (
            REPOSITORY_ROOT / "deploy" / "systemd" / "haltewecker-static-departures.timer"
        ).read_text(encoding="utf-8")
        toronto_timer = (
            REPOSITORY_ROOT / "deploy" / "systemd" / "haltewecker-toronto-static-departures.timer"
        ).read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 03:30:00 America/Vancouver", vancouver_timer)
        self.assertIn("OnCalendar=*-*-* 03:30:00 America/Toronto", toronto_timer)
        self.assertIn("Unit=haltewecker-static-departures.service", vancouver_timer)
        self.assertIn("Unit=haltewecker-static-departures.service", toronto_timer)
        self.assertIn("Persistent=true", vancouver_timer)
        self.assertIn("Persistent=true", toronto_timer)

    def test_toronto_namespaced_sources_merge_without_cross_join(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            (root / "config" / "ttc-cities.json").write_text(
                json.dumps([
                    {
                        "id": "toronto",
                        "name": "Toronto",
                        "aliases": [],
                        "latitude": 43.6532,
                        "longitude": -79.3832,
                        "radiusMeters": 50_000,
                        "packageMode": "external",
                        "externalGTFSProviders": ["ttc-surface", "ttc-subway"],
                    }
                ])
            )
            surface_zip = _gtfs_zip(
                root / "surface.zip",
                stops="stop_id,stop_name,stop_lat,stop_lon\n100,Surface stop,43.6532,-79.3832\n",
                routes="route_id,route_short_name,route_long_name,route_type\n600,506,Surface route,0\n",
                trips="route_id,service_id,trip_id,trip_headsign,direction_id\n600,S1,trip-600,Surface destination,0\n",
                stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\ntrip-600,08:00:00,08:00:00,100,1\n",
            )
            subway_zip = _gtfs_zip(
                root / "subway.zip",
                stops="stop_id,stop_name,stop_lat,stop_lon\n100,Subway stop,43.6532,-79.3832\n",
                routes="route_id,route_short_name,route_long_name,route_type\n600,1,Yonge-University,1\n",
                trips="route_id,service_id,trip_id,trip_headsign,direction_id\n600,S1,trip-600,Subway destination,0\n",
                stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\ntrip-600,08:05:00,08:05:00,100,1\n",
            )
            sources = [
                {
                    "id": "ttc-surface",
                    "url": str(surface_zip),
                    "cities": "config/ttc-cities.json",
                    "timezone": "America/Toronto",
                    "identifierPrefix": "ttc-surface:",
                    "namespace": "ttc-surface:",
                    "mergeGroup": "toronto",
                    "stopIDMode": "exact",
                    "country": "CA",
                },
                {
                    "id": "ttc-subway",
                    "url": str(subway_zip),
                    "cities": "config/ttc-cities.json",
                    "timezone": "America/Toronto",
                    "identifierPrefix": "ttc-subway:",
                    "namespace": "ttc-subway:",
                    "mergeGroup": "toronto",
                    "stopIDMode": "exact",
                    "country": "CA",
                },
            ]
            sources_path = root / "sources.json"
            sources_path.write_text(json.dumps(sources))
            output = root / "out"

            manifest, cities, packages, lines = process_external_gtfs_sources(
                repository_root=root,
                sources_path=sources_path,
                url_by_provider={},
                output=output,
                load_gtfs_archive=load_gtfs_archive,
            )

            self.assertEqual([entry["id"] for entry in manifest], ["toronto"])
            self.assertEqual([city["id"] for city in cities], ["toronto"])
            stop_ids = {stop["id"] for stop in packages["toronto"]}
            self.assertEqual(stop_ids, {"ttc-surface:100", "ttc-subway:100"})
            routes = json.loads((output / "routes" / "toronto.json").read_text())
            self.assertEqual(set(routes), {"ttc-surface:600", "ttc-subway:600"})
            departures = json.loads(
                (output / "departures" / "toronto.json").read_text()
            )
            self.assertEqual(
                set(departures["stops"]),
                {"ttc-surface:100", "ttc-subway:100"},
            )
            self.assertEqual(
                {item["r"] for items in departures["stops"].values() for item in items},
                {"ttc-surface:600", "ttc-subway:600"},
            )
            trips = json.loads((output / "trips" / "toronto.json").read_text())
            self.assertEqual(
                set(trips), {"ttc-surface:trip-600", "ttc-subway:trip-600"}
            )
            self.assertEqual(set(lines), {"ttc-surface:100", "ttc-subway:100"})

    def test_norway_radar_manifest_preserves_multiple_codespaces(self) -> None:
        cities = load_cities(REPOSITORY_ROOT / "config" / "norway-cities.json")
        manifest = transit_radar_manifest(cities)
        oslo = next(city for city in manifest["cities"] if city["appCityID"] == "oslo")
        provider = oslo["providers"][0]
        self.assertEqual(oslo["cityID"], "oslo-no")
        self.assertEqual(provider["providerID"], "entur-oslo")
        self.assertEqual(provider["radarCodespaces"], ["VYG", "FLT", "GOA"])
        self.assertEqual(provider["allowedVehicleModes"], ["RAIL"])
        self.assertIn("liveVehicles", provider["features"])

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
    def test_kyiv_static_package_has_center_stops_and_valid_coordinates(self) -> None:
        source = next(
            source
            for source in load_external_gtfs_sources(
                REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
            )
            if source["id"] == "kyiv"
        )
        cities = load_external_cities(source, REPOSITORY_ROOT)
        self.assertEqual([city["id"] for city in cities], ["kyiv"])
        self.assertEqual(source["timezone"], "Europe/Kyiv")
        self.assertEqual(source["identifierPrefix"], "kyiv:")
        self.assertEqual(
            source["url"],
            "https://data.kyivcity.gov.ua/dataset/rozklad-rukhu-miskoho-elektrychnoho-ta-avtomobilnoho-transportu-dep-transport/resource/58f0c3d0-9409-4de9-92c8-de4afa035efd/data/download",
        )
        self.assertEqual(source["preflight"], "download")
        self.assertFalse(source["allowStale"])
        self.assertEqual(
            external_gtfs_resilience_policy(source),
            {
                "allowStale": True,
                "retryAttempts": 3,
                "metadataProbe": False,
                "requireDataRows": True,
            },
        )
        sweden = next(
            item
            for item in load_external_gtfs_sources(
                REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
            )
            if item["id"] == "sweden"
        )
        self.assertIsNone(external_gtfs_resilience_policy(sweden))
        catalog = source["supplementalStopCatalog"]
        self.assertEqual(catalog["resourceID"], "269ffb2e-dac0-4978-9552-10d29a30724b")
        self.assertEqual(catalog["idPrefix"], "kyiv-catalog:")
        self.assertEqual(catalog["staticDepartureProviderID"], "kyiv")
        request_url, headers = authenticated_external_request("kyiv", source["url"])
        self.assertEqual(request_url, source["url"])
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertEqual(headers["Referer"], "https://data.kyivcity.gov.ua/")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "kyiv.zip"
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type\n"
                    "3_6_1,Khreshchatyk,50.4501,30.5234,,0\n"
                    "3_6_2,Independence Square,50.4488,30.5220,,0\n"
                    "3_6_3,Kontraktova Ploshcha,50.4650,30.5170,,0\n"
                    "outside,Outside Kyiv,51.5000,30.5000,,0\n"
                ),
            )
            with zipfile.ZipFile(archive_path) as archive:
                manifest, package_stops = build_external_stop_packages(
                    archive,
                    cities,
                    root / "out",
                    stop_id_mode="exact",
                )

            center_counts = validate_external_stop_packages(
                cities=cities,
                manifest_entries=manifest,
                package_stops_by_city_id=package_stops,
                output=root / "out",
            )

            self.assertEqual(manifest[0]["id"], "kyiv")
            self.assertEqual(manifest[0]["stopCount"], 3)
            self.assertEqual(center_counts["kyiv"], 3)
            self.assertEqual(
                {stop["id"] for stop in package_stops["kyiv"]},
                {"3_6_1", "3_6_2", "3_6_3"},
            )
            for stop in package_stops["kyiv"]:
                self.assertTrue(-90 <= stop["latitude"] <= 90)
                self.assertTrue(-180 <= stop["longitude"] <= 180)

    def test_kyiv_official_surface_catalog_adds_unrepresented_stops(self) -> None:
        source = next(
            source
            for source in load_external_gtfs_sources(
                REPOSITORY_ROOT / "config" / "external-gtfs-sources.json"
            )
            if source["id"] == "kyiv"
        )
        cities = load_external_cities(source, REPOSITORY_ROOT)
        catalog = [
            {
                "uid": 2477,
                "label": "Вул. Олени Теліги",
                "lat": 50.4776622073,
                "lon": 30.451028891,
                "type": 0,
            },
            {
                "uid": 2493,
                "label": "Парк відпочинку",
                "lat": 50.4795460908,
                "lon": 30.4533223812,
                "type": 0,
            },
            {
                "uid": 2244,
                "label": "АЗС",
                "lat": 50.4821995518,
                "lon": 30.4611564072,
                "type": 0,
            },
            {
                "uid": 9991,
                "label": "Existing GTFS twin",
                "lat": 50.45011,
                "lon": 30.52341,
                "type": 0,
            },
            {
                "uid": 9992,
                "label": "Outside Kyiv",
                "lat": 51.5,
                "lon": 30.5,
                "type": 0,
            },
        ]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "kyiv.zip"
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type\n"
                    "3_6_1,Khreshchatyk,50.4501,30.5234,,0\n"
                ),
            )
            with zipfile.ZipFile(archive_path) as archive:
                manifest, package_stops = build_external_stop_packages(
                    archive,
                    cities,
                    root / "out",
                    stop_id_mode="exact",
                    supplemental_stop_catalog=catalog,
                    supplemental_catalog_configuration=source[
                        "supplementalStopCatalog"
                    ],
                )

        stops = package_stops["kyiv"]
        supplemental = [
            stop for stop in stops if stop["id"].startswith("kyiv-catalog:")
        ]
        self.assertEqual(
            {stop["id"] for stop in supplemental},
            {"kyiv-catalog:2477", "kyiv-catalog:2493", "kyiv-catalog:2244"},
        )
        self.assertEqual(manifest[0]["stopCount"], len(stops))
        self.assertTrue(all(stop["staticDeparturesAvailable"] is False for stop in supplemental))
        self.assertTrue(all(stop["staticDepartureProviderID"] == "kyiv" for stop in supplemental))
        self.assertTrue(all(-90 <= stop["latitude"] <= 90 for stop in stops))
        self.assertTrue(all(-180 <= stop["longitude"] <= 180 for stop in stops))

    def test_passenger_stop_mode_keeps_real_stops_and_required_stations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "cta.zip"
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_name,stop_lat,stop_lon,parent_station,location_type\n"
                    "station,Station,41.88,-87.63,,1\n"
                    "platform,Platform,41.88,-87.63,station,0\n"
                    "street,Street Stop,41.881,-87.631,,0\n"
                    "unused-station,Unused Station,41.882,-87.632,,1\n"
                    "entrance,Entrance,41.88,-87.63,station,2\n"
                    "node,Node,41.88,-87.63,station,3\n"
                    "boarding-area,Boarding Area,41.88,-87.63,station,4\n"
                ),
                stop_times=(
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "T1,08:00:00,08:00:00,platform,1\n"
                    "T1,08:10:00,08:10:00,street,2\n"
                ),
            )
            city = {
                "id": "chicago",
                "name": "Chicago",
                "aliases": [],
                "latitude": 41.8781,
                "longitude": -87.6298,
                "radiusMeters": 55_000,
                "packageMode": "external",
            }
            with zipfile.ZipFile(archive_path) as archive:
                _manifest, package_stops = build_external_stop_packages(
                    archive,
                    [city],
                    root / "out",
                    stop_id_mode="exact",
                    publish_passenger_stop_ids=True,
                )

            self.assertEqual(
                {stop["id"] for stop in package_stops["chicago"]},
                {"station", "platform", "street"},
            )

    def test_translink_native_stop_codes_and_departure_sequence_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "translink.zip"
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
                    "75,50075,Northbound Burrard St @ Davie St,49.2827,-123.1207\n"
                    "11535,61519,Northbound Seymour St @ Dunsmuir St,49.2828,-123.1208\n"
                ),
                routes=(
                    "route_id,route_short_name,route_long_name,route_type\n"
                    "6612,002,Macdonald,3\n"
                ),
                trips=(
                    "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                    "6612,S1,15210220,2 Macdonald/To Burrard Station,0\n"
                ),
                stop_times=(
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "15210220,08:00:00,08:00:00,75,15\n"
                ),
            )
            city = {
                "id": "vancouver",
                "name": "Vancouver",
                "aliases": ["Metro Vancouver"],
                "latitude": 49.2827,
                "longitude": -123.1207,
                "radiusMeters": 55000,
                "packageMode": "external",
            }
            with zipfile.ZipFile(archive_path) as archive:
                _, package_stops = build_external_stop_packages(
                    archive, [city], root / "out", stop_id_mode="exact"
                )
                build_external_route_index(archive, [city], root / "out")
                build_external_departure_index(
                    archive, [city], root / "out", "America/Vancouver"
                )

            stops = {item["id"]: item for item in package_stops["vancouver"]}
            self.assertEqual(stops["75"]["stopCode"], "50075")
            self.assertEqual(stops["11535"]["stopCode"], "61519")
            routes = json.loads((root / "out/routes/vancouver.json").read_text())
            self.assertEqual(routes["6612"]["short_name"], "002")
            self.assertEqual(
                routes["6612"]["headsigns"]["0"],
                "2 Macdonald/To Burrard Station",
            )
            departures = json.loads(
                (root / "out/departures/vancouver.json").read_text()
            )
            departure = departures["stops"]["75"][0]
            self.assertEqual(departure["t"], "15210220")
            self.assertEqual(departure["r"], "6612")
            self.assertEqual(departure["q"], "15")

    def test_kyiv_empty_headsign_uses_terminal_stop_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "kyiv.zip"
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "3_6_1,Khreshchatyk,50.4501,30.5234\n"
                    "3_6_2,Kontraktova Ploshcha,50.4650,30.5170\n"
                ),
                routes="route_id,route_short_name,route_long_name,route_type\n3_6,101,Kyiv route 101,3\n",
                trips="route_id,service_id,trip_id,trip_headsign,direction_id\n3_6,S1,trip-101,,0\n",
                stop_times=(
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "trip-101,08:00:00,08:00:00,3_6_1,1\n"
                    "trip-101,08:10:00,08:10:00,3_6_2,2\n"
                ),
            )
            city = {
                "id": "kyiv",
                "name": "Kyiv",
                "aliases": ["Kiev"],
                "latitude": 50.4501,
                "longitude": 30.5234,
                "radiusMeters": 55_000,
                "packageMode": "external",
            }
            with zipfile.ZipFile(archive_path) as archive:
                build_external_stop_packages(archive, [city], root / "out")
                build_external_route_index(archive, [city], root / "out")
                build_external_departure_index(
                    archive, [city], root / "out", "Europe/Kyiv"
                )

            routes = json.loads((root / "out/routes/kyiv.json").read_text())
            departures = json.loads((root / "out/departures/kyiv.json").read_text())
            self.assertEqual(routes["3_6"]["short_name"], "101")
            self.assertEqual(routes["3_6"]["headsigns"], {})
            self.assertEqual(departures["stops"]["3_6_1"][0]["h"], "Kontraktova Ploshcha")

    def test_ireland_verified_trip_route_stop_join_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "ireland.zip"
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "8460B5550401,Galway Ceannt,53.27395,-9.0474\n"
                ),
                routes=(
                    "route_id,route_short_name,route_long_name,route_type\n"
                    "2 51 c b,51,Cork - Limerick - Galway,3\n"
                ),
                trips=(
                    "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                    "2 51 c b,162,5789_34702,Galway,0\n"
                ),
                stop_times=(
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "5789_34702,19:45:00,19:45:00,8460B5550401,24\n"
                ),
                calendar=(
                    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                    "start_date,end_date\n"
                    "162,1,1,1,1,1,1,1,20200101,20301231\n"
                ),
            )
            city = {
                "id": "galway",
                "name": "Galway",
                "aliases": [],
                "latitude": 53.2707,
                "longitude": -9.0568,
                "radiusMeters": 15000,
                "packageMode": "external",
            }
            with zipfile.ZipFile(archive_path) as archive:
                build_external_stop_packages(archive, [city], root / "out")
                build_external_route_index(archive, [city], root / "out")
                build_external_departure_index(
                    archive, [city], root / "out", "Europe/Dublin"
                )

            routes = json.loads((root / "out/routes/galway.json").read_text())
            departures = json.loads((root / "out/departures/galway.json").read_text())
            self.assertEqual(routes["2 51 c b"]["short_name"], "51")
            self.assertEqual(routes["2 51 c b"]["headsigns"]["0"], "Galway")
            departure = departures["stops"]["8460B5550401"][0]
            self.assertEqual(departure["t"], "5789_34702")
            self.assertEqual(departure["r"], "2 51 c b")
            self.assertEqual(departure["h"], "Galway")
            self.assertEqual(departure["p"], "19:45:00")

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

    def test_trip_index_maps_operator_namespaces_to_routes_and_headsigns(self) -> None:
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
                ),
                trips=(
                    "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                    "R1,S1,1401000012345678,Real Headsign,0\n"
                    "R1,S1,1401000099999999,,0\n"
                    "R1,S1,7611000012345678,Kronoberg Headsign,0\n"
                    "R1,S1,1211000012345678,Skane Headsign,1\n"
                    "R1,S1,1410000012345678,Vasttrafik Headsign,1\n"
                    "OTHER,S1,NON_REALTIME_ID,,0\n"
                ),
                stop_times=(
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "1401000012345678,08:00:00,08:00:00,9022001000001001,1\n"
                    "1401000012345678,08:10:00,08:10:00,9022001000002002,2\n"
                    "1401000099999999,09:00:00,09:00:00,9022001000001001,1\n"
                    "1401000099999999,09:12:00,09:12:00,9022001000002002,2\n"
                    "7611000012345678,10:00:00,10:00:00,9022001000001001,1\n"
                    "7611000012345678,10:12:00,10:12:00,9022001000002002,2\n"
                    "1211000012345678,11:00:00,11:00:00,9022001000001001,1\n"
                    "1211000012345678,11:12:00,11:12:00,9022001000002002,2\n"
                    "1410000012345678,12:00:00,12:00:00,9022001000001001,1\n"
                    "1410000012345678,12:12:00,12:12:00,9022001000002002,2\n"
                    "NON_REALTIME_ID,10:00:00,10:00:00,9022001000001001,1\n"
                    "NON_REALTIME_ID,10:12:00,10:12:00,9022001000002002,2\n"
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
                build_external_route_index(archive, cities, out)
                build_external_trip_index(archive, cities, out)

            trip_index = json.loads((out / "trips" / "stockholm.json").read_text())
            # Operator namespaces resolve to their route without a prefix allowlist.
            self.assertEqual(trip_index["1401000012345678"]["r"], "R1")
            self.assertEqual(trip_index["1401000099999999"]["r"], "R1")
            self.assertEqual(trip_index["7611000012345678"]["r"], "R1")
            self.assertEqual(trip_index["1211000012345678"]["r"], "R1")
            self.assertEqual(trip_index["1410000012345678"]["r"], "R1")
            # headsign from trips.txt, not the terminal-stop fallback
            self.assertEqual(trip_index["1401000012345678"]["h"], "Real Headsign")
            self.assertEqual(trip_index["7611000012345678"]["h"], "Kronoberg Headsign")
            self.assertEqual(trip_index["1211000012345678"]["h"], "Skane Headsign")
            self.assertEqual(trip_index["1410000012345678"]["h"], "Vasttrafik Headsign")
            # Trips whose route is not present in routes.txt are excluded.
            self.assertNotIn("NON_REALTIME_ID", trip_index)

    def test_sweden_appears_once_with_production_static_urls_in_manifests(self) -> None:
        cities = load_cities(REPOSITORY_ROOT / "config" / "sweden-cities.json")
        radar = transit_radar_manifest(cities, skip_auto_radar_stops=True)
        app_ids = [city["appCityID"] for city in radar["cities"]]
        expected_ids = {
            "stockholm", "malmo", "goteborg", "uppsala", "vaxjo",
            "helsingborg", "linkoping", "jonkoping", "orebro", "vasteras",
        }
        self.assertEqual(set(app_ids), expected_ids)
        self.assertEqual(len(app_ids), len(expected_ids))
        stockholm = next(city for city in radar["cities"] if city["appCityID"] == "stockholm")
        self.assertEqual(stockholm["cityID"], "stockholm-se")
        for city in radar["cities"]:
            provider = city["providers"][0]
            self.assertEqual(provider["adapter"], "sweden")
            self.assertEqual(provider["providerID"], f"sweden-{city['appCityID']}")
            self.assertEqual(provider["staticBaseURL"], "https://api.asoftlabs.app")
            self.assertEqual(
                provider["boardURL"],
                "https://api.asoftlabs.app/static-departures",
            )
            self.assertIn("region", provider)
            self.assertNotIn("gatewayURL", provider)

        manifest: list[dict[str, object]] = []
        sources: dict[str, str] = {}
        merge_manifest_entries(
            manifest,
            [{"id": "stockholm", "name": "Stockholm"}],
            source="External GTFS source sweden",
            sources_by_city_id=sources,
        )
        self.assertEqual([entry["id"] for entry in manifest].count("stockholm"), 1)

    def test_sweden_registry_builds_stops_routes_and_departures_for_every_city(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            city_config = json.loads(
                (REPOSITORY_ROOT / "config" / "sweden-cities.json").read_text(
                    encoding="utf-8"
                )
            )
            (root / "config" / "sweden-cities.json").write_text(
                json.dumps(city_config),
                encoding="utf-8",
            )
            archive_path = root / "sweden.zip"
            stop_rows = ["stop_id,stop_name,stop_lat,stop_lon,parent_station"]
            stop_time_rows = [
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence"
            ]
            for sequence, city in enumerate(city_config, start=1):
                stop_id = f"SE-{city['id']}"
                stop_rows.append(
                    f"{stop_id},{city['name']},{city['latitude']},{city['longitude']},"
                )
                departure = f"08:{sequence:02d}:00"
                stop_time_rows.append(
                    f"1401000000000001,{departure},{departure},{stop_id},{sequence}"
                )
            _gtfs_zip(
                archive_path,
                stops="\n".join(stop_rows) + "\n",
                trips=(
                    "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                    "R1,S1,1401000000000001,Sweden,0\n"
                ),
                stop_times="\n".join(stop_time_rows) + "\n",
            )
            (root / "config" / "external-gtfs-sources.json").write_text(
                json.dumps([{
                    "id": "sweden",
                    "cities": "config/sweden-cities.json",
                    "timezone": "Europe/Stockholm",
                    "identifierPrefix": "se:",
                    "stopIDMode": "exact",
                    "country": "SE",
                    "buildStops": True,
                    "buildRoutes": True,
                    "buildDepartures": True,
                    "buildTripIndex": True,
                }]),
                encoding="utf-8",
            )

            output = root / "out"
            entries, external_cities, package_stops, _ = process_external_gtfs_sources(
                repository_root=root,
                sources_path=root / "config" / "external-gtfs-sources.json",
                url_by_provider={"sweden": str(archive_path)},
                output=output,
                load_gtfs_archive=load_gtfs_archive,
            )

            expected_ids = [city["id"] for city in city_config]
            self.assertEqual([entry["id"] for entry in entries], expected_ids)
            self.assertEqual(
                [city["id"] for city in external_cities],
                expected_ids,
            )
            for city_id in expected_ids:
                self.assertTrue((output / "stops" / f"{city_id}.json").is_file())
                self.assertTrue((output / "routes" / f"{city_id}.json").is_file())
                self.assertTrue(
                    (output / "departures" / f"{city_id}.json").is_file()
                )
                self.assertTrue(package_stops[city_id])

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

    def test_end_to_end_norway_source_builds_static_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            archive_path = root / "norway.zip"
            _gtfs_zip(
                archive_path,
                stops=(
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    "NSR:StopPlace:58366,Jernbanetorget,59.9119,10.75038\n"
                    "NSR:StopPlace:59872,Oslo S,59.9098,10.7528\n"
                ),
                stop_times=(
                    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                    "T1,08:00:00,08:00:00,NSR:StopPlace:58366,1\n"
                    "T1,08:10:00,08:10:00,NSR:StopPlace:59872,2\n"
                ),
            )
            (root / "config" / "norway-cities.json").write_text(json.dumps([{
                "id": "oslo",
                "name": "Oslo",
                "aliases": [],
                "latitude": 59.9139,
                "longitude": 10.7522,
                "radiusMeters": 30_000,
                "packageMode": "external",
                "externalGTFSProvider": "norway",
                "transitRadar": {
                    "adapter": "entur",
                    "radarCodespaces": ["VYG", "FLT", "GOA"],
                    "isEnabled": True,
                    "features": [
                        "liveVehicles",
                        "realtimeDepartures",
                        "firstDepartures",
                        "stopLookup",
                        "realtimeDelay",
                    ],
                    "region": {
                        "minimumLongitude": 10.25,
                        "minimumLatitude": 59.65,
                        "maximumLongitude": 11.25,
                        "maximumLatitude": 60.15,
                    },
                },
            }]))
            (root / "config" / "external-gtfs-sources.json").write_text(json.dumps([{
                "id": "norway",
                "url": str(archive_path),
                "cities": "config/norway-cities.json",
                "timezone": "Europe/Oslo",
                "identifierPrefix": "no:",
                "stopIDMode": "exact",
                "country": "NO",
                "buildStops": True,
                "buildRoutes": True,
                "buildDepartures": True,
                "buildTripIndex": False,
            }]))

            out = root / "out"
            entries, external_cities, package_stops, lines = process_external_gtfs_sources(
                repository_root=root,
                sources_path=root / "config" / "external-gtfs-sources.json",
                url_by_provider={},
                output=out,
                load_gtfs_archive=load_gtfs_archive,
            )

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["id"], "oslo")
            self.assertEqual(entries[0]["country"], "NO")
            self.assertEqual([city["id"] for city in external_cities], ["oslo"])
            self.assertEqual(
                {stop["id"] for stop in package_stops["oslo"]},
                {"NSR:StopPlace:58366", "NSR:StopPlace:59872"},
            )
            self.assertTrue((out / "stops" / "oslo.json").exists())
            self.assertTrue((out / "routes" / "oslo.json").exists())
            self.assertTrue((out / "departures" / "oslo.json").exists())
            self.assertIn("NSR:StopPlace:58366", lines)

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
                def __init__(self, body):
                    self.body = body

                def read(self):
                    body, self.body = self.body, b""
                    return body
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            with mock.patch(
                "build_stop_packages.urllib.request.urlopen",
                return_value=FakeResponse(compressed),
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


class ExternalMembershipIntegrationTests(unittest.TestCase):
    def test_external_membership_stage_uses_indexed_path(self) -> None:
        source = {
            "id": "fixture-provider",
            "cities": "fixture-cities.json",
            "identifierPrefix": "fixture:",
            "namespace": "fixture:",
            "staticIdentifierPrefix": "fixture:",
            "staticStopIDPrefix": "fixture:",
            "timezone": "UTC",
        }
        city = {
            "id": "fixture-city",
            "packageMode": "external",
            "externalGTFSProvider": "fixture-provider",
        }
        connection = mock.Mock()
        connection.execute.return_value = []
        archive = mock.Mock()
        with (
            mock.patch.object(static_importer, "load_external_gtfs_sources", return_value=[source]),
            mock.patch.object(static_importer, "validate_external_gtfs_source"),
            mock.patch.object(static_importer, "load_external_cities", return_value=[city]),
            mock.patch.object(
                static_importer,
                "authenticated_external_request",
                return_value=("fixture.gtfs.zip", {}),
            ),
            mock.patch.object(static_importer, "load_gtfs_archive", return_value=archive),
            mock.patch.object(static_importer, "agency_scoped_archive", return_value=archive),
            mock.patch.object(static_importer, "populate_gtfs"),
            mock.patch.object(static_importer, "resolve_canonical_stops"),
            mock.patch.object(static_importer, "populate_provider_city_memberships", return_value={"fixture-city"}) as memberships,
            mock.patch.object(static_importer, "populate_active_services"),
            mock.patch.object(static_importer, "update_terminal_stops"),
            mock.patch.object(static_importer, "timed_stage", wraps=static_importer.timed_stage) as timed,
        ):
            result = static_importer.add_external_gtfs(
                connection,
                Path("/tmp/fixture-stop-data"),
                {"fixture-provider": "fixture.gtfs.zip"},
                repository_root=Path("/tmp"),
                sources_path=Path("/tmp/fixture-sources.json"),
                dates=[date(2026, 8, 14)],
            )

        self.assertEqual(result, {"fixture-city"})
        self.assertTrue(memberships.call_args.kwargs["indexed_ownership_lookup"])
        stage_names = {(call.args[0], call.args[1]) for call in timed.call_args_list}
        self.assertIn(("external", "provider-city-memberships"), stage_names)

if __name__ == "__main__":
    unittest.main()
