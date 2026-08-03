import csv
import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_stop_packages import (  # noqa: E402
    build_nl_departure_index,
    build_nl_route_index,
    build_nl_stop_packages,
    load_cities,
)


class NetherlandsCityCoverageTests(unittest.TestCase):
    def test_venlo_is_unique_and_builds_stop_routes_and_static_departures(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        cities = load_cities(repo / "config" / "cities.json")
        venlo = [city for city in cities if city["id"] == "venlo"]
        self.assertEqual(len(venlo), 1)
        self.assertEqual(venlo[0]["transitRadar"]["adapter"], "netherlands")
        self.assertEqual(
            {
                key
                for key, value in venlo[0]["transitRadar"].items()
                if key.startswith("supports") and value
            },
            {"supportsDepartures", "supportsLiveVehicles", "supportsRealtimeDelay"},
        )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "out"
            archive_path = root / "netherlands.zip"
            self._write_fixture(archive_path, cities)

            with zipfile.ZipFile(archive_path) as archive:
                manifest = build_nl_stop_packages(archive, cities, output)
                build_nl_route_index(archive, cities, output)
                build_nl_departure_index(archive, cities, output)

            venlo_manifest = [entry for entry in manifest if entry["id"] == "venlo"]
            self.assertEqual(len(venlo_manifest), 1)
            self.assertGreater(venlo_manifest[0]["stopCount"], 0)
            self.assertEqual(
                json.loads((output / "stops" / "venlo.json").read_text()),
                [{"id": "venlo-stop", "name": "Venlo Station", "latitude": 51.3700, "longitude": 6.1681, "searchName": "venlo station"}],
            )
            self.assertTrue((output / "routes" / "venlo.json").exists())
            departures = json.loads((output / "departures" / "venlo.json").read_text())
            self.assertIn("venlo-stop", departures["stops"])
            self.assertEqual(len([entry for entry in manifest if entry["id"] == "venlo"]), 1)

    @staticmethod
    def _write_fixture(path: Path, cities: list[dict[str, object]]) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            stops = io.StringIO()
            writer = csv.writer(stops)
            writer.writerow(["stop_id", "stop_name", "stop_lat", "stop_lon"])
            writer.writerow(["venlo-stop", "Venlo Station", "51.3700", "6.1681"])
            for city in cities:
                if city["id"] == "venlo":
                    continue
                writer.writerow([f"{city['id']}-stop", city["name"], city["latitude"], city["longitude"]])
            archive.writestr("stops.txt", stops.getvalue())
            archive.writestr(
                "routes.txt",
                "route_id,route_short_name,route_long_name,route_type,agency_id\n"
                "route-1,1,City line,3,operator\n",
            )
            archive.writestr(
                "trips.txt",
                "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                "route-1,service-1,trip-1,Venlo Station,0\n",
            )
            archive.writestr(
                "stop_times.txt",
                "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                "trip-1,08:00:00,08:00:00,venlo-stop,1\n",
            )
            archive.writestr(
                "calendar_dates.txt",
                f"service_id,date,exception_type\nservice-1,{date.today():%Y%m%d},1\n",
            )


if __name__ == "__main__":
    unittest.main()
