#!/usr/bin/env python3

import json
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import (
    build_city_line_catalogs,
    build_lines_by_stop_id,
    build_rnv_assets,
    build_stop_packages,
    transit_radar_manifest
)


class CityLineCatalogTests(unittest.TestCase):
    def test_configured_city_keeps_legacy_municipality_package_url(self) -> None:
        municipalities = [municipality(
            code="09564000",
            name="Nürnberg",
            minimum_longitude=10.98,
            minimum_latitude=49.33,
            maximum_longitude=11.28,
            maximum_latitude=49.59
        )]
        cities = [{
            "id": "nurnberg",
            "name": "Nürnberg",
            "aliases": ["Nuernberg"],
            "latitude": 49.4521,
            "longitude": 11.0767,
            "radiusMeters": 18_000
        }]

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            manifest, _, _ = build_stop_packages(
                stops=[stop("nuernberg-stop", "Nürnberg Hbf", 49.446, 11.082)],
                cities=cities,
                municipalities=municipalities,
                output=output
            )
            current = json.loads(
                (output / "stops" / "nurnberg.json").read_text(encoding="utf-8")
            )
            legacy = json.loads(
                (output / "stops" / "nurnberg-09564000.json")
                .read_text(encoding="utf-8")
            )
            transliterated_legacy = json.loads(
                (output / "stops" / "nuernberg-09564000.json")
                .read_text(encoding="utf-8")
            )

        self.assertEqual(manifest[0]["id"], "nurnberg")
        self.assertEqual(current, legacy)
        self.assertEqual(current, transliterated_legacy)

    def test_provider_enabled_state_comes_only_from_city_configuration(self) -> None:
        cities = [{
            "id": "nurnberg",
            "name": "Nürnberg",
            "latitude": 49.4521,
            "longitude": 11.0767,
            "transitRadar": {
                "adapter": "vagPuls",
                "isEnabled": False,
                "region": {
                    "minimumLongitude": 10.98,
                    "minimumLatitude": 49.33,
                    "maximumLongitude": 11.28,
                    "maximumLatitude": 49.59
                }
            }
        }]

        disabled = transit_radar_manifest(cities)
        enabled = transit_radar_manifest(
            cities,
            vag_gateway_url="https://api.example.com"
        )

        disabled_provider = disabled["cities"][0]["providers"][0]
        enabled_provider = enabled["cities"][0]["providers"][0]
        self.assertFalse(disabled_provider["isEnabled"])
        self.assertNotIn("gatewayURL", disabled_provider)
        self.assertFalse(enabled_provider["isEnabled"])
        self.assertEqual(
            enabled_provider["gatewayURL"],
            "https://api.example.com"
        )

    def test_vvo_radar_and_departures_are_available_without_gateway(self) -> None:
        cities = [{
            "id": "dresden",
            "name": "Dresden",
            "latitude": 51.0504,
            "longitude": 13.7373,
            "transitRadar": {
                "adapter": "vvo",
                "efaPath": "vvo",
                "isEnabled": True,
                "supportsLiveVehicles": True,
                "supportsRealtimeDelay": True,
                "region": {
                    "minimumLongitude": 13.5,
                    "minimumLatitude": 50.8,
                    "maximumLongitude": 14.0,
                    "maximumLatitude": 51.3
                }
            }
        }]

        manifest = transit_radar_manifest(cities)

        expected_features = [
            "liveVehicles",
            "realtimeDepartures",
            "firstDepartures",
            "stopLookup",
            "realtimeDelay"
        ]
        self.assertEqual(
            manifest["cities"][0]["providers"][0]["features"],
            expected_features
        )
        self.assertNotIn(
            "gatewayURL",
            manifest["cities"][0]["providers"][0]
        )

    def test_vrr_efa_can_publish_schedule_radar_without_gateway(self) -> None:
        cities = [{
            "id": "hagen-05914000",
            "name": "Hagen",
            "latitude": 51.3671,
            "longitude": 7.4633,
            "transitRadar": {
                "adapter": "vrrEFA",
                "efaPath": "static03",
                "supportsLiveVehicles": True,
                "radarStops": [{
                    "id": "20002007",
                    "latitude": 51.361965,
                    "longitude": 7.461802
                }],
                "region": {
                    "minimumLongitude": 7.30,
                    "minimumLatitude": 51.25,
                    "maximumLongitude": 7.65,
                    "maximumLatitude": 51.50
                }
            }
        }]

        provider = transit_radar_manifest(cities)["cities"][0]["providers"][0]

        self.assertEqual(
            provider["features"],
            [
                "liveVehicles",
                "realtimeDepartures",
                "firstDepartures",
                "stopLookup",
                "realtimeDelay"
            ]
        )
        self.assertNotIn("gatewayURL", provider)
        self.assertEqual(provider["region"]["minimumLongitude"], 7.30)

    def test_vrr_efa_can_build_schedule_radar_stops_from_city_package(self) -> None:
        cities = [{
            "id": "bochum",
            "name": "Bochum",
            "latitude": 51.4818,
            "longitude": 7.2162,
            "radiusMeters": 18_000,
            "transitRadar": {
                "adapter": "vrrEFA",
                "efaPath": "static03",
                "supportsDepartures": True,
                "supportsLiveVehicles": True,
                "autoRadarStops": True
            }
        }]
        city_stops = [
            stop("central", "Bochum Hbf", 51.4789, 7.2220),
            stop("west", "Bochum West", 51.4800, 7.1500),
            stop("east", "Bochum Ost", 51.4850, 7.2800)
        ]
        lines_by_stop_id = {
            "central": {
                "line-1": {"routeID": "line-1"},
                "line-2": {"routeID": "line-2"}
            }
        }

        provider = transit_radar_manifest(
            cities,
            package_stops_by_city_id={"bochum": city_stops},
            lines_by_stop_id=lines_by_stop_id
        )["cities"][0]["providers"][0]

        self.assertEqual(provider["radarStops"][0]["id"], "central")
        self.assertEqual(len(provider["radarStops"]), 3)
        self.assertLess(provider["region"]["minimumLatitude"], 51.4818)
        self.assertGreater(provider["region"]["maximumLatitude"], 51.4818)
        self.assertEqual(
            provider["features"],
            [
                "liveVehicles",
                "realtimeDepartures",
                "firstDepartures",
                "stopLookup",
                "realtimeDelay"
            ]
        )

    def test_automatic_efa_radar_requires_generated_stop_package(self) -> None:
        cities = [{
            "id": "bochum",
            "name": "Bochum",
            "latitude": 51.4818,
            "longitude": 7.2162,
            "radiusMeters": 18_000,
            "transitRadar": {
                "adapter": "vrrEFA",
                "efaPath": "static03",
                "supportsLiveVehicles": True,
                "autoRadarStops": True
            }
        }]

        with self.assertRaisesRegex(ValueError, "Missing stop packages"):
            transit_radar_manifest(cities)

    def test_regional_efa_adapters_publish_full_feature_set_without_gateway(self) -> None:
        providers = {}
        for adapter, path, city_id in (
            ("kvvEFA", "sl3-alone", "karlsruhe-08212000"),
            ("hvvEFA", "efa", "hamburg"),
            ("vvsEFA", "mngvvs", "stuttgart")
        ):
            cities = [{
                "id": city_id,
                "name": city_id,
                "latitude": 49.0,
                "longitude": 9.0,
                "transitRadar": {
                    "adapter": adapter,
                    "efaPath": path,
                    "supportsLiveVehicles": True,
                    "radarStops": [{
                        "id": "central-stop",
                        "latitude": 49.0,
                        "longitude": 9.0
                    }],
                    "region": {
                        "minimumLongitude": 8.8,
                        "minimumLatitude": 48.8,
                        "maximumLongitude": 9.2,
                        "maximumLatitude": 49.2
                    }
                }
            }]
            providers[adapter] = transit_radar_manifest(cities)["cities"][0]["providers"][0]

        for provider in providers.values():
            self.assertEqual(
                provider["features"],
                [
                    "liveVehicles",
                    "realtimeDepartures",
                    "firstDepartures",
                    "stopLookup",
                    "realtimeDelay"
                ]
            )
            self.assertNotIn("gatewayURL", provider)
        self.assertEqual(
            providers["kvvEFA"]["providerID"],
            "kvv-efa-karlsruhe-08212000"
        )
        self.assertEqual(
            providers["kvvEFA"]["radarStops"][0]["id"],
            "central-stop"
        )

    def test_live_efa_provider_requires_valid_radar_stops(self) -> None:
        city = {
            "id": "karlsruhe-08212000",
            "name": "Karlsruhe",
            "latitude": 49.0,
            "longitude": 9.0,
            "transitRadar": {
                "adapter": "kvvEFA",
                "efaPath": "sl3-alone",
                "supportsLiveVehicles": True,
                "region": {
                    "minimumLongitude": 8.8,
                    "minimumLatitude": 48.8,
                    "maximumLongitude": 9.2,
                    "maximumLatitude": 49.2
                }
            }
        }

        with self.assertRaisesRegex(ValueError, "Invalid EFA radar stops"):
            transit_radar_manifest([city])

    def test_static_provider_is_published_without_adapter(self) -> None:
        cities = [{
            "id": "wuppertal",
            "name": "Wuppertal",
            "latitude": 51.2562,
            "longitude": 7.1508,
            "transitRadar": {
                "providerID": "wsw-wuppertal",
                "features": ["liveVehicles", "realtimeDelay"]
            }
        }]

        provider = transit_radar_manifest(cities)["cities"][0]["providers"][0]

        self.assertEqual(provider["providerID"], "wsw-wuppertal")
        self.assertNotIn("adapter", provider)
        self.assertEqual(provider["features"], ["liveVehicles", "realtimeDelay"])

    def test_dusseldorf_vrr_provider_is_live_only(self) -> None:
        cities = [{
            "id": "dusseldorf",
            "name": "Düsseldorf",
            "latitude": 51.2277,
            "longitude": 6.7735,
            "transitRadar": {
                "adapter": "vrrEFA",
                "efaPath": "static03",
                "supportsDepartures": False,
                "supportsLiveVehicles": True,
                "radarStops": [{
                    "id": "20018235",
                    "latitude": 51.220253,
                    "longitude": 6.792997
                }],
                "region": {
                    "minimumLongitude": 6.60,
                    "minimumLatitude": 51.10,
                    "maximumLongitude": 6.95,
                    "maximumLatitude": 51.35
                }
            }
        }]

        provider = transit_radar_manifest(cities)["cities"][0]["providers"][0]

        self.assertEqual(provider["features"], ["liveVehicles", "realtimeDelay"])

    def test_vbb_departure_features_are_enabled_explicitly(self) -> None:
        cities = [{
            "id": "berlin",
            "name": "Berlin",
            "latitude": 52.52,
            "longitude": 13.405,
            "transitRadar": {
                "adapter": "vbb",
                "isEnabled": True,
                "supportsDepartures": True,
                "region": {
                    "minimumLongitude": 13.08,
                    "minimumLatitude": 52.33,
                    "maximumLongitude": 13.77,
                    "maximumLatitude": 52.68
                }
            }
        }]

        provider = transit_radar_manifest(cities)["cities"][0]["providers"][0]

        self.assertEqual(provider["providerID"], "vbb-berlin")
        self.assertEqual(
            provider["features"],
            [
                "liveVehicles",
                "realtimeDepartures",
                "firstDepartures",
                "stopLookup",
                "realtimeDelay"
            ]
        )

    def test_rnv_assets_cover_every_municipality_with_an_rnv_stop(self) -> None:
        archive_data = io.BytesIO()
        with zipfile.ZipFile(archive_data, "w") as archive:
            archive.writestr(
                "stops.txt",
                "stop_id,stop_name,stop_lat,stop_lon\n"
                "ma-stop,Mannheim Paradeplatz,49.4875,8.4660\n"
            )
            archive.writestr(
                "routes.txt",
                "route_id,route_short_name,route_long_name,route_type\n"
                "route-5,5,Weinheim - Mannheim,0\n"
            )
            archive.writestr(
                "trips.txt",
                "route_id,service_id,trip_id,trip_headsign\n"
                "route-5,weekday,trip-5,Weinheim\n"
            )
        archive_data.seek(0)

        with tempfile.TemporaryDirectory() as temporary_directory:
            availability, city_ids = build_rnv_assets(
                archive=zipfile.ZipFile(archive_data),
                output=Path(temporary_directory),
                manifest=[{
                    "id": "mannheim-08222000",
                    "name": "Mannheim",
                    "aliases": []
                }],
                cities=[],
                municipalities=[municipality(
                    code="08222000",
                    name="Mannheim",
                    minimum_longitude=8.3,
                    minimum_latitude=49.3,
                    maximum_longitude=8.7,
                    maximum_latitude=49.7
                )],
                gateway_url=""
            )
            network = json.loads(
                (Path(temporary_directory) / "transit" / "rnv" / "network.json")
                .read_text(encoding="utf-8")
            )

        self.assertEqual(city_ids, {"mannheim-08222000"})
        self.assertEqual(availability[0]["appCityID"], "mannheim-08222000")
        self.assertFalse(availability[0]["providers"][0]["isEnabled"])
        self.assertNotIn("gatewayURL", availability[0]["providers"][0])
        self.assertEqual(network["routes"][0]["shortName"], "5")
        self.assertEqual(network["trips"][0]["routeID"], "route-5")

    def test_configured_city_line_scope_uses_municipality_not_search_radius(self) -> None:
        municipalities = [
            municipality(
                code="velbert-code",
                name="Velbert",
                minimum_longitude=7.00,
                minimum_latitude=51.30,
                maximum_longitude=7.10,
                maximum_latitude=51.40
            ),
            municipality(
                code="essen-code",
                name="Essen",
                minimum_longitude=6.90,
                minimum_latitude=51.40,
                maximum_longitude=7.00,
                maximum_latitude=51.50
            )
        ]
        stops = [
            stop("velbert-stop", "Velbert ZOB", 51.35, 7.05),
            stop("essen-stop", "Essen Werden", 51.45, 6.95)
        ]
        cities = [{
            "id": "velbert",
            "name": "Velbert",
            "aliases": [],
            "latitude": 51.35,
            "longitude": 7.05,
            "radiusMeters": 30_000,
            "transitRadar": {"adapter": "dbRegioBusNRW"}
        }]

        with tempfile.TemporaryDirectory() as temporary_directory:
            _, _, package_stops = build_stop_packages(
                stops=stops,
                cities=cities,
                municipalities=municipalities,
                output=Path(temporary_directory)
            )

        self.assertEqual(
            [item["id"] for item in package_stops["velbert"]],
            ["velbert-stop"]
        )

    def test_builds_catalog_from_routes_serving_city_stops(self) -> None:
        stop_rows = [
            {"stop_id": "velbert", "stop_name": "Velbert ZOB"},
            {
                "stop_id": "velbert-platform",
                "parent_station": "velbert",
                "stop_name": "Velbert ZOB"
            },
            {"stop_id": "essen", "stop_name": "Essen Hbf"}
        ]
        stop_times = [
            {"trip_id": "trip-169", "stop_id": "velbert-platform"},
            {"trip_id": "trip-169", "stop_id": "essen"},
            {"trip_id": "trip-160", "stop_id": "essen"}
        ]
        trips = [
            {"trip_id": "trip-169", "route_id": "route-169"},
            {"trip_id": "trip-160", "route_id": "route-160"}
        ]
        routes = [
            {
                "route_id": "route-169",
                "agency_id": "ruhrbahn",
                "route_short_name": "169",
                "route_long_name": "Essen - Velbert",
                "route_type": "3"
            },
            {
                "route_id": "route-160",
                "agency_id": "ruhrbahn",
                "route_short_name": "160",
                "route_type": "3"
            }
        ]

        lines_by_stop_id = build_lines_by_stop_id(
            stop_rows,
            stop_times,
            trips,
            routes
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            build_city_line_catalogs(
                output=output,
                manifest=[
                    {
                        "id": "velbert",
                        "name": "Velbert",
                        "aliases": []
                    }
                ],
                package_stops_by_city_id={
                    "velbert": [{"id": "velbert"}]
                },
                lines_by_stop_id=lines_by_stop_id,
                cities=[
                    {
                        "id": "velbert",
                        "name": "Velbert",
                        "aliases": [],
                        "transitRadar": {"adapter": "dbRegioBusNRW"}
                    }
                ]
            )

            payload = json.loads(
                (output / "transit" / "city-lines" / "velbert.json")
                .read_text(encoding="utf-8")
            )

        self.assertEqual(payload["cityID"], "velbert")
        self.assertEqual([line["names"][0] for line in payload["lines"]], ["169"])


def stop(stop_id: str, name: str, latitude: float, longitude: float) -> dict:
    return {
        "id": stop_id,
        "name": name,
        "latitude": latitude,
        "longitude": longitude,
        "searchName": name.lower()
    }


def municipality(
    code: str,
    name: str,
    minimum_longitude: float,
    minimum_latitude: float,
    maximum_longitude: float,
    maximum_latitude: float
) -> dict:
    ring = [
        [minimum_longitude, minimum_latitude],
        [maximum_longitude, minimum_latitude],
        [maximum_longitude, maximum_latitude],
        [minimum_longitude, maximum_latitude],
        [minimum_longitude, minimum_latitude]
    ]
    return {
        "code": code,
        "name": name,
        "state": "05",
        "bbox": [
            minimum_longitude,
            minimum_latitude,
            maximum_longitude,
            maximum_latitude
        ],
        "geometry": {"type": "Polygon", "coordinates": [ring]}
    }


if __name__ == "__main__":
    unittest.main()
