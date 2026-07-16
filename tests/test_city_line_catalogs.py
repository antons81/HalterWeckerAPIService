#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import build_city_line_catalogs, build_lines_by_stop_id


class CityLineCatalogTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
