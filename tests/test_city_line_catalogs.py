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
    def test_vag_provider_is_enabled_only_when_gateway_is_configured(self) -> None:
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
        self.assertTrue(enabled_provider["isEnabled"])
        self.assertEqual(
            enabled_provider["gatewayURL"],
            "https://api.example.com"
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
